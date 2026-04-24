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
    "DAX":    SymbolSpec(name="GER40",  tick_size=0.01, tick_value=0.01176, volume_min=0.01, volume_max=1000.0, volume_step=0.01),
    # .cash suffixed aliases (FTMO broker symbols)
    "US30.cash":  SymbolSpec(name="US30",  tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=1000.0, volume_step=0.01),
    "US100.cash": SymbolSpec(name="US100", tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=1000.0, volume_step=0.01),
    "US500.cash": SymbolSpec(name="US500", tick_size=0.01, tick_value=0.01, volume_min=0.01, volume_max=1000.0, volume_step=0.01),
    "GER40.cash": SymbolSpec(name="GER40", tick_size=0.01, tick_value=0.01176, volume_min=0.01, volume_max=1000.0, volume_step=0.01),
    "UKOIL.cash": SymbolSpec(name="UKOIL", tick_size=0.01, tick_value=0.01, volume_min=0.1,  volume_max=500.0, volume_step=0.1),
}

# Hard max lot size per symbol — absolute safety cap regardless of risk calculation
HARD_MAX_LOTS: dict[str, float] = {
    "ETHUSD": 1.0, "BTCUSD": 0.5, "SOLUSD": 5.0, "DOGEUSD": 50.0,
    "EURUSD": 2.0, "GBPUSD": 2.0, "USDJPY": 2.0, "AUDUSD": 2.0, "NZDUSD": 2.0,
    "XAUUSD": 0.5, "XAGUSD": 2.0,
    "US30.cash": 2.0, "US100.cash": 2.0, "US500.cash": 3.0, "UKOIL.cash": 3.0,
    "US30": 2.0, "US100": 2.0, "US500": 3.0, "UKOIL": 3.0,
    "GER40.cash": 3.0, "GER40": 3.0,
}


# Portfolio-level correlation caps — assets that move together in risk-on / risk-off regimes
RISK_ON_ASSETS: frozenset[str] = frozenset({
    "BTCUSD", "ETHUSD", "SOLUSD", "DOGEUSD",
    "US500", "US100", "DAX", "GER40",
    "GBPUSD", "AUDUSD", "NZDUSD",
})
RISK_OFF_ASSETS: frozenset[str] = frozenset({
    "XAUUSD", "XAGUSD", "USDJPY", "UKOIL",
})

MAX_SAME_CLASS_POSITIONS = 4

# -----------------------------------------------------------------------------
# DXY exposure model — prevents stacking correlated forex trades
# -----------------------------------------------------------------------------
# Each entry is the DXY-exposure coefficient when BUYING the symbol.
# Positive = buying the pair shorts DXY (e.g. BUY EURUSD = long EUR / short USD).
# Negative = buying the pair longs DXY (e.g. BUY USDJPY = long USD / short JPY).
# JPY crosses are near-zero DXY because USD doesn't appear in the pair.
# Gold/silver are partial short-DXY (inverse USD correlation, not 1:1).
#
# For SELL trades, flip the sign (SELL EURUSD = long DXY = -1.0 coefficient).
#
# Sum of |exposures| across open positions is the concentration number we gate.
DXY_EXPOSURE_ON_BUY: dict[str, float] = {
    # USD-quote forex majors (USD is the quote currency)
    "EURUSD": +1.00,   # buy EUR, sell USD
    "GBPUSD": +0.85,   # cable, high correlation with EUR
    "AUDUSD": +0.75,   # commodity-linked but still DXY-sensitive
    "NZDUSD": +0.70,
    # USD-base forex majors (USD is the base currency)
    "USDJPY": -1.00,   # buy USD, sell JPY
    "USDCAD": -0.85,   # oil also plays, but mostly USD-driven
    "USDCHF": -0.90,   # (not yet in watchlist)
    # JPY crosses (USD not in pair — no direct DXY exposure)
    "EURJPY": +0.10,   # very mild short-DXY via EUR leg
    "GBPJPY": +0.10,
    # Metals — partial inverse-DXY correlation
    "XAUUSD": +0.70,
    "XAGUSD": +0.60,
    # Indices — risk-on tends to correlate with USD weakness
    "US500":  +0.30,
    "US100":  +0.30,
    "GER40":  +0.35,
    "YM1!":   +0.30,
    # Oil — commodity/DXY inverse
    "UKOIL":  +0.50,
    # Crypto — weak DXY correlation for BTC, near-zero for alts
    "BTCUSD": +0.20,
    "ETHUSD": +0.15,
    "SOLUSD": +0.10,
    "DOGEUSD": +0.05,
}

