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

import sys
from dataclasses import dataclass
from typing import Any

from bridge.config import ensure_trading_ai_path, get_bridge_config

# Ensure trading-ai-v2 is importable
ensure_trading_ai_path()

from risk.ftmo import FTMORules, AccountState
from risk.sizing import SymbolSpec, calculate_lots
from core.types import Direction


# ---------------------------------------------------------------------------
# Default symbol specs (paper trading — no MT5 connection)
# ---------------------------------------------------------------------------

PAPER_SYMBOL_SPECS: dict[str, SymbolSpec] = {
    # Crypto (typical broker specs)
    "BTCUSD": SymbolSpec(name="BTCUSD", tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=100.0, volume_step=0.01),
    "ETHUSD": SymbolSpec(name="ETHUSD", tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=500.0, volume_step=0.01),
    "SOLUSD": SymbolSpec(name="SOLUSD", tick_size=0.001, tick_value=0.001, volume_min=0.1, volume_max=5000.0, volume_step=0.1),
    # Forex
    "EURUSD": SymbolSpec(name="EURUSD", tick_size=0.00001, tick_value=1.0, volume_min=0.01, volume_max=500.0, volume_step=0.01),
    "GBPUSD": SymbolSpec(name="GBPUSD", tick_size=0.00001, tick_value=1.0, volume_min=0.01, volume_max=500.0, volume_step=0.01),
    # Indices / Commodities
    "XAUUSD": SymbolSpec(name="XAUUSD", tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=100.0, volume_step=0.01),
    "US100":  SymbolSpec(name="US100", tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=100.0, volume_step=0.01),
    "US500":  SymbolSpec(name="US500", tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=100.0, volume_step=0.01),
}


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
        return calculate_lots(
            account_balance=balance,
            risk_pct=risk_pct,
            entry_price=entry_price,
            sl_price=sl_price,
            direction=dir_enum,
            spec=spec,
        )

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
        lot_size = self.get_lot_size(symbol, balance, adjusted_risk, entry_price, sl_price, direction)

        if lot_size <= 0:
            return False, 0.0, "Invalid lot size (check SL distance)"

        return True, lot_size, f"Approved: {lot_size:.4f} lots (risk={adjusted_risk:.3%}, proximity={multiplier:.2f})"


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
