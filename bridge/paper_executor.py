"""
Paper Trading Executor — simulates MT5 fills with position tracking.

Fills at current price, tracks positions in memory + JSONL log,
applies trailing stops and partial TP.

Usage:
    from bridge.paper_executor import PaperExecutor
    executor = PaperExecutor(initial_balance=10000)
    result = executor.open_position(decision)
    executor.check_positions(current_prices)
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bridge.decision_types import TradeDecision, PaperPosition, ClosedPosition
from bridge.live_executor import BRIDGE_MAX_POSITIONS


# ---------------------------------------------------------------------------
# Paper Executor
# ---------------------------------------------------------------------------

class PaperExecutor:
    """
    Simulates trade execution for paper trading mode.

    Features:
    - Instant fills at current price
    - Position tracking with floating P&L
    - Trailing stop management
    - Partial TP at first target
    - JSONL trade log
    - Account balance tracking
    """

    def __init__(
        self,
        initial_balance: float = 100_000.0,
        max_positions: int = BRIDGE_MAX_POSITIONS,  # single source of truth
        log_dir: Path | None = None,
    ):
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.peak_balance = initial_balance
        self.max_positions = max_positions

        self._positions_lock = threading.Lock()
        self.open_positions: dict[int, PaperPosition] = {}
        self.closed_positions: list[ClosedPosition] = []
        self._next_ticket = self._load_ticket_counter()

        # Logging
        self.log_dir = log_dir or Path(__file__).resolve().parent.parent / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.trade_log = self.log_dir / f"paper_trades_{session_id}.jsonl"

        # Stats
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.grade_a_wins = 0
        self.grade_a_losses = 0

        # Daily P&L: track from day-start balance, reset at midnight UTC
        self._day_start_balance: float = initial_balance
        self._day_start_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Slippage simulation: half-spread applied against trader on entry & exit
        # Keys are base symbol names (no exchange prefix)
        self._slippage_bps: dict[str, float] = {
            "BTCUSD": 3.0,    # ~$2 on $70k
            "ETHUSD": 5.0,    # ~$0.10 on $2k
            "SOLUSD": 10.0,   # ~$0.015 on $150
            "AVAXUSD": 15.0,
            "LINKUSD": 15.0,
            "DOGEUSD": 20.0,
            "EURUSD": 0.5,    # tight forex spread
            "GBPUSD": 0.8,
            "XAUUSD": 3.0,
            "UKOIL": 5.0,
            "US30": 2.0,
            "US100": 2.0,
            "US500": 1.5,
        }

    # ------------------------------------------------------------------
    # Ticket counter persistence (avoids collisions across restarts)
    # ------------------------------------------------------------------

    _TICKET_FILE = Path.home() / ".tradingview-mcp" / "paper_ticket_counter.txt"

    @classmethod
    def _load_ticket_counter(cls) -> int:
        """Load the last ticket number from disk, or start at 100001."""
        try:
            if cls._TICKET_FILE.exists():
                val = int(cls._TICKET_FILE.read_text().strip())
                if val >= 100_001:
                    return val
        except (ValueError, OSError):
            pass
        return 100_001

    def _save_ticket_counter(self) -> None:
        """Persist the current ticket counter to disk."""
        try:
            self._TICKET_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._TICKET_FILE.write_text(str(self._next_ticket))
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Opposite-direction helpers
    # ------------------------------------------------------------------

    def find_opposite_positions(self, symbol: str, new_direction: str) -> list[tuple[int, Any]]:
        """Find open positions on the same symbol with the opposite direction."""
        result = []
        with self._positions_lock:
            for ticket, pos in self.open_positions.items():
                if pos.symbol == symbol and pos.direction != new_direction:
                    result.append((ticket, pos))
        return result

    def close_position_by_ticket(self, ticket: int, reason: str = "SIGNAL_FLIP") -> dict | None:
        """Close a specific position by ticket. Returns close event dict or None."""
        with self._positions_lock:
            pos = self.open_positions.get(ticket)
        if pos is None:
            return None

        pnl = pos.floating_pnl
        r_mult = pos.r_multiple
        closed_at = datetime.now(timezone.utc).isoformat()

        with self._positions_lock:
            self.open_positions.pop(ticket, None)

        self.balance += pnl
        self.peak_balance = max(self.peak_balance, self.balance)
        if pnl >= 0:
            self.wins += 1
            self.consecutive_losses = 0
        else:
            self.losses += 1
            self.consecutive_losses += 1

        return {
            "ticket": ticket, "symbol": pos.symbol,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "exit_price": pos.current_price,
            "actual_trigger_price": pos.current_price,
            "pnl": round(pnl, 2), "r_multiple": round(r_mult, 2),
            "reason": reason, "balance": round(self.balance, 2),
            "opened_at": pos.opened_at, "closed_at": closed_at,
            "sl_price": pos.sl_price, "tp_price": pos.tp_price,
            "tp2_price": pos.tp2_price, "lot_size": pos.lot_size,
            "ict_grade": pos.ict_grade, "ict_score": pos.ict_score,
            "trailing_sl": pos.trailing_sl, "tp1_hit": pos.tp1_hit,
        }

    # ------------------------------------------------------------------
    # Open position
    # ------------------------------------------------------------------

    def open_position(self, decision: TradeDecision, lot_size: float | None = None) -> dict:
        """
        Open a paper position from a trade decision.

        Args:
            decision: Trade decision from Claude
            lot_size: Pre-calculated lot size from RiskBridge (preferred).
                      If not provided, falls back to internal calculation.

        Returns:
            {"success": bool, "ticket": int, "message": str}
        """
        if not decision.is_trade:
            return {"success": False, "ticket": 0, "message": "Decision is not a trade"}

        with self._positions_lock:
            if len(self.open_positions) >= self.max_positions:
                return {"success": False, "ticket": 0,
                        "message": f"Max {self.max_positions} positions reached"}

            # Check for duplicate: same symbol+direction with similar entry price
            for pos in self.open_positions.values():
                if pos.symbol == decision.symbol and pos.direction == decision.action:
                    entry_diff = abs(pos.entry_price - decision.entry_price)
                    threshold = pos.entry_price * 0.005  # 0.5% tolerance
                    if entry_diff < threshold:
                        return {"success": False, "ticket": 0,
                                "message": f"Duplicate: already {pos.direction} {decision.symbol} "
                                           f"@ {pos.entry_price:.4f} (new entry {decision.entry_price:.4f} "
                                           f"within 0.5%)"}
                # Opposite direction is now handled by analysis_pipeline
                # via close_position_by_ticket before calling open_position

        ticket = self._next_ticket
        self._next_ticket += 1
        self._save_ticket_counter()

        # Use pre-calculated lot size from RiskBridge if provided
        if lot_size is None:
            risk_amount = self.balance * decision.risk_pct
            risk_distance = abs(decision.entry_price - decision.sl_price)
            if risk_distance <= 0:
                return {"success": False, "ticket": 0, "message": "Invalid SL distance"}
            lot_size = round(risk_amount / risk_distance, 4)

        # Apply slippage: entry price moved against trader
        entry_price = decision.entry_price
        base_sym = decision.symbol.split(":")[-1]
        slip_bps = self._slippage_bps.get(base_sym, 2.0)
        slip_amount = entry_price * (slip_bps / 10_000)
        if decision.action == "BUY":
            entry_price += slip_amount  # buy higher
        else:
            entry_price -= slip_amount  # sell lower
        entry_price = round(entry_price, 5)

        position = PaperPosition(
            ticket=ticket,
            symbol=decision.symbol,
            direction=decision.action,
            entry_price=entry_price,
            sl_price=decision.sl_price,
            tp_price=decision.tp_price,
            tp2_price=decision.tp2_price,
            trade_type=decision.trade_type,
            lot_size=lot_size,
            risk_pct=decision.risk_pct,
            opened_at=datetime.now(timezone.utc).isoformat(),
            ict_grade=decision.grade,
            ict_score=decision.ict_score,
            reasoning=decision.reasoning,
            current_price=decision.entry_price,
            trailing_sl=decision.sl_price,
        )

        with self._positions_lock:
            self.open_positions[ticket] = position
        self._log_event("OPEN", {
            **position.to_dict(),
            "risk_pct": position.risk_pct,
            "reasoning": position.reasoning,
            "ict_score": position.ict_score,
            "trade_type": position.trade_type,
            "tp2_price": position.tp2_price,
        })

        return {
            "success": True,
            "ticket": ticket,
            "message": f"{decision.action} {decision.symbol} @ {decision.entry_price:.2f} "
                       f"SL={decision.sl_price:.2f} TP={decision.tp_price:.2f} "
                       f"Size={lot_size:.4f}",
        }

    # ------------------------------------------------------------------
    # Check positions (price updates, SL/TP/trailing)
    # ------------------------------------------------------------------

    def check_positions(self, current_prices: dict[str, float]) -> list[dict]:
        """
        Update all open positions with current prices.
        Closes positions that hit SL, TP, or trailing SL.

        Args:
            current_prices: {"BTCUSD": 69000.0, ...}

        Returns:
            List of close events (if any).
        """
        events: list[dict] = []
        to_close: list[tuple[int, str, float]] = []  # (ticket, reason, exit_price)

        with self._positions_lock:
            for ticket, pos in self.open_positions.items():
                price = current_prices.get(pos.symbol)
                if price is None:
                    continue

                pos.current_price = price

                # Calculate floating P&L using proper tick_value conversion
                from bridge.risk_bridge import calculate_pnl
                pos.floating_pnl = calculate_pnl(
                    pos.symbol.split(":")[-1], pos.entry_price, price, pos.lot_size, pos.direction
                )

                # Check TP1 hit (partial close at 50%)
                if not pos.tp1_hit and pos.tp2_price > 0:
                    tp1_triggered = (
                        (pos.direction == "BUY" and price >= pos.tp_price) or
                        (pos.direction == "SELL" and price <= pos.tp_price)
                    )
                    if tp1_triggered:
                        pos.tp1_hit = True
                        partial_pnl = calculate_pnl(
                            pos.symbol.split(":")[-1], pos.entry_price, pos.tp_price,
                            pos.lot_size * 0.5, pos.direction
                        )
                        self.balance += partial_pnl
                        pos.lot_size = round(pos.lot_size * 0.5, 4)
                        pos.trailing_sl = pos.entry_price  # move to breakeven
                        self._log_event("PARTIAL_CLOSE", {
                            "ticket": ticket, "symbol": pos.symbol,
                            "exit_price": pos.tp_price, "pnl": round(partial_pnl, 2),
                            "reason": "TP1_PARTIAL", "remaining_lots": pos.lot_size,
                        })
                        print(f"  [PARTIAL] {pos.symbol} TP1 hit @ {pos.tp_price:.2f} partial PnL={partial_pnl:+.2f}", flush=True)

                # Check TP2 hit (full close of remaining)
                tp_final = pos.tp2_price if (pos.tp2_price > 0 and pos.tp1_hit) else pos.tp_price
                if pos.direction == "BUY" and price >= tp_final:
                    to_close.append((ticket, "TP2" if pos.tp1_hit else "TP", tp_final))
                    continue
                elif pos.direction == "SELL" and price <= tp_final:
                    to_close.append((ticket, "TP2" if pos.tp1_hit else "TP", tp_final))
                    continue

                # Check SL hit
                if pos.direction == "BUY" and price <= pos.sl_price:
                    to_close.append((ticket, "SL", pos.sl_price))
                    continue
                elif pos.direction == "SELL" and price >= pos.sl_price:
                    to_close.append((ticket, "SL", pos.sl_price))
                    continue

                # Check trailing SL
                if pos.trailing_sl != pos.sl_price:
                    if pos.direction == "BUY" and price <= pos.trailing_sl:
                        to_close.append((ticket, "TRAILING_SL", pos.trailing_sl))
                        continue
                    elif pos.direction == "SELL" and price >= pos.trailing_sl:
                        to_close.append((ticket, "TRAILING_SL", pos.trailing_sl))
                        continue

                # Update trailing stop (move to breakeven at 1R, trail at 0.5R increments)
                self._update_trailing_stop(pos)

        # Close positions (outside lock — _close_position acquires its own lock)
        for ticket, reason, exit_price in to_close:
            event = self._close_position(ticket, reason, exit_price)
            if event:
                events.append(event)

        return events

    def _update_trailing_stop(self, pos: PaperPosition) -> None:
        """Move trailing stop based on R-multiple progress.

        Mirrors the live-executor algorithm (REVISED 2026-04-26). See
        bridge/live_executor_adapter.py::_update_trailing_stop for full
        rationale — short version: hold off trailing until R >= 1.5 to
        let ICT shake-out retraces complete, then lock +0.5R / +1R /
        (R-1) as R grows.
        """
        r = pos.r_multiple
        risk = abs(pos.entry_price - pos.sl_price)
        if risk == 0 or r < 1.5:
            return

        if r < 2.0:
            floor_r = 0.5
        elif r < 3.0:
            floor_r = 1.0
        else:
            floor_r = r - 1.0

        if pos.direction == "BUY":
            new_sl = pos.entry_price + floor_r * risk
            if new_sl > pos.trailing_sl:
                pos.trailing_sl = round(new_sl, 5)
        else:
            new_sl = pos.entry_price - floor_r * risk
            if new_sl < pos.trailing_sl:
                pos.trailing_sl = round(new_sl, 5)

    # ------------------------------------------------------------------
    # Close position
    # ------------------------------------------------------------------

    def _close_position(self, ticket: int, reason: str, exit_price: float) -> dict | None:
        """Close a position and record the result."""
        with self._positions_lock:
            pos = self.open_positions.pop(ticket, None)
        if pos is None:
            return None

        # actual_price is the real market price that triggered the close
        # exit_price is the SL/TP level used for P&L calculation
        actual_price = pos.current_price  # last known market price
        closed_at = datetime.now(timezone.utc).isoformat()

        from bridge.risk_bridge import calculate_pnl
        pnl = calculate_pnl(
            pos.symbol.split(":")[-1], pos.entry_price, exit_price, pos.lot_size, pos.direction
        )

        risk_pnl = abs(calculate_pnl(
            pos.symbol, pos.entry_price, pos.sl_price, pos.lot_size, pos.direction
        ))
        r_mult = pnl / risk_pnl if risk_pnl > 0 else 0.0

        closed = ClosedPosition(
            ticket=ticket,
            symbol=pos.symbol,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            sl_price=pos.sl_price,
            tp_price=pos.tp_price,
            lot_size=pos.lot_size,
            pnl=round(pnl, 2),
            r_multiple=round(r_mult, 2),
            opened_at=pos.opened_at,
            closed_at=closed_at,
            close_reason=reason,
            ict_grade=pos.ict_grade,
            ict_score=pos.ict_score,
        )

        self.closed_positions.append(closed)
        self.balance += pnl
        self.peak_balance = max(self.peak_balance, self.balance)

        # Track win/loss streaks
        if pnl >= 0:
            self.wins += 1
            self.consecutive_losses = 0
            if pos.ict_grade == "A":
                self.grade_a_wins += 1
        else:
            self.losses += 1
            self.consecutive_losses += 1
            if pos.ict_grade == "A":
                self.grade_a_losses += 1

        self._log_event("CLOSE", {
            "ticket": ticket,
            "symbol": pos.symbol,
            "direction": pos.direction,
            "entry": pos.entry_price,
            "exit": exit_price,
            "actual_trigger_price": actual_price,
            "sl_price": pos.sl_price,
            "tp_price": pos.tp_price,
            "tp2_price": pos.tp2_price,
            "pnl": round(pnl, 2),
            "r_multiple": round(r_mult, 2),
            "reason": reason,
            "balance": round(self.balance, 2),
            "opened_at": pos.opened_at,
            "closed_at": closed_at,
            "lot_size": pos.lot_size,
            "ict_grade": pos.ict_grade,
            "ict_score": pos.ict_score,
            "trailing_sl": pos.trailing_sl,
            "tp1_hit": pos.tp1_hit,
        })

        return {
            "ticket": ticket,
            "symbol": pos.symbol,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "actual_trigger_price": actual_price,
            "pnl": round(pnl, 2),
            "r_multiple": round(r_mult, 2),
            "reason": reason,
            "balance": round(self.balance, 2),
            "opened_at": pos.opened_at,
            "closed_at": closed_at,
        }

    # ------------------------------------------------------------------
    # Account state
    # ------------------------------------------------------------------

    def _check_daily_reset(self) -> None:
        """Reset day-start balance at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day_start_date:
            self._day_start_balance = self.balance
            self._day_start_date = today
            print(f"[PAPER] Daily reset — day-start balance: ${self._day_start_balance:,.2f}", flush=True)

    @property
    def daily_pnl(self) -> float:
        """Today's P&L — resets at midnight UTC."""
        self._check_daily_reset()
        return round(self.balance - self._day_start_balance, 2)

    @property
    def daily_pnl_pct(self) -> float:
        self._check_daily_reset()
        return self.daily_pnl / self._day_start_balance if self._day_start_balance else 0.0

    @property
    def total_drawdown_pct(self) -> float:
        if self.peak_balance <= 0:
            return 0.0
        return (self.peak_balance - self.balance) / self.peak_balance

    @property
    def can_trade(self) -> tuple[bool, str]:
        """Check account limits. Daily loss limit: 2% (self-imposed, prop-firm safe)."""
        if self.daily_pnl_pct <= -0.02:
            return False, f"Daily loss {self.daily_pnl_pct:.1%} hit 2% limit"
        if self.total_drawdown_pct >= 0.08:
            return False, f"Total DD {self.total_drawdown_pct:.1%} hit 8% soft limit"
        return True, "OK"

    def get_account_summary(self) -> dict:
        grade_a_total = self.grade_a_wins + self.grade_a_losses
        grade_a_wr = f"{self.grade_a_wins/grade_a_total:.0%}" if grade_a_total > 0 else "N/A"
        return {
            "balance": round(self.balance, 2),
            "initial_balance": self.initial_balance,
            "daily_pnl": self.daily_pnl,
            "daily_pnl_pct": f"{self.daily_pnl_pct:.2%}",
            "total_drawdown_pct": f"{self.total_drawdown_pct:.2%}",
            "open_positions": len(self.open_positions),
            "closed_today": len(self.closed_positions),
            "wins": self.wins,
            "losses": self.losses,
            "consecutive_losses": self.consecutive_losses,
            "grade_a_win_rate": grade_a_wr,
            "can_trade": self.can_trade[0],
        }

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_event(self, event_type: str, data: dict) -> None:
        """Append event to JSONL trade log."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **data,
        }
        with open(self.trade_log, "a") as f:
            f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    executor = PaperExecutor(initial_balance=10_000)

    # Simulate a BUY
    decision = TradeDecision(
        action="BUY",
        symbol="BTCUSD",
        entry_price=69000.0,
        sl_price=68500.0,
        tp_price=70000.0,
        confidence=80,
        risk_pct=0.01,
        reasoning="Test trade",
        grade="B",
        ict_score=76.8,
    )

    result = executor.open_position(decision)
    print(f"Open: {result}")
    print(f"Account: {json.dumps(executor.get_account_summary(), indent=2)}")

    # Simulate price move to TP
    events = executor.check_positions({"BTCUSD": 70000.0})
    print(f"Events: {events}")
    print(f"Account after TP: {json.dumps(executor.get_account_summary(), indent=2)}")
