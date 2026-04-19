"""
Risk Bridge — wires trading-ai-v2's FTMO risk management into the bridge pipeline.

Gates every trade through RiskManager.evaluate_signal() before execution.
Maintains AccountState from paper or live P&L.

Usage:
    from bridge.risk_bridge import RiskBridge
    bridge = RiskBridge()
    approved, lot_size, reason = bridge.check_trade(decision, balance_info)
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from typing import Any

from bridge.config import ensure_trading_ai_path, get_bridge_config, SMT_PAIRS, tv_to_ftmo_symbol

# Ensure trading-ai-v2 is importable
ensure_trading_ai_path()

from risk.ftmo import FTMORules, AccountState
from risk.sizing import SymbolSpec, calculate_lots
from core.types import Direction


# ---------------------------------------------------------------------------
# Default symbol specs (paper trading — no MT5 connection)
# ---------------------------------------------------------------------------

PAPER_SYMBOL_SPECS: dict[str, SymbolSpec] = {
    # Crypto — verified from FTMO MT5 on 2026-04-18
    "BTCUSD": SymbolSpec(name="BTCUSD", tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=5.0, volume_step=0.01),
    "ETHUSD": SymbolSpec(name="ETHUSD", tick_size=0.01, tick_value=0.1, volume_min=0.01, volume_max=5.0, volume_step=0.01),
    "SOLUSD": SymbolSpec(name="SOLUSD", tick_size=0.01, tick_value=1.0, volume_min=0.01, volume_max=5.0, volume_step=0.01),
    "DOGEUSD": SymbolSpec(name="DOGEUSD", tick_size=0.00001, tick_value=1.0, volume_min=0.01, volume_max=1.0, volume_step=0.01),
    # Forex — verified from FTMO MT5
    "EURUSD": SymbolSpec(name="EURUSD", tick_size=0.00001, tick_value=1.0, volume_min=0.01, volume_max=50.0, volume_step=0.01),
    "GBPUSD": SymbolSpec(name="GBPUSD", tick_size=0.00001, tick_value=1.0, volume_min=0.01, volume_max=50.0, volume_step=0.01),
    "USDJPY": SymbolSpec(name="USDJPY", tick_size=0.001, tick_value=0.63, volume_min=0.01, volume_max=50.0, volume_step=0.01),
    "AUDUSD": SymbolSpec(name="AUDUSD", tick_size=0.00001, tick_value=1.0, volume_min=0.01, volume_max=50.0, volume_step=0.01),
    "NZDUSD": SymbolSpec(name="NZDUSD", tick_size=0.00001, tick_value=1.0, volume_min=0.01, volume_max=50.0, volume_step=0.01),
    # Gold / Silver / Oil — verified from FTMO MT5
    "XAUUSD": SymbolSpec(name="XAUUSD", tick_size=0.01, tick_value=1.0, volume_min=0.01, volume_max=100.0, volume_step=0.01),
    "XAGUSD": SymbolSpec(name="XAGUSD", tick_size=0.001, tick_value=5.0, volume_min=0.01, volume_max=100.0, volume_step=0.01),
    "UKOIL":  SymbolSpec(name="UKOIL",  tick_size=0.01, tick_value=0.01, volume_min=0.1,  volume_max=500.0, volume_step=0.1),
    # Indices — verified from FTMO MT5 (.cash suffix)
    "US30":   SymbolSpec(name="US30",   tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=1000.0, volume_step=0.01),
    "US100":  SymbolSpec(name="US100",  tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=1000.0, volume_step=0.01),
    "US500":  SymbolSpec(name="US500",  tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=1000.0, volume_step=0.01),
    "GER40":  SymbolSpec(name="GER40",  tick_size=0.01, tick_value=0.01176, volume_min=0.01, volume_max=1000.0, volume_step=0.01),
}

# Hard max lot size per symbol — absolute safety cap regardless of risk calculation
HARD_MAX_LOTS: dict[str, float] = {
    "ETHUSD": 1.0, "BTCUSD": 0.5, "SOLUSD": 5.0, "DOGEUSD": 50.0,
    "EURUSD": 5.0, "GBPUSD": 5.0, "USDJPY": 5.0, "AUDUSD": 5.0, "NZDUSD": 5.0,
    "XAUUSD": 2.0, "XAGUSD": 2.0,
    "US30.cash": 2.0, "US100.cash": 2.0, "US500.cash": 5.0, "UKOIL.cash": 5.0,
    "US30": 2.0, "US100": 2.0, "US500": 5.0, "UKOIL": 5.0,
    "GER40.cash": 5.0, "GER40": 5.0,
}


def calculate_pnl(symbol: str, entry_price: float, exit_price: float, lot_size: float, direction: str) -> float:
    """Calculate P&L using proper tick_value conversion."""
    # Strip exchange prefix (e.g. "BITSTAMP:BTCUSD" -> "BTCUSD")
    symbol = symbol.split(":")[-1] if ":" in symbol else symbol
    spec = PAPER_SYMBOL_SPECS.get(symbol)
    if not spec:
        # fallback to raw calculation for unknown symbols — may be inaccurate
        print(f"  [WARN] calculate_pnl: no SymbolSpec for {symbol}, using raw delta*lots", flush=True)
        delta = exit_price - entry_price
        if direction.upper() == "SELL":
            delta = -delta
        return delta * lot_size
    delta = exit_price - entry_price
    if direction.upper() == "SELL":
        delta = -delta
    ticks = delta / spec.tick_size
    return ticks * spec.tick_value * lot_size


def _clamp_to_hard_max(symbol: str, lot_size: float) -> float:
    """Clamp lot size to HARD_MAX_LOTS if defined for this symbol."""
    base = symbol.split(":")[-1] if ":" in symbol else symbol
    hard_max = HARD_MAX_LOTS.get(base)
    if hard_max is not None and lot_size > hard_max:
        print(f"  [{base}] HARD_MAX clamp: {lot_size:.4f} -> {hard_max:.2f} lots", flush=True)
        return hard_max
    return lot_size


# ---------------------------------------------------------------------------
# Risk Bridge
# ---------------------------------------------------------------------------

class RiskBridge:
    """
    Bridge between the paper/live executor and trading-ai-v2 FTMO risk management.

    Provides:
    - FTMO compliance checks (daily loss, total drawdown)
    - Position sizing via calculate_lots()
    - Drawdown warnings and proximity multiplier
    """

    def __init__(self):
        self.ftmo = FTMORules()
        self.config = get_bridge_config()

    def build_account_state(
        self,
        balance: float,
        initial_balance: float,
        daily_pnl: float,
        peak_balance: float,
    ) -> AccountState:
        """Build an AccountState from executor state."""
        return AccountState(
            balance=balance,
            initial_balance=initial_balance,
            daily_pnl=daily_pnl,
            peak_balance=peak_balance,
        )

    def can_trade(
        self,
        balance: float,
        initial_balance: float,
        daily_pnl: float,
        peak_balance: float,
    ) -> tuple[bool, str]:
        """Check FTMO limits using trading-ai-v2's FTMORules."""
        state = self.build_account_state(balance, initial_balance, daily_pnl, peak_balance)
        return self.ftmo.can_trade(state)

    def get_lot_size(
        self,
        symbol: str,
        balance: float,
        risk_pct: float,
        entry_price: float,
        sl_price: float,
        direction: str,
    ) -> float:
        """
        Calculate proper lot size using trading-ai-v2's position sizing.

        Args:
            symbol: Trading symbol
            balance: Current account balance
            risk_pct: Risk percentage (e.g., 0.01 = 1%)
            entry_price: Entry price
            sl_price: Stop loss price
            direction: "BUY" or "SELL"

        Returns:
            Lot size (0.0 if invalid)
        """
        spec = PAPER_SYMBOL_SPECS.get(symbol)
        if spec is None:
            # Fallback: calculate manually
            risk_amount = balance * risk_pct
            risk_dist = abs(entry_price - sl_price)
            if risk_dist <= 0:
                return 0.0
            return round(risk_amount / risk_dist, 4)

        dir_enum = Direction.BULLISH if direction == "BUY" else Direction.BEARISH
        lots = calculate_lots(
            account_balance=balance,
            risk_pct=risk_pct,
            entry_price=entry_price,
            sl_price=sl_price,
            direction=dir_enum,
            spec=spec,
        )
        return _clamp_to_hard_max(symbol, lots)

    def get_lot_size_live(
        self,
        symbol: str,
        balance: float,
        risk_pct: float,
        entry_price: float,
        sl_price: float,
        direction: str,
    ) -> float:
        """Calculate lot size using LIVE MT5 symbol specs (most accurate)."""
        try:
            import MetaTrader5 as mt5
            ftmo_sym = tv_to_ftmo_symbol(symbol)
            info = mt5.symbol_info(ftmo_sym)
            if info and info.trade_tick_value > 0:
                spec = SymbolSpec(
                    name=ftmo_sym,
                    tick_size=info.trade_tick_size,
                    tick_value=info.trade_tick_value,
                    volume_min=info.volume_min,
                    volume_max=info.volume_max,
                    volume_step=info.volume_step,
                )
                dir_enum = Direction.BULLISH if direction == "BUY" else Direction.BEARISH
                lots = calculate_lots(
                    account_balance=balance,
                    risk_pct=risk_pct,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    direction=dir_enum,
                    spec=spec,
                )
                return _clamp_to_hard_max(symbol, lots)
        except ImportError:
            pass
        except Exception as e:
            print(f"  [WARN] MT5 spec lookup failed for {symbol}: {e}", flush=True)
        # Fallback to paper specs
        return self.get_lot_size(symbol, balance, risk_pct, entry_price, sl_price, direction)

    def get_proximity_multiplier(
        self,
        balance: float,
        initial_balance: float,
        daily_pnl: float,
        peak_balance: float,
    ) -> float:
        """
        Get FTMO proximity multiplier (0.0-1.0).
        Reduces position size as drawdown limits approach.
        """
        state = self.build_account_state(balance, initial_balance, daily_pnl, peak_balance)
        return self.ftmo.proximity_multiplier(state)

    def get_headroom(
        self,
        balance: float,
        initial_balance: float,
        daily_pnl: float,
        peak_balance: float,
    ) -> dict[str, float]:
        """Get remaining headroom before FTMO limits."""
        state = self.build_account_state(balance, initial_balance, daily_pnl, peak_balance)
        return {
            "daily_headroom_pct": self.ftmo.daily_headroom_pct(state),
            "total_headroom_pct": self.ftmo.total_headroom_pct(state),
            "proximity_multiplier": self.ftmo.proximity_multiplier(state),
            "daily_pnl_pct": state.daily_pnl_pct,
            "total_drawdown_pct": state.total_drawdown_pct,
        }

    def check_trade(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        sl_price: float,
        risk_pct: float,
        balance: float,
        initial_balance: float,
        daily_pnl: float,
        peak_balance: float,
    ) -> tuple[bool, float, str]:
        """
        Full risk check for a proposed trade.

        Returns:
            (approved, lot_size, reason)
        """
        # FTMO check
        can, reason = self.can_trade(balance, initial_balance, daily_pnl, peak_balance)
        if not can:
            return False, 0.0, f"FTMO: {reason}"

        # Proximity multiplier reduces size near limits
        multiplier = self.get_proximity_multiplier(balance, initial_balance, daily_pnl, peak_balance)
        if multiplier <= 0.0:
            return False, 0.0, "FTMO proximity multiplier is 0 (at limit)"

        # Calculate lot size
        adjusted_risk = risk_pct * multiplier
        lot_size = self.get_lot_size_live(symbol, balance, adjusted_risk, entry_price, sl_price, direction)

        if lot_size <= 0:
            return False, 0.0, "Invalid lot size (check SL distance)"

        # Notional exposure cap: max 20% of balance per position
        max_notional_pct = 0.20
        notional = lot_size * entry_price
        max_notional = balance * max_notional_pct
        if notional > max_notional:
            capped_lots = max_notional / entry_price
            # Round down to volume_step
            spec = PAPER_SYMBOL_SPECS.get(symbol)
            step = spec.volume_step if spec else 0.01
            capped_lots = math.floor(capped_lots / step) * step
            capped_lots = max(step, capped_lots)
            print(f"  [{symbol}] Notional cap: {lot_size:.2f} lots (${notional:,.0f}) "
                  f"exceeds {max_notional_pct:.0%} of ${balance:,.0f} — "
                  f"capped to {capped_lots:.2f} lots", flush=True)
            lot_size = capped_lots

        # Hard max lot safety clamp
        lot_size = _clamp_to_hard_max(symbol, lot_size)

        return True, lot_size, f"Approved: {lot_size:.4f} lots (risk={adjusted_risk:.3%}, proximity={multiplier:.2f})"

    def check_correlation(
        self,
        new_symbol: str,
        new_direction: str,
        open_positions: dict,
    ) -> tuple[bool, str]:
        """
        Check if a new trade is too correlated with existing open positions.

        Uses SMT_PAIRS to identify correlated instruments. Blocks if:
        - Same symbol already open in same direction
        - Correlated pair (e.g., US500 + US100) both open in same direction

        Returns:
            (ok, reason) — ok=True if trade is allowed, False if blocked.
        """
        if not open_positions:
            return True, ""

        new_base = new_symbol.split(":")[-1]

        for pos in open_positions.values():
            pos_base = pos.symbol.split(":")[-1]

            # Same symbol, same direction — already exposed
            if pos_base == new_base and pos.direction == new_direction:
                return False, f"Already have {pos.direction} on {pos_base} (#{pos.ticket})"

            # Check SMT correlation — same direction on correlated pair
            smt_pair = SMT_PAIRS.get(new_base)
            if smt_pair and smt_pair == pos_base and pos.direction == new_direction:
                return False, (
                    f"Correlated: {new_base} + {pos_base} both {new_direction} "
                    f"(SMT pair — concentrated risk)"
                )

            # Time-based correlation: block same-direction crypto trades within 60 min
            crypto_symbols = {"BTCUSD", "ETHUSD", "SOLUSD", "DOGEUSD"}
            if new_base in crypto_symbols and pos_base in crypto_symbols:
                if pos.direction == new_direction:
                    # Check if existing position was opened recently (within 60 min)
                    opened_at = getattr(pos, "opened_at", None)
                    if opened_at:
                        from datetime import datetime, timezone, timedelta
                        try:
                            if isinstance(opened_at, str):
                                opened_dt = datetime.fromisoformat(opened_at)
                            else:
                                opened_dt = opened_at
                            age = datetime.now(timezone.utc) - opened_dt
                            if age < timedelta(minutes=60):
                                return False, (
                                    f"Crypto correlation: {new_base} + {pos_base} both {new_direction} "
                                    f"within 60min (opened {age.total_seconds()/60:.0f}m ago)"
                                )
                        except (ValueError, TypeError):
                            pass

        return True, ""


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    bridge = RiskBridge()

    # Test FTMO check with healthy account
    can, reason = bridge.can_trade(
        balance=10000, initial_balance=10000, daily_pnl=0, peak_balance=10000
    )
    print(f"Can trade (healthy): {can} - {reason}")

    # Test FTMO check near daily limit
    can, reason = bridge.can_trade(
        balance=9500, initial_balance=10000, daily_pnl=-500, peak_balance=10000
    )
    print(f"Can trade (-5% daily): {can} - {reason}")

    # Test lot sizing
    lot = bridge.get_lot_size("BTCUSD", 10000, 0.01, 69000.0, 68500.0, "BUY")
    print(f"BTCUSD lot size (1% risk, 500pt SL): {lot}")

    # Test full check
    approved, lots, msg = bridge.check_trade(
        symbol="BTCUSD", direction="BUY",
        entry_price=69000.0, sl_price=68500.0, risk_pct=0.01,
        balance=10000, initial_balance=10000, daily_pnl=0, peak_balance=10000,
    )
    print(f"Full check: approved={approved}, lots={lots}, msg={msg}")

    # Test headroom
    headroom = bridge.get_headroom(9800, 10000, -200, 10000)
    print(f"Headroom: {json.dumps(headroom, indent=2)}")