# Gate threshold: sum of |DXY exposure| across open positions must stay below this
# before a new forex trade is allowed. 2.0 means you can have EURUSD + GBPUSD but
# adding AUDUSD (would push sum to ~2.6) gets blocked.
MAX_DXY_EXPOSURE = 2.0


def _signed_dxy_exposure(symbol_base: str, direction: str) -> float:
    """Signed DXY exposure for a position. BUY uses the table directly,
    SELL flips the sign. Unknown symbols return 0 (not gated)."""
    coeff = DXY_EXPOSURE_ON_BUY.get(symbol_base, 0.0)
    return coeff if direction.upper() == "BUY" else -coeff


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

        # Hard max lot safety clamp
        lot_size = _clamp_to_hard_max(symbol, lot_size)

        # Per-trade USD risk cap: never risk more than $750 on a single trade
        MAX_RISK_USD = 750.0
        sl_distance = abs(entry_price - sl_price)
        if sl_distance > 0:
            ftmo_sym = tv_to_ftmo_symbol(symbol)
            try:
                import MetaTrader5 as mt5
                info = mt5.symbol_info(ftmo_sym)
                if info and info.trade_tick_size > 0:
                    ticks = sl_distance / info.trade_tick_size
                    risk_usd = ticks * info.trade_tick_value * lot_size
                    if risk_usd > MAX_RISK_USD:
                        capped_lots = MAX_RISK_USD / (ticks * info.trade_tick_value)
                        step = info.volume_step if info.volume_step > 0 else 0.01
                        capped_lots = math.floor(capped_lots / step) * step
                        capped_lots = max(step, capped_lots)
                        print(
                            f"  [{symbol}] USD risk cap: ${risk_usd:,.0f} exceeds ${MAX_RISK_USD:.0f} — "
                            f"lots {lot_size:.2f} -> {capped_lots:.2f}",
                            flush=True,
                        )
                        lot_size = capped_lots
            except ImportError:
                pass
            except Exception as e:
                print(f"  [{symbol}] USD risk cap check warning: {e}", flush=True)

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

        # MT5 direct check — catch positions missed by internal tracking after restarts
        try:
            import MetaTrader5 as mt5
            from bridge.config import tv_to_ftmo_symbol
            ftmo_sym = tv_to_ftmo_symbol(new_symbol)
            mt5_positions = mt5.positions_get(symbol=ftmo_sym)
            if mt5_positions:
                for p in mt5_positions:
                    if p.magic == 99002:
                        mt5_dir = "BUY" if p.type == 0 else "SELL"
                        return False, f"MT5 already has {mt5_dir} on {ftmo_sym} (#{p.ticket}) — no duplicate entries"
        except Exception:
            pass  # Fall through to internal check if MT5 query fails

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

            # Contradiction gate: opposite directions on highly-correlated crypto pairs.
            # BTC + ETH move together ~0.85 correlation. Opposite directions = one leg
            # is a guaranteed loser unless there's a specific SMT divergence thesis.
            # Block unless the new signal explicitly has SMT confluence (handled upstream).
            btc_eth = {"BTCUSD", "ETHUSD"}
            if new_base in btc_eth and pos_base in btc_eth and new_base != pos_base:
                if pos.direction != new_direction:
                    return False, (
                        f"Contradiction: {new_direction} {new_base} vs {pos.direction} {pos_base} "
                        f"(BTC/ETH correlation ~0.85 — opposite directions guarantee one loser)"
                    )

        # Portfolio-level correlation cap: limit same-direction same-class positions.
        # A BUY on a risk-on asset or SELL on a risk-off asset both express the same
        # macro bet (markets up, USD down). Cap at MAX_SAME_CLASS_POSITIONS to avoid
        # having the whole account levered in one regime direction.
        def _is_risk_on_trade(symbol_base: str, direction: str) -> bool:
            if symbol_base in RISK_ON_ASSETS and direction == "BUY":
                return True
            if symbol_base in RISK_OFF_ASSETS and direction == "SELL":
                return True
            return False

        def _is_risk_off_trade(symbol_base: str, direction: str) -> bool:
            if symbol_base in RISK_OFF_ASSETS and direction == "BUY":
                return True
            if symbol_base in RISK_ON_ASSETS and direction == "SELL":
                return True
            return False

        new_is_risk_on = _is_risk_on_trade(new_base, new_direction)
        new_is_risk_off = _is_risk_off_trade(new_base, new_direction)

        if new_is_risk_on or new_is_risk_off:
            risk_on_count = 0
            risk_off_count = 0
            for pos in open_positions.values():
                pos_base = pos.symbol.split(":")[-1]
                if _is_risk_on_trade(pos_base, pos.direction):
                    risk_on_count += 1
                if _is_risk_off_trade(pos_base, pos.direction):
                    risk_off_count += 1

            if new_is_risk_on and risk_on_count >= MAX_SAME_CLASS_POSITIONS:
                return False, (
                    f"Portfolio cap: already {risk_on_count} risk-on positions "
                    f"(max {MAX_SAME_CLASS_POSITIONS}) — new {new_direction} {new_base} blocked"
                )
            if new_is_risk_off and risk_off_count >= MAX_SAME_CLASS_POSITIONS:
                return False, (
                    f"Portfolio cap: already {risk_off_count} risk-off positions "
                    f"(max {MAX_SAME_CLASS_POSITIONS}) — new {new_direction} {new_base} blocked"
                )

        # -- DXY exposure gate ------------------------------------------------
        # Prevent stacking correlated forex trades where multiple pairs are really
        # one thesis on DXY (USD up or USD down). Sums signed DXY exposure across
        # open positions; blocks a new trade if adding it pushes |net| above the
        # threshold AND reinforces the existing bias (does not block hedges).
        new_dxy = _signed_dxy_exposure(new_base, new_direction)
        if new_dxy != 0.0:
            current_dxy = 0.0
            contributors: list[str] = []
            for pos in open_positions.values():
                pos_base = pos.symbol.split(":")[-1]
                pos_dxy = _signed_dxy_exposure(pos_base, pos.direction)
                if pos_dxy != 0.0:
                    current_dxy += pos_dxy
                    contributors.append(f"{pos.direction} {pos_base}({pos_dxy:+.2f})")

            projected = current_dxy + new_dxy
            # Only block if (a) projected magnitude exceeds cap AND (b) the new
            # trade is in the SAME direction as the existing bias — i.e. it's
            # adding to concentration, not hedging it.
            reinforcing = (current_dxy * new_dxy) > 0
            if abs(projected) > MAX_DXY_EXPOSURE and reinforcing:
                sign = "short-DXY (USD bearish)" if projected > 0 else "long-DXY (USD bullish)"
                return False, (
                    f"DXY exposure cap: adding {new_direction} {new_base}({new_dxy:+.2f}) "
                    f"would push net DXY from {current_dxy:+.2f} to {projected:+.2f} "
                    f"(cap ±{MAX_DXY_EXPOSURE}, {sign}). "
                    f"Open: {', '.join(contributors) if contributors else 'none'}"
                )

        return True, ""


