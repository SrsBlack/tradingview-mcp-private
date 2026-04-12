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
from datetime import datetime, timezone
from typing import Any

from bridge.config import get_bridge_config, BridgeConfig
from bridge.decision_types import TradeDecision, PaperPosition
from bridge.live_executor import LiveExecutor


class LiveExecutorAdapter:
    """
    Thin adapter that gives LiveExecutor the same synchronous interface
    as PaperExecutor so the Orchestrator can use either without branching.
    """

    def __init__(self, initial_balance: float = 10_000.0):
        self._live = LiveExecutor(max_positions=3)
        self._live.confirm_session()  # auto-confirm for fully autonomous mode
        self._config = get_bridge_config()  # for TV→MT5 symbol name translation
        self._mt5_connector = None
        self._connect_mt5()  # initialize + login to MT5 before any trades
        self.open_positions: dict[int, Any] = {}  # mirrors PaperExecutor interface
        self.closed_positions: list = []
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.grade_a_wins = 0
        self.grade_a_losses = 0
        # Balance pulled from MT5 on each check; use initial as fallback
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.peak_balance = initial_balance

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

    @property
    def daily_pnl(self) -> float:
        return round(self.balance - self.initial_balance, 2)

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
            risk_amount = self.balance * decision.risk_pct
            risk_dist = abs(decision.entry_price - decision.sl_price)
            lot_size = round(risk_amount / risk_dist, 4) if risk_dist > 0 else 0.01

        # Translate TV symbol (e.g. "CBOT:YM1!") to MT5 symbol (e.g. "US30")
        mt5_decision = decision
        mt5_symbol = self._config.internal_symbol(decision.symbol)
        if mt5_symbol != decision.symbol:
            mt5_decision = copy.copy(decision)
            mt5_decision.symbol = mt5_symbol

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
            self.open_positions[result["ticket"]] = pos

        return result

    def check_positions(self, current_prices: dict[str, float]) -> list[dict]:
        """
        Check MT5 positions against current prices.
        MT5 manages SL/TP natively — this just syncs our local state.
        """
        events = []
        to_remove = []

        for ticket, pos in self.open_positions.items():
            price = current_prices.get(pos.symbol)
            if price is None:
                continue

            pos.current_price = price
            if pos.direction == "BUY":
                pos.floating_pnl = (price - pos.entry_price) * pos.lot_size
            else:
                pos.floating_pnl = (pos.entry_price - price) * pos.lot_size

            closed_reason = None
            if pos.direction == "BUY":
                if price <= pos.sl_price:
                    closed_reason = "SL"
                elif price >= (pos.tp2_price if pos.tp2_price > 0 else pos.tp_price):
                    closed_reason = "TP"
            else:
                if price >= pos.sl_price:
                    closed_reason = "SL"
                elif price <= (pos.tp2_price if pos.tp2_price > 0 else pos.tp_price):
                    closed_reason = "TP"

            if closed_reason:
                pnl = pos.floating_pnl
                risk = abs(pos.entry_price - pos.sl_price)
                r_mult = pnl / (risk * pos.lot_size) if risk > 0 and pos.lot_size > 0 else 0.0
                closed_at = datetime.now(timezone.utc).isoformat()
                exit_level = pos.sl_price if closed_reason == "SL" else (
                    pos.tp2_price if pos.tp2_price > 0 else pos.tp_price)
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
                })

        for t in to_remove:
            self.open_positions.pop(t, None)

        return events

    def get_account_summary(self) -> dict:
        daily_pnl_pct = self.daily_pnl / self.initial_balance if self.initial_balance else 0
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
