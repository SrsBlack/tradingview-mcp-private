"""
Live Executor — wraps trading-ai-v2's MT5Executor for real trade execution.

Safety gates:
- Magic number 99002 for bridge signals
- Max 8 positions from bridge (configurable via BRIDGE_MAX_POSITIONS); risk-on/risk-off sub-cap of 4 enforced in risk_bridge.py
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

from bridge.config import ensure_trading_ai_path, tv_to_ftmo_symbol
from bridge.decision_types import TradeDecision

# Ensure trading-ai-v2 is importable
ensure_trading_ai_path()

from core.types import Direction, Symbol, EngineName
from core.events import ExecutionEvent


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BRIDGE_MAGIC = 99002
# 2026-04-27: bumped 5 -> 8 for data-collection mode on FTMO demo. Goal is
# fast accumulation of trade signals across signal classes for WR-by-class
# audit, not capital preservation. The 4% daily-loss kill-switch +
# loss-cooldowns still protect the demo. Revert to 5 once signal categorisation
# is dialled in.
BRIDGE_MAX_POSITIONS = 8
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

        # Map TradingView symbol to FTMO/MT5 broker symbol
        ftmo_symbol = tv_to_ftmo_symbol(decision.symbol)

        # Normalize volume to broker's step size
        normalized_lots = self._normalize_volume(ftmo_symbol, lot_size)
        if normalized_lots <= 0:
            return {
                "success": False, "ticket": 0,
                "message": f"Volume {lot_size:.4f} below minimum for {ftmo_symbol}",
                "fill_price": 0.0,
            }

        # Build ExecutionEvent
        direction = Direction.BULLISH if decision.action == "BUY" else Direction.BEARISH

        # For two-tier TP trades (tp2_price > 0), do NOT set TP on MT5.
        # MT5's server-side TP would close 100% of the position at TP1,
        # preventing the bridge from doing a 50% partial close + TP2 runner.
        # SL stays on MT5 as crash protection; TP is managed by the bridge.
        mt5_tp = 0.0 if decision.tp2_price > 0 else decision.tp_price

        event = ExecutionEvent(
            symbol=ftmo_symbol,
            direction=direction,
            lot_size=normalized_lots,
            entry_price=decision.entry_price,
            sl_price=decision.sl_price,
            tp_price=mt5_tp,
            ticket=0,
            signal_id=f"bridge_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            engine=EngineName.ICT,
            strategy_name=BRIDGE_STRATEGY_NAME,
        )

        # Submit to MT5
        result = await self.mt5.submit(event)

        if result.success:
            self.open_tickets[result.ticket] = {
                "symbol": ftmo_symbol,  # MT5/FTMO-side symbol for modify/close calls
                "tv_symbol": decision.symbol,
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

            # Verify SL/TP are actually set on the MT5 position
            await self._verify_sl_tp(result.ticket, decision, result.fill_price)

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
    # Post-trade verification
    # ------------------------------------------------------------------

    async def _verify_sl_tp(
        self, ticket: int, decision: TradeDecision, fill_price: float
    ) -> None:
        """Verify MT5 actually has SL/TP set after order placement."""
        try:
            import MetaTrader5 as mt5
            pos = mt5.positions_get(ticket=ticket)
            if pos is None or len(pos) == 0:
                print(f"[SL_VERIFY] WARNING: Cannot find MT5 position #{ticket} after fill!", flush=True)
                return

            mt5_pos = pos[0]
            mt5_sl = mt5_pos.sl
            mt5_tp = mt5_pos.tp

            # Check SL is set
            if mt5_sl == 0:
                print(
                    f"[SL_VERIFY] CRITICAL: #{ticket} has NO SL set on MT5! "
                    f"Expected SL={decision.sl_price:.5f} — attempting to set now",
                    flush=True,
                )
                await self.modify_sl(ticket, decision.sl_price)

            elif abs(mt5_sl - decision.sl_price) > decision.sl_price * 0.001:
                print(
                    f"[SL_VERIFY] WARNING: #{ticket} SL mismatch — "
                    f"MT5={mt5_sl:.5f} vs intended={decision.sl_price:.5f}",
                    flush=True,
                )

            # Check SL distance isn't dangerously tight relative to spread
            info = mt5.symbol_info(mt5_pos.symbol)
            if info and info.spread > 0:
                spread_price = info.spread * info.point
                sl_distance = abs(fill_price - decision.sl_price)
                if sl_distance < spread_price * 3:
                    print(
                        f"[SL_VERIFY] WARNING: #{ticket} SL distance ({sl_distance:.5f}) "
                        f"is < 3x spread ({spread_price:.5f}) — high risk of immediate SL hit",
                        flush=True,
                    )

            # Check TP is set (expected to be 0 for two-tier TP trades — bridge manages TP)
            if mt5_tp == 0 and decision.tp2_price <= 0:
                print(
                    f"[SL_VERIFY] WARNING: #{ticket} has NO TP set on MT5! "
                    f"Expected TP={decision.tp_price:.5f}",
                    flush=True,
                )

        except ImportError:
            pass
        except Exception as e:
            print(f"[SL_VERIFY] Error verifying #{ticket}: {e}", flush=True)

    # ------------------------------------------------------------------
    # Close position
    # ------------------------------------------------------------------

    async def close_position(self, ticket: int, pnl: float = 0.0) -> bool:
        """Close a position and update stats.

        IMPORTANT: open_tickets is only removed AFTER a confirmed MT5 close
        to prevent ghost positions (position open on broker but invisible to bridge).
        """
        info = self.open_tickets.get(ticket)
        if info is None:
            return False

        result = await self.mt5.close_position(
            ticket=ticket,
            symbol=info["symbol"],
            lot_size=info["lot_size"],
        )

        if result:
            # Only remove after confirmed close
            self.open_tickets.pop(ticket, None)

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
        else:
            # MT5 close failed — check if position still exists on broker
            try:
                import MetaTrader5 as mt5
                pos = mt5.positions_get(ticket=ticket)
                if not pos:
                    # Position gone broker-side (server SL/TP fired) — safe to remove
                    self.open_tickets.pop(ticket, None)
                    print(f"  [CLOSE] #{ticket} already closed broker-side — removed from tracking", flush=True)
                    return True
                else:
                    print(f"  [CLOSE] WARNING: #{ticket} close failed but still open on MT5!", flush=True)
            except Exception:
                pass

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
        """Update stop loss on an open position (for trailing stops).

        Uses direct mt5.order_send() path (not core.events) because the
        PositionUpdateEvent layer was returning False silently during
        live trailing — a symptom that direct-path is more reliable
        for a single SLTP modify with no state machine around it.

        Logging: emits [MT5_SL] for every attempt (success or fail) with enough
        detail to diagnose the ticket #100040-style "silent drift" where the
        bridge thought it was trailing but MT5 still held the original SL.
        """
        try:
            import MetaTrader5 as mt5
        except ImportError:
            print(f"  [MT5_SL] #{ticket} FAIL: MetaTrader5 not available", flush=True)
            return False

        info = self.open_tickets.get(ticket)
        old_sl = info.get("sl_price") if info else None

        # Always query broker first — this is the authoritative source.
        # Don't gate on open_tickets, because post-restart open_tickets can be
        # empty while MT5 still holds our positions. Previously this was a
        # silent failure path that masked trailing SL desync.
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            err = mt5.last_error()
            print(
                f"  [MT5_SL] #{ticket} FAIL: position not found on broker "
                f"(may have just closed); last_error={err}",
                flush=True,
            )
            return False

        pos = positions[0]
        # Prefer broker's TP if info is missing (restart case)
        tp = (info.get("tp_price") if info else 0.0) or pos.tp or 0.0

        # No-op guard — avoid churn if SL already at target (within 1 point)
        if info is not None and abs(pos.sl - float(new_sl)) < 1e-6:
            return True

        if info is None:
            # Adopt: register the broker's view into open_tickets so future
            # modify/partial calls don't hit this same silent-fail path.
            self.open_tickets[ticket] = {
                "symbol":     pos.symbol,
                "entry_price": pos.price_open,
                "sl_price":   pos.sl,
                "tp_price":   pos.tp,
                "lot_size":   pos.volume,
                "tp1_hit":    False,
                "direction":  "BUY" if pos.type == 0 else "SELL",
                "adopted":    True,
            }
            print(
                f"  [MT5_SL] #{ticket} adopted from broker "
                f"(sym={pos.symbol} sl={pos.sl} tp={pos.tp} vol={pos.volume})",
                flush=True,
            )
            info = self.open_tickets[ticket]
            old_sl = pos.sl

        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol":   pos.symbol,      # use authoritative broker symbol
            "sl":       float(new_sl),
            "tp":       float(tp),
        }
        result = mt5.order_send(request)
        if result is None:
            err = mt5.last_error()
            print(
                f"  [MT5_SL] #{ticket} {pos.symbol} FAIL: order_send returned None "
                f"(old_sl={old_sl} -> new_sl={new_sl}); last_error={err}",
                flush=True,
            )
            return False

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            info["sl_price"] = new_sl
            print(
                f"  [MT5_SL] #{ticket} {pos.symbol} OK: {old_sl} -> {new_sl}",
                flush=True,
            )
            return True

        print(
            f"  [MT5_SL] #{ticket} {pos.symbol} REJECT: retcode={result.retcode} "
            f"comment={result.comment!r} old_sl={old_sl} new_sl={new_sl} tp={tp}",
            flush=True,
        )
        return False

    # ------------------------------------------------------------------
    # Volume normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_volume(symbol: str, lot_size: float) -> float:
        """Round lot_size to the broker's volume_step and clamp to min/max."""
        try:
            import MetaTrader5 as mt5
            info = mt5.symbol_info(symbol)
            if info is None:
                return round(lot_size, 2)
            vol_min = info.volume_min
            vol_max = info.volume_max
            vol_step = info.volume_step
            if vol_step <= 0:
                vol_step = 0.01
            # Round to nearest step
            steps = round(lot_size / vol_step)
            normalized = steps * vol_step
            # Clamp
            normalized = max(vol_min, min(vol_max, normalized))
            # Round to avoid floating point artifacts
            decimals = max(0, len(str(vol_step).rstrip('0').split('.')[-1]))
            return round(normalized, decimals)
        except Exception:
            return round(lot_size, 2)

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