# ---------------------------------------------------------------------------
# Scaled entry helpers (module-level, not method — no RiskBridge state needed)
# ---------------------------------------------------------------------------

def calculate_scaled_entries(
    entry_price: float,
    sl_price: float,
    direction: str,
    fvg_entry_price: float = 0.0,
    num_entries: int = 3,
) -> list[tuple[float, float]]:
    """Calculate scaled entry levels across the FVG/OB zone.

    ICT methodology: Instead of one market order, split into 2-3 entries
    across the FVG zone for better average price.

    Args:
        entry_price: Claude's suggested entry (usually FVG CE)
        sl_price: Stop loss price
        direction: "BUY" or "SELL"
        fvg_entry_price: FVG CE price (if available, used as anchor)
        num_entries: Number of entry levels (2-3)

    Returns:
        List of (price, weight) tuples where weight sums to 1.0.
        First entry is nearest to market (entered immediately).
        Subsequent entries are deeper into the zone (limit orders).

    Example for BUY with entry=100, sl=97:
        [(100.0, 0.5), (99.0, 0.3), (98.5, 0.2)]
    """
    if num_entries < 2:
        return [(entry_price, 1.0)]

    sl_dist = abs(entry_price - sl_price)
    if sl_dist == 0:
        return [(entry_price, 1.0)]

    anchor = fvg_entry_price if fvg_entry_price > 0 else entry_price

    # Weights: 50% at first level, 30% at second, 20% at third
    weights = [0.5, 0.3, 0.2][:num_entries]
    # Normalize weights
    total_w = sum(weights)
    weights = [w / total_w for w in weights]

    entries = []
    for i in range(num_entries):
        # Each subsequent entry is deeper into the zone
        # (closer to SL, better price)
        depth = i * 0.15  # 0%, 15%, 30% of SL distance
        if direction.upper() == "BUY":
            price = anchor - (sl_dist * depth)
        else:
            price = anchor + (sl_dist * depth)
        entries.append((round(price, 5), weights[i]))

    return entries


