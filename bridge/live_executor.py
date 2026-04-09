"""
Live Executor — wraps trading-ai-v2's MT5Executor for real trade execution.

Safety gates:
- Magic number 99002 for bridge signals
- Max 2 positions from bridge (separate from EA's 10-position limit)
- Kill switch: 3 consecutive losses -> pause + alert
- Spread guard (inherited from MT5Executor)
- Double confirmation for first live trade of session

Usage:
    from bridge.live_executor import LiveExecutor
    executor = LiveExecutor()
    result = await executor.submit_trade(decision, lot_size)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bridge.config import ensure_trading_ai_path
from bridge.decision_types import TradeDecision

# Ensure trading-ai-v2 is importable
ensure_trading_ai_path()

from core.types import Direction, Symbol, EngineName
from core.events import ExecutionEvent


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BRIDGE_MAGIC = 99002
BRIDGE_MAX_POSITIONS = 2
BRIDGE_KILL_SWITCH_LOSSES = 3
BRIDGE_STRATEGY_NAME = "ICT_Bridge"


# ---------------------------------------------------------------------------
# Live Executor
# ---------------------------------------------------------------------------

class LiveExecutor:
    """
    Wraps MT5Executor with bridge-specific safety gates.

    Safety layers:
    1. Bridge position limit (max 2, separate from EA's 10)
    2. Kill switch (3 consecutive losses = hard pause)
    3. Session confirmation (first trade requires explicit confirm)
    4. Spread guard (inherited from MT5Executor)
    5. FTMO compliance (checked by RiskBridge before reaching here)
    """

    def __init__(self, max_positions: int = BRIDGE_MAX_POSITIONS):
        self._mt5_executor = None
        self.max_positions = max_positions
        self.open_tickets: dict[int, dict] = {}  # ticket -> trade info
        self.consecutive_losses = 0
        self.session_confirmed = False
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self._kill_switch = False

    @property
    def mt5(self):
        """Lazy-load MT5Executor (requires MT5 to be running)."""
        if self._mt5_executor is None:
            from execution.mt5_executor import MT5Executor
            self._mt5_executor = MT5Executor(max_spread_multiplier=3.0)
        return self._mt5_executor

    # ------------------------------------------------------------------
    # Safety gates
    # ------------------------------------------------------------------

    def pre_flight_check(self) -> tuple[bool, str]:
        """Run all safety checks before submitting a trade."""
        if self._kill_switch:
            return False, f"Kill switch active ({self.consecutive_losses} consecutive losses)"

        if len(self.open_tickets) >= self.max_positions:
            return False, f"Max {self.max_positions} bridge positions reached"

        if not self.session_confirmed:
            return False, "Session not confirmed. Call confirm_session() first."

        return True, "OK"

    def confirm_session(self) -> None:
        """Explicit confirmation to enable live trading for this session."""
        self.session_confirmed = True
        print(f"[LIVE] Session confirmed. Live trading ENABLED.", flush=True)

    def reset_kill_switch(self) -> None:
        """Reset kill switch after manual review."""
        self._kill_switch = False
        self.consecutive_losses = 0
        print("[LIVE] Kill switch reset.", flush=True)

    # ------------------------------------------------------------------
    # Submit trade
    # ------------------------------------------------------------------

    async def submit_trade(
        self,
        decision: TradeDecision,
        lot_size: float,
    ) -> dict:
        """
        Submit a live trade to MT5.

        Args:
            decision: TradeDecision from Claude
            lot_size: Position size from RiskBridge

        Returns:
            {"success": bool, "ticket": int, "message": str, "fill_price": float}
        """
        # Pre-flight
        ok, reason = self.pre_flight_check()
        if not ok:
            return {"success": False, "ticket": 0, "message": reason, "fill_price": 0.0}

        # Build ExecutionEvent
        direction = Direction.BULLISH if decision.action == "BUY" else Direction.BEARISH

        event = ExecutionEvent(
            symbol=decision.symbol,
            direction=direction,
            lot_size=lot_size,
            entry_price=decision.entry_price,
            sl_price=decision.sl_price,
            tp_price=decision.tp_price,
            ticket=0,
            signal_id=f"bridge_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            engine=EngineName.ICT,
            strategy_name=BRIDGE_STRATEGY_NAME,
            magic=BRIDGE_MAGIC,
        )

        # Submit to MT5
        result = await self.mt5.submit(event)

        if result.success:
            self.open_tickets[result.ticket] = {
                "symbol": decision.symbol,
                "direction": decision.action,
                "entry_price": result.fill_price,
                "sl_price": decision.sl_price,
                "tp_price": decision.tp_price,
                "tp2_price": decision.tp2_price,
                "tp1_hit": False,
                "lot_size": lot_size,
                "opened_at": datetime.now(timezone.utc).isoformat(),
            }
            self.total_trades += 1
            return {
                "success": True,
                "ticket": result.ticket,
                "message": f"{decision.action} {decision.symbol} @ {result.fill_price:.5f} "
                           f"Lots={lot_size:.2f} Magic={BRIDGE_MAGIC}",
                "fill_price": result.fill_price,
            }
        else:
            return {
                "success": False,
                "ticket": 0,
                "message": f"MT5 rejected: {result.comment} (retcode={result.retcode})",
                "fill_price": 0.0,
            }

    # ------------------------------------------------------------------
    # Close position
    # ------------------------------------------------------------------

    async def close_position(self, ticket: int, pnl: float = 0.0) -> bool:
        """Close a position and update stats."""
        info = self.open_tickets.pop(ticket, None)
        if info is None:
            return False

        result = await self.mt5.close_position(
            ticket=ticket,
            symbol=info["symbol"],
            lot_size=info["lot_size"],
        )

        # Update win/loss tracking
        if pnl >= 0:
            self.wins += 1
            self.consecutive_losses = 0
        else:
            self.losses += 1
            self.consecutive_losses += 1

        # Kill switch check
        if self.consecutive_losses >= BRIDGE_KILL_SWITCH_LOSSES:
            self._kill_switch = True
            print(
                f"[LIVE] KILL SWITCH ACTIVATED: {self.consecutive_losses} consecutive losses. "
                f"Call reset_kill_switch() after review.",
                flush=True,
            )

        return result

    # ------------------------------------------------------------------
    # Modify SL/TP
    # ------------------------------------------------------------------

    async def partial_close_tp1(self, ticket: int, tp1_price: float) -> bool:
        """Close 50% of position at TP1 and move SL to breakeven."""
        info = self.open_tickets.get(ticket)
        if info is None or info.get("tp1_hit"):
            return False

        # Partial close via MT5 (close half the lot size)
        half_lots = round(info["lot_size"] / 2, 2)
        result = await self.mt5.close_position(
            ticket=ticket,
            symbol=info["symbol"],
            lot_size=half_lots,
        )
        if result:
            info["tp1_hit"] = True
            info["lot_size"] = half_lots
            # Move SL to breakeven
            await self.modify_sl(ticket, info["entry_price"])
            print(f"[LIVE] TP1 partial close: {info['symbol']} {half_lots} lots @ {tp1_price}", flush=True)
        return result

    async def modify_sl(self, ticket: int, new_sl: float) -> bool:
        """Update stop loss on an open position (for trailing stops)."""
        from core.events import PositionUpdateEvent

        info = self.open_tickets.get(ticket)
        if info is None:
            return False

        update = PositionUpdateEvent(
            ticket=ticket,
            symbol=info["symbol"],
            new_sl=new_sl,
            new_tp=info["tp_price"],
        )
        result = await self.mt5.modify_position(update)
        if result:
            info["sl_price"] = new_sl
        return result

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Get current executor status."""
        return {
            "mode": "live",
            "magic": BRIDGE_MAGIC,
            "session_confirmed": self.session_confirmed,
            "kill_switch": self._kill_switch,
            "open_positions": len(self.open_tickets),
            "max_positions": self.max_positions,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "consecutive_losses": self.consecutive_losses,
        }


# ---------------------------------------------------------------------------
# CLI test (no MT5 needed — tests safety gates only)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    executor = LiveExecutor()

    # Test pre-flight without session confirmation
    ok, reason = executor.pre_flight_check()
    print(f"Pre-flight (no confirm): {ok} - {reason}")

    # Confirm session
    executor.confirm_session()
    ok, reason = executor.pre_flight_check()
    print(f"Pre-flight (confirmed): {ok} - {reason}")

    # Test kill switch
    executor.consecutive_losses = 3
    executor._kill_switch = True
    ok, reason = executor.pre_flight_check()
    print(f"Pre-flight (kill switch): {ok} - {reason}")

    # Reset
    executor.reset_kill_switch()
    ok, reason = executor.pre_flight_check()
    print(f"Pre-flight (reset): {ok} - {reason}")

    print(f"\nStatus: {json.dumps(executor.get_status(), indent=2)}")
