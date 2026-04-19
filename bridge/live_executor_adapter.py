"""
LiveExecutorAdapter — wraps LiveExecutor with PaperExecutor-compatible interface.

Thin adapter that gives LiveExecutor the same synchronous interface
as PaperExecutor so the Orchestrator can use either without branching.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import threading
from datetime import datetime, timezone, timedelta
from typing import Any

from bridge.config import get_bridge_config, BridgeConfig
from bridge.decision_types import TradeDecision, PaperPosition
from bridge.live_executor import LiveExecutor


class LiveExecutorAdapter:
    """
    Thin adapter that gives LiveExecutor the same synchronous interface
    as PaperExecutor so the Orchestrator can use either without branching.
    """

    def __init__(self, initial_balance: float = 100_000.0):
        self._live = LiveExecutor(max_positions=3)
        self._live.confirm_session()  # auto-confirm for fully autonomous mode
        self._config = get_bridge_config()  # for TV→MT5 symbol name translation
        self._mt5_connector = None
        self._connect_mt5()  # initialize + login to MT5 before any trades
        self._positions_lock = threading.Lock()
        self.open_positions: dict[int, Any] = {}  # mirrors PaperExecutor interface
        self.closed_positions: list = []
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.grade_a_wins = 0
        self.grade_a_losses = 0
        # Sync balance from MT5 at startup instead of using CLI default
        mt5_balance = self._get_mt5_balance()
        if mt5_balance is not None:
            self.balance = mt5_balance
            self.initial_balance = initial_balance
            self.peak_balance = max(initial_balance, mt5_balance)
            print(f"[LIVE] MT5 account balance: ${mt5_balance:,.2f}", flush=True)
        else:
            self.balance = initial_balance
            self.initial_balance = initial_balance
            self.peak_balance = initial_balance

        # Daily P&L tracking — resets at midnight UTC.
        # Uses current MT5 balance as day-start baseline so the kill switch
        # reflects TODAY's P&L, not cumulative loss from initial_balance.
        self._day_start_balance: float = self.balance
        self._day_start_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Per-symbol loss cooldown: symbol -> UTC ISO timestamp when cooldown expires
        self._symbol_loss_cooldowns: dict[str, str] = {}
        self._symbol_loss_cooldown_hours: int = 4

    def _connect_mt5(self) -> None:
        """Initialize and login to MT5. Must be called before any order submission."""
        try:
            from data.mt5_connector import MT5Connector
            self._mt5_connector = MT5Connector()
            self._mt5_connector.connect()
            print("[LIVE] MT5 connected and logged in.", flush=True)
        except ImportError:
            print("[LIVE] WARNING: MetaTrader5 package not installed. Install with: pip install MetaTrader5", flush=True)
        except Exception as e:
            print(f"[LIVE] WARNING: MT5 connection failed: {e}", flush=True)
            print("[LIVE] Make sure MT5 is running and credentials are correct in .env", flush=True)

    def _get_mt5_balance(self) -> float | None:
        """Read current account balance from MT5."""
        try:
            import MetaTrader5 as mt5
            info = mt5.account_info()
            if info and info.balance > 0:
                self._mt5_login = info.login
                return float(info.balance)
        except Exception:
            pass
        return None

    def get_bridge_floating_pnl(self) -> float:
        """Floating P&L on bridge-only positions (comment contains ICT_Bridge).

        Filters out EA positions that share the account so health/dashboard
        can show a bridge-specific figure separate from the full-account
        balance used for FTMO risk rules.
        """
        try:
            import MetaTrader5 as mt5
            positions = mt5.positions_get() or []
            return float(sum(
                p.profit for p in positions
                if p.comment and "ICT_Bridge" in p.comment
            ))
        except Exception:
            return 0.0

    def get_bridge_open_count(self) -> int:
        """Number of bridge-owned open positions on MT5."""
        try:
            import MetaTrader5 as mt5
            positions = mt5.positions_get() or []
            return sum(
                1 for p in positions
                if p.comment and "ICT_Bridge" in p.comment
            )
        except Exception:
            return 0

    def _check_daily_reset(self) -> None:
        """Reset day-start balance at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day_start_date:
            # Read fresh balance from MT5 for the new day
            mt5_balance = self._get_mt5_balance()
            if mt5_balance is not None:
                self._day_start_balance = mt5_balance
                self.balance = mt5_balance
            else:
                self._day_start_balance = self.balance
            self._day_start_date = today
            # Clear per-symbol loss cooldowns on new day
            self._symbol_loss_cooldowns.clear()
            print(f"[LIVE] Daily reset — day-start balance: ${self._day_start_balance:,.2f}", flush=True)

    @property
    def daily_pnl(self) -> float:
        """Daily P&L *for FTMO risk rules* — uses full account balance delta.

        Compares current balance to the balance at the start of today (UTC),
        NOT to the initial $100k. This ensures the 2% daily loss kill switch
        reflects today's actual loss, not cumulative drawdown.

        This intentionally includes EA P&L because the 2%-daily / 10%-total
        FTMO limits apply to the whole account, not just bridge slice.
        """
        self._check_daily_reset()
        return round(self.balance - self._day_start_balance, 2)

    def is_symbol_on_loss_cooldown(self, symbol: str) -> bool:
        """Check if a symbol is in post-loss cooldown."""
        base = symbol.split(":")[-1]
        expiry_iso = self._symbol_loss_cooldowns.get(base)
        if not expiry_iso:
            return False
        now = datetime.now(timezone.utc)
        expiry = datetime.fromisoformat(expiry_iso)
        if now >= expiry:
            del self._symbol_loss_cooldowns[base]
            return False
        remaining = (expiry - now).total_seconds() / 60
        return True

    def set_symbol_loss_cooldown(self, symbol: str) -> None:
        """Set a cooldown on a symbol after a loss."""
        base = symbol.split(":")[-1]
        expiry = datetime.now(timezone.utc) + timedelta(hours=self._symbol_loss_cooldown_hours)
        self._symbol_loss_cooldowns[base] = expiry.isoformat()
        print(
            f"  [COOLDOWN] {base} on {self._symbol_loss_cooldown_hours}h loss cooldown "
            f"until {expiry.strftime('%H:%M')} UTC",
            flush=True,
        )

    def open_position(self, decision: TradeDecision, lot_size: float | None = None) -> dict:
        """Submit trade to MT5 and mirror state.

        Args:
            decision: Trade decision from Claude
            lot_size: Pre-calculated lot size from RiskBridge (preferred).
                      If not provided, falls back to internal calculation.
        """
        if not decision.is_trade:
            return {"success": False, "ticket": 0, "message": "Not a trade"}

        # Dedup check: same symbol+direction with similar entry price
        with self._positions_lock:
            for pos in self.open_positions.values():
                if pos.symbol == decision.symbol and pos.direction == decision.action:
                    entry_diff = abs(pos.entry_price - decision.entry_price)
                    threshold = pos.entry_price * 0.005  # 0.5% tolerance
                    if entry_diff < threshold:
                        return {"success": False, "ticket": 0,
                                "message": f"Duplicate: already {pos.direction} {decision.symbol} "
                                           f"@ {pos.entry_price:.4f}"}
                elif pos.symbol == decision.symbol:
                    return {"success": False, "ticket": 0,
                            "message": f"Already have {pos.direction} position on {decision.symbol}"}

        # Use pre-calculated lot size from RiskBridge if provided
        if lot_size is None:
            from bridge.risk_bridge import RiskBridge
            _rb = RiskBridge()
            lot_size = _rb.get_lot_size_live(
                decision.symbol, self.balance, decision.risk_pct,
                decision.entry_price, decision.sl_price, decision.action,
            )
            if lot_size <= 0:
                lot_size = 0.01

        # Translate TV symbol (e.g. "CBOT:YM1!") to MT5 symbol (e.g. "US30")
        mt5_decision = decision
        mt5_symbol = self._config.internal_symbol(decision.symbol)
        if mt5_symbol != decision.symbol:
            mt5_decision = copy.copy(decision)
            mt5_decision.symbol = mt5_symbol

        # MT5-level dedup: check broker for existing positions on same symbol
        try:
            import MetaTrader5 as mt5
            from bridge.config import tv_to_ftmo_symbol
            ftmo_sym = tv_to_ftmo_symbol(mt5_symbol)
            existing = mt5.positions_get(symbol=ftmo_sym)
            if existing:
                bridge_positions = [p for p in existing if p.comment and "ICT_Bridge" in p.comment]
                for bp in bridge_positions:
                    mt5_dir = "BUY" if bp.type == 0 else "SELL"
                    if mt5_dir == decision.action:
                        return {"success": False, "ticket": 0,
                                "message": f"MT5 dedup: already have {mt5_dir} on {ftmo_sym} (#{bp.ticket})"}
        except ImportError:
            pass
        except Exception as e:
            print(f"[LIVE] MT5 dedup check warning: {e}", flush=True)

        # Run async submit_trade in a dedicated thread with its own event loop
        result_holder: list[Any] = []
        error_holder: list[Exception] = []

        def _run_in_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                r = loop.run_until_complete(self._live.submit_trade(mt5_decision, lot_size))
                result_holder.append(r)
            except Exception as e:
                error_holder.append(e)
            finally:
                loop.close()

        t = threading.Thread(target=_run_in_thread, daemon=True)
        t.start()
        t.join(timeout=15)

        if error_holder:
            return {"success": False, "ticket": 0, "message": f"MT5 error: {error_holder[0]}"}
        if not result_holder:
            return {"success": False, "ticket": 0, "message": "MT5 submit timed out"}
        result = result_holder[0]

        if result["success"]:
            pos = PaperPosition(
                ticket=result["ticket"],
                symbol=decision.symbol,
                direction=decision.action,
                entry_price=result["fill_price"] or decision.entry_price,
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
                current_price=result["fill_price"] or decision.entry_price,
                trailing_sl=decision.sl_price,
            )
            with self._positions_lock:
                self.open_positions[result["ticket"]] = pos

        return result

    def check_positions(self, current_prices: dict[str, float]) -> list[dict]:
        """
        Check MT5 positions against current prices.
        Handles TP1 partial close, trailing SL sync to MT5, and SL/TP detection.

        Lock is held only for in-memory state updates; MT5 I/O runs outside
        the lock to avoid blocking other threads during network calls.
        """
        from bridge.risk_bridge import calculate_pnl

        events = []
        to_remove = []
        # Deferred MT5 operations: run outside lock
        mt5_ops: list[tuple[str, int, Any, Any]] = []  # (op, ticket, arg1, arg2)

        with self._positions_lock:
            for ticket, pos in self.open_positions.items():
                price = current_prices.get(pos.symbol)
                if price is None:
                    continue

                pos.current_price = price
                pos.floating_pnl = calculate_pnl(
                    pos.symbol.split(":")[-1], pos.entry_price, price, pos.lot_size, pos.direction
                )

                # -- TP1 partial close (50% at TP1, move SL to breakeven) --
                if not pos.tp1_hit and pos.tp2_price > 0:
                    tp1_hit = False
                    if pos.direction == "BUY" and price >= pos.tp_price:
                        tp1_hit = True
                    elif pos.direction == "SELL" and price <= pos.tp_price:
                        tp1_hit = True

                    if tp1_hit:
                        pos.tp1_hit = True
                        partial_pnl = calculate_pnl(
                            pos.symbol.split(":")[-1], pos.entry_price, pos.tp_price,
                            pos.lot_size * 0.5, pos.direction
                        )

                        self.balance += partial_pnl
                        pos.lot_size = round(pos.lot_size * 0.5, 4)
                        pos.trailing_sl = pos.entry_price  # breakeven

                        # Defer MT5 I/O: partial close + SL modify
                        mt5_ops.append(("partial_close", ticket, pos, pos.lot_size))
                        mt5_ops.append(("modify_sl", ticket, pos.entry_price, None))

                        print(
                            f"  [LIVE TP1] {pos.symbol} TP1 hit @ {pos.tp_price:.2f} "
                            f"partial PnL={partial_pnl:+.2f} — SL moved to breakeven",
                            flush=True,
                        )

                # -- Check TP2/final TP hit --
                tp_final = pos.tp2_price if (pos.tp2_price > 0 and pos.tp1_hit) else pos.tp_price
                closed_reason = None
                if pos.direction == "BUY":
                    if price <= pos.trailing_sl and pos.trailing_sl != pos.sl_price:
                        closed_reason = "TRAILING_SL"
                    elif price <= pos.sl_price:
                        closed_reason = "SL"
                    elif price >= tp_final:
                        closed_reason = "TP2" if pos.tp1_hit else "TP"
                else:
                    if price >= pos.trailing_sl and pos.trailing_sl != pos.sl_price:
                        closed_reason = "TRAILING_SL"
                    elif price >= pos.sl_price:
                        closed_reason = "SL"
                    elif price <= tp_final:
                        closed_reason = "TP2" if pos.tp1_hit else "TP"

                if closed_reason:
                    pnl = pos.floating_pnl
                    risk_pnl = abs(calculate_pnl(
                        pos.symbol, pos.entry_price, pos.sl_price, pos.lot_size, pos.direction
                    ))
                    r_mult = pnl / risk_pnl if risk_pnl > 0 else 0.0
                    closed_at = datetime.now(timezone.utc).isoformat()
                    exit_level = pos.sl_price if closed_reason == "SL" else (
                        pos.trailing_sl if closed_reason == "TRAILING_SL" else tp_final)
                    to_remove.append(ticket)
                    self.balance += pnl
                    self.peak_balance = max(self.peak_balance, self.balance)
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
                        # Set per-symbol loss cooldown
                        self.set_symbol_loss_cooldown(pos.symbol)
                    events.append({
                        "ticket": ticket, "symbol": pos.symbol,
                        "direction": pos.direction,
                        "entry_price": pos.entry_price,
                        "exit_price": exit_level,
                        "actual_trigger_price": price,
                        "pnl": round(pnl, 2), "r_multiple": round(r_mult, 2),
                        "reason": closed_reason, "balance": round(self.balance, 2),
                        "opened_at": pos.opened_at, "closed_at": closed_at,
                        "sl_price": pos.sl_price, "tp_price": pos.tp_price,
                        "tp2_price": pos.tp2_price, "lot_size": pos.lot_size,
                        "ict_grade": pos.ict_grade, "ict_score": pos.ict_score,
                        "trailing_sl": pos.trailing_sl, "tp1_hit": pos.tp1_hit,
                    })
                    continue

                # -- Update trailing stop (in-memory only; MT5 sync deferred) --
                old_trailing = pos.trailing_sl
                self._update_trailing_stop(pos)
                if pos.trailing_sl != old_trailing:
                    mt5_ops.append(("trail_sl", ticket, pos.trailing_sl, old_trailing))
                    print(
                        f"  [LIVE TRAIL] {pos.symbol} #{ticket} trailing SL "
                        f"{old_trailing:.2f} → {pos.trailing_sl:.2f} (syncing to MT5)",
                        flush=True,
                    )

            for t in to_remove:
                self.open_positions.pop(t, None)

        # -- Execute deferred MT5 I/O outside the lock --
        for op, ticket, arg1, arg2 in mt5_ops:
            if op == "partial_close":
                self._mt5_partial_close(ticket, arg1, arg1.lot_size)
            elif op == "modify_sl":
                self._mt5_modify_sl(ticket, arg1)
            elif op == "trail_sl":
                self._mt5_modify_sl_with_revert(ticket, arg1, arg2)

        return events

    def _update_trailing_stop(self, pos: PaperPosition) -> None:
        """Move trailing stop based on R-multiple progress (same logic as PaperExecutor)."""
        r = pos.r_multiple
        risk = abs(pos.entry_price - pos.sl_price)
        if risk == 0 or r < 1.0:
            return

        new_sl = pos.entry_price  # breakeven at 1R
        if pos.direction == "BUY":
            trail_level = pos.entry_price + (r - 0.5) * risk
            new_sl = max(new_sl, trail_level)
            if new_sl > pos.trailing_sl:
                pos.trailing_sl = round(new_sl, 5)
        else:
            trail_level = pos.entry_price - (r - 0.5) * risk
            new_sl = min(new_sl, trail_level)
            if new_sl < pos.trailing_sl:
                pos.trailing_sl = round(new_sl, 5)

    def _mt5_modify_sl(self, ticket: int, new_sl: float) -> bool:
        """Update SL on MT5 position. Returns True on success."""
        success = False
        def _run():
            nonlocal success
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(self._live.modify_sl(ticket, new_sl))
                if result:
                    success = True
                else:
                    print(f"  [MT5_SL] Failed to modify SL for #{ticket} to {new_sl:.5f}", flush=True)
            except Exception as e:
                print(f"  [MT5_SL] Error modifying SL for #{ticket}: {e}", flush=True)
            finally:
                loop.close()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=10)
        return success

    def _mt5_modify_sl_with_revert(self, ticket: int, new_sl: float, old_sl: float) -> None:
        """Update SL on MT5 and revert in-memory trailing_sl if MT5 call fails."""
        if not self._mt5_modify_sl(ticket, new_sl):
            with self._positions_lock:
                pos = self.open_positions.get(ticket)
                if pos:
                    pos.trailing_sl = old_sl
                    print(
                        f"  [MT5_SL] Reverted trailing SL for #{ticket} to {old_sl:.5f} (MT5 sync failed)",
                        flush=True,
                    )

    def _mt5_partial_close(self, ticket: int, pos: PaperPosition, close_lots: float) -> None:
        """Partial close on MT5 — close specified lot size."""
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                half_lots = round(close_lots, 2)
                result = loop.run_until_complete(
                    self._live.partial_close_tp1(ticket, pos.tp_price)
                )
                if not result:
                    print(f"  [MT5_TP1] Failed partial close for #{ticket}", flush=True)
            except Exception as e:
                print(f"  [MT5_TP1] Error partial close for #{ticket}: {e}", flush=True)
            finally:
                loop.close()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=15)

    def get_account_summary(self) -> dict:
        daily_pnl_pct = self.daily_pnl / self._day_start_balance if self._day_start_balance else 0
        total_dd = (self.peak_balance - self.balance) / self.peak_balance if self.peak_balance > 0 else 0
        grade_a_total = self.grade_a_wins + self.grade_a_losses
        return {
            "balance": round(self.balance, 2),
            "initial_balance": self.initial_balance,
            "daily_pnl": self.daily_pnl,
            "daily_pnl_pct": f"{daily_pnl_pct:.2%}",
            "total_drawdown_pct": f"{total_dd:.2%}",
            "open_positions": len(self.open_positions),
            "closed_today": len(self.closed_positions),
            "wins": self.wins,
            "losses": self.losses,
            "consecutive_losses": self.consecutive_losses,
            "grade_a_win_rate": f"{self.grade_a_wins/grade_a_total:.0%}" if grade_a_total > 0 else "N/A",
            "can_trade": True,
        }