def check_correlation_with_scaling(
    new_symbol: str,
    new_direction: str,
    open_positions: dict,
) -> tuple[bool, float, str]:
    """Enhanced correlation check that allows reduced-size positions beyond the cap.

    Returns:
        (ok, size_multiplier, reason)
        - ok=True, mult=1.0: fully allowed
        - ok=True, mult=0.5: allowed at half size (at cap)
        - ok=False, mult=0.0: blocked (hard cap exceeded)
    """
    new_base = new_symbol.split(":")[-1]

    # First run the existing hard checks (same symbol, SMT pair, crypto correlation)
    # These should still block regardless
    if not open_positions:
        return True, 1.0, ""

    for pos in open_positions.values():
        pos_base = pos.symbol.split(":")[-1]

        # Same symbol same direction — always block
        if pos_base == new_base and pos.direction == new_direction:
            return False, 0.0, f"Already have {pos.direction} on {pos_base}"

        # SMT pair same direction — always block
        smt_pair = SMT_PAIRS.get(new_base)
        if smt_pair and smt_pair == pos_base and pos.direction == new_direction:
            return False, 0.0, f"Correlated: {new_base} + {pos_base} both {new_direction}"

    # Portfolio-level: count same-class positions
    def _is_risk_on(sym: str, dir: str) -> bool:
        return (sym in RISK_ON_ASSETS and dir == "BUY") or (sym in RISK_OFF_ASSETS and dir == "SELL")

    def _is_risk_off(sym: str, dir: str) -> bool:
        return (sym in RISK_OFF_ASSETS and dir == "BUY") or (sym in RISK_ON_ASSETS and dir == "SELL")

    new_is_risk_on = _is_risk_on(new_base, new_direction)
    new_is_risk_off = _is_risk_off(new_base, new_direction)

    if new_is_risk_on or new_is_risk_off:
        count = 0
        for pos in open_positions.values():
            pos_base = pos.symbol.split(":")[-1]
            if new_is_risk_on and _is_risk_on(pos_base, pos.direction):
                count += 1
            elif new_is_risk_off and _is_risk_off(pos_base, pos.direction):
                count += 1

        if count >= MAX_SAME_CLASS_POSITIONS + 1:  # Hard cap at 5
            class_name = "risk-on" if new_is_risk_on else "risk-off"
            return False, 0.0, f"Hard cap: {count} {class_name} positions (max {MAX_SAME_CLASS_POSITIONS + 1})"
        elif count >= MAX_SAME_CLASS_POSITIONS:  # Soft cap: allow at 50%
            class_name = "risk-on" if new_is_risk_on else "risk-off"
            return True, 0.5, f"At cap: {count} {class_name} positions — allowing at 50% size"

    return True, 1.0, ""


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
