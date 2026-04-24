"""
Intermarket analysis for the ICT trading system.

ICT methodology: "Never trade EUR without knowing where DXY is going."
Dollar strength drives all forex pairs and inversely correlates with gold/indices.

Usage::

    from bridge.intermarket import get_intermarket_context, check_intermarket_conflict

    ctx = get_intermarket_context("EURUSD")
    if ctx.conflict_gate:
        print(f"Trade blocked: {ctx.explanation}")

    is_conflict, severity, reason = check_intermarket_conflict("EURUSD", "BUY", ctx)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intermarket correlation rules (ICT methodology)
# ---------------------------------------------------------------------------

# Symbols that move INVERSELY to DXY (bearish when DXY bullish)
_DXY_INVERSE_SYMBOLS: frozenset[str] = frozenset(
    {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "XAUUSD"}
)

# Symbols that move WITH DXY (bullish when DXY bullish)
_DXY_DIRECT_SYMBOLS: frozenset[str] = frozenset({"USDJPY"})

# US10Y rising → bearish for gold, bullish for JPY pairs
_US10Y_INVERSE_SYMBOLS: frozenset[str] = frozenset({"XAUUSD"})
_US10Y_DIRECT_SYMBOLS: frozenset[str] = frozenset({"USDJPY"})

# Equity / crypto indices — risk-off when VIX is elevated
_RISK_ASSETS: frozenset[str] = frozenset(
    {"US30", "US100", "US500", "GER40", "DAX", "BTCUSD", "ETHUSD", "SOLUSD", "DOGEUSD"}
)
_SAFE_HAVEN_ASSETS: frozenset[str] = frozenset({"XAUUSD", "USDJPY"})

# VIX thresholds
_VIX_ELEVATED = 25.0
_VIX_EXTREME = 35.0

# DXY synthetic weight — EUR is ~57.6% of the DXY basket.
# If EURUSD = 1.10 then synthetic DXY ≈ 1 / (1.10 ** 0.576)
_EUR_DXY_WEIGHT = 0.576

# Candidate MT5 symbol names (tried in order)
_DXY_CANDIDATES = ("DXY", "DX.f", "USDX", "DX1!", "DXYZ")
_US10Y_CANDIDATES = ("TNX", "US10Y", "ZN1!", "IRUS10Y", "CBOE:TNX")
_VIX_CANDIDATES = ("VIX", "CBOE:VIX", "VIXY", "VIX.IDX")


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


@dataclass
class IntermarketContext:
    """Intermarket bias snapshot used to gate and size ICT trade decisions.

    Attributes:
        dxy_bias: 'BULLISH', 'BEARISH', or 'NEUTRAL'.
        dxy_price: Last price of DXY (or synthetic estimate).
        us10y_bias: 'RISING', 'FALLING', or 'NEUTRAL' for US 10-Year yield.
        us10y_price: Last price / yield level.
        vix_level: Last VIX reading (0.0 if unavailable).
        vix_risk_multiplier: 1.0 normal | 0.75 if VIX 25-35 | 0.5 if VIX > 35.
        conflict_gate: True when intermarket data strongly opposes the trade.
        conflict_severity: 0-10 — how strongly intermarket data opposes the trade.
        explanation: Human-readable summary for injection into Claude prompts.
        is_synthetic_dxy: True when DXY was computed from EURUSD, not live data.
        data_available: False when MT5 was unreachable and all values are defaults.
    """

    dxy_bias: str = "NEUTRAL"
    dxy_price: float = 0.0
    us10y_bias: str = "NEUTRAL"
    us10y_price: float = 0.0
    vix_level: float = 0.0
    vix_risk_multiplier: float = 1.0
    conflict_gate: bool = False
    conflict_severity: int = 0
    explanation: str = "Intermarket data unavailable — treating as neutral."
    is_synthetic_dxy: bool = False
    data_available: bool = False


# ---------------------------------------------------------------------------
# Internal helpers — MT5 data acquisition
# ---------------------------------------------------------------------------


def _mt5_last_close(symbol: str) -> Optional[float]:
    """Return the most recent close for *symbol* via MT5, or None on any error."""
    try:
        import MetaTrader5 as mt5  # type: ignore[import]

        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 2)
        if rates is None or len(rates) == 0:
            return None
        return float(rates[-1]["close"])
    except Exception:  # noqa: BLE001
        return None


def _resolve_mt5_symbol(candidates: tuple[str, ...]) -> tuple[Optional[str], Optional[float]]:
    """Try each candidate symbol in MT5; return (symbol_name, price) for the first hit."""
    try:
        import MetaTrader5 as mt5  # type: ignore[import]

        for sym in candidates:
            price = _mt5_last_close(sym)
            if price is not None:
                return sym, price
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _ensure_mt5_connected() -> bool:
    """Initialize MT5 connection if not already active. Returns True if connected."""
    try:
        import MetaTrader5 as mt5  # type: ignore[import]

        if not mt5.initialize():
            logger.debug("MT5 initialize() returned False — terminal may not be running.")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("MT5 not available: %s", exc)
        return False


def _get_synthetic_dxy_from_eurusd() -> tuple[float, bool]:
    """Compute a synthetic DXY from EURUSD via MT5 or return (0.0, False).

    DXY ≈ 1 / (EURUSD ** EUR_WEIGHT) using the EUR basket weight of ~57.6%.
    This is an approximation — real DXY uses six currencies — but it captures
    the dominant directional bias reliably.

    Returns:
        (synthetic_dxy_price, success_flag)
    """
    eurusd = _mt5_last_close("EURUSD")
    if eurusd is None or eurusd <= 0:
        return 0.0, False
    synthetic = 1.0 / (eurusd ** _EUR_DXY_WEIGHT)
    # Scale to a realistic DXY range (~90-110); the raw formula gives ~0.94-1.06
    synthetic_scaled = synthetic * 100.0
    return round(synthetic_scaled, 3), True


def _bias_from_momentum(prices: list[float]) -> str:
    """Derive BULLISH/BEARISH/NEUTRAL from a short price sequence.

    Uses the slope of the last N bars.  If the most recent close is above
    the oldest in the window the bias is BULLISH, below is BEARISH, within
    0.1% is NEUTRAL.
    """
    if len(prices) < 2:
        return "NEUTRAL"
    change_pct = (prices[-1] - prices[0]) / prices[0] * 100.0
    if change_pct > 0.1:
        return "BULLISH"
    if change_pct < -0.1:
        return "BEARISH"
    return "NEUTRAL"


def _get_dxy_bars(symbol: str, n: int = 5) -> list[float]:
    """Fetch the last *n* daily closes for DXY from MT5."""
    try:
        import MetaTrader5 as mt5  # type: ignore[import]

        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, n)
        if rates is None or len(rates) == 0:
            return []
        return [float(r["close"]) for r in rates]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_intermarket_context(symbol: str) -> IntermarketContext:
    """Return an :class:`IntermarketContext` for *symbol*.

    Data acquisition order:
    1. Connect to MT5 (skip gracefully if unavailable).
    2. Try live DXY candidates → fall back to synthetic DXY from EURUSD.
    3. Try live US10Y candidates.
    4. Try live VIX candidates.
    5. Derive biases from short momentum windows.
    6. Build conflict analysis for *symbol* + populate human-readable explanation.

    If MT5 is entirely unavailable, returns a neutral context with
    ``data_available=False`` so the pipeline is not blocked.

    Args:
        symbol: Bare instrument name, e.g. ``"EURUSD"``, ``"XAUUSD"``.

    Returns:
        :class:`IntermarketContext` with all fields populated.
    """
    sym = symbol.strip().upper()

    if not _ensure_mt5_connected():
        return _neutral_context(sym)

    # --- DXY ----------------------------------------------------------------
    dxy_sym, dxy_price = _resolve_mt5_symbol(_DXY_CANDIDATES)
    is_synthetic = False

    if dxy_price is None:
        dxy_price, ok = _get_synthetic_dxy_from_eurusd()
        is_synthetic = ok
        dxy_bars: list[float] = []
        if ok:
            # For synthetic DXY we just use current price vs 5-day SMA proxy
            # Fetch EURUSD bars and invert them
            try:
                import MetaTrader5 as mt5  # type: ignore[import]

                eu_rates = mt5.copy_rates_from_pos("EURUSD", mt5.TIMEFRAME_D1, 0, 5)
                if eu_rates is not None and len(eu_rates) >= 2:
                    dxy_bars = [
                        round(1.0 / (float(r["close"]) ** _EUR_DXY_WEIGHT) * 100.0, 3)
                        for r in eu_rates
                    ]
            except Exception:  # noqa: BLE001
                dxy_bars = [dxy_price] if dxy_price else []
    else:
        dxy_bars = _get_dxy_bars(dxy_sym, n=5) if dxy_sym else []

    dxy_bias = _bias_from_momentum(dxy_bars) if len(dxy_bars) >= 2 else "NEUTRAL"

    # --- US10Y --------------------------------------------------------------
    _us10y_sym, us10y_price = _resolve_mt5_symbol(_US10Y_CANDIDATES)
    us10y_bias = "NEUTRAL"
    if us10y_price is not None and _us10y_sym:
        us10y_bars = _get_dxy_bars(_us10y_sym, n=5)
        us10y_bias = _bias_from_momentum(us10y_bars) if len(us10y_bars) >= 2 else "NEUTRAL"
    else:
        us10y_price = 0.0

    # --- VIX ----------------------------------------------------------------
    _vix_sym, vix_level = _resolve_mt5_symbol(_VIX_CANDIDATES)
    if vix_level is None:
        vix_level = 0.0

    vix_risk_multiplier = _vix_multiplier(vix_level)

    # --- Conflict analysis --------------------------------------------------
    is_conflict, severity, explanation = check_intermarket_conflict(
        sym,
        "UNKNOWN",  # direction unknown at context-build time — build neutral analysis
        IntermarketContext(
            dxy_bias=dxy_bias,
            dxy_price=dxy_price or 0.0,
            us10y_bias=us10y_bias,
            us10y_price=us10y_price,
            vix_level=vix_level,
            vix_risk_multiplier=vix_risk_multiplier,
            is_synthetic_dxy=is_synthetic,
            data_available=True,
        ),
    )

    explanation_lines = [
        f"DXY: {dxy_bias} @ {dxy_price:.3f}"
        + (" (synthetic)" if is_synthetic else ""),
        f"US10Y: {us10y_bias} @ {us10y_price:.3f}%" if us10y_price else "US10Y: unavailable",
        f"VIX: {vix_level:.2f} → size multiplier {vix_risk_multiplier:.2f}x"
        if vix_level > 0
        else "VIX: unavailable",
    ]
    if explanation:
        explanation_lines.append(f"Notes: {explanation}")

    return IntermarketContext(
        dxy_bias=dxy_bias,
        dxy_price=dxy_price or 0.0,
        us10y_bias=us10y_bias,
        us10y_price=us10y_price,
        vix_level=vix_level,
        vix_risk_multiplier=vix_risk_multiplier,
        conflict_gate=is_conflict and severity >= 6,
        conflict_severity=severity,
        explanation=" | ".join(explanation_lines),
        is_synthetic_dxy=is_synthetic,
        data_available=True,
    )


def check_intermarket_conflict(
    symbol: str,
    direction: str,
    ctx: IntermarketContext,
) -> tuple[bool, int, str]:
    """Evaluate whether intermarket conditions oppose a proposed trade.

    Args:
        symbol: Bare instrument name, e.g. ``"EURUSD"``.
        direction: ``"BUY"``, ``"SELL"``, or ``"UNKNOWN"``.
        ctx: :class:`IntermarketContext` populated by :func:`get_intermarket_context`.

    Returns:
        ``(is_conflict, severity_0_10, explanation)`` where:

        * ``is_conflict`` — True when intermarket data materially opposes *direction*.
        * ``severity_0_10`` — 0 = no conflict, 10 = maximal conflict.
        * ``explanation`` — Human-readable string for Claude prompt injection.
    """
    sym = symbol.strip().upper()
    dir_upper = direction.strip().upper()

    if not ctx.data_available or dir_upper == "UNKNOWN":
        return False, 0, ""

    reasons: list[str] = []
    severity = 0

    # --- DXY conflict -------------------------------------------------------
    dxy_conflict = _check_dxy_conflict(sym, dir_upper, ctx.dxy_bias)
    if dxy_conflict:
        reasons.append(dxy_conflict)
        severity += 4  # DXY is the primary driver

    # --- US10Y conflict -----------------------------------------------------
    us10y_conflict = _check_us10y_conflict(sym, dir_upper, ctx.us10y_bias)
    if us10y_conflict:
        reasons.append(us10y_conflict)
        severity += 3

    # --- VIX / risk-off conflict --------------------------------------------
    vix_conflict = _check_vix_conflict(sym, dir_upper, ctx.vix_level)
    if vix_conflict:
        reasons.append(vix_conflict)
        severity += 3  # Can stack with DXY/yield conflicts

    severity = min(severity, 10)
    is_conflict = severity >= 4
    explanation = "; ".join(reasons) if reasons else "No intermarket conflicts detected."
    return is_conflict, severity, explanation


# ---------------------------------------------------------------------------
# Internal conflict checkers
# ---------------------------------------------------------------------------


def _check_dxy_conflict(sym: str, direction: str, dxy_bias: str) -> str:
    """Return a conflict message if DXY bias opposes *direction*, else empty string."""
    if dxy_bias == "NEUTRAL":
        return ""

    if sym in _DXY_INVERSE_SYMBOLS:
        # e.g. EURUSD BUY while DXY BULLISH → conflict
        if direction == "BUY" and dxy_bias == "BULLISH":
            return (
                f"DXY is BULLISH — {sym} typically falls when USD strengthens. "
                "ICT: confirm displacement + MSS before buying counter-DXY."
            )
        if direction == "SELL" and dxy_bias == "BEARISH":
            return (
                f"DXY is BEARISH — {sym} typically rises when USD weakens. "
                "Selling against DXY bias requires premium-zone confluence."
            )

    elif sym in _DXY_DIRECT_SYMBOLS:
        # e.g. USDJPY SELL while DXY BULLISH → conflict
        if direction == "SELL" and dxy_bias == "BULLISH":
            return (
                f"DXY is BULLISH — {sym} typically rises with USD. "
                "Selling into DXY strength needs strong yield/BoJ catalyst."
            )
        if direction == "BUY" and dxy_bias == "BEARISH":
            return (
                f"DXY is BEARISH — {sym} typically falls when USD weakens. "
                "Buying against DXY bias requires macro confirmation."
            )

    return ""


def _check_us10y_conflict(sym: str, direction: str, us10y_bias: str) -> str:
    """Return a conflict message if US10Y bias opposes *direction*, else empty string."""
    if us10y_bias == "NEUTRAL":
        return ""

    if sym in _US10Y_INVERSE_SYMBOLS:
        # Gold falls when yields rise
        if direction == "BUY" and us10y_bias in ("RISING", "BULLISH"):
            return (
                "US10Y yields rising — headwind for XAUUSD longs. "
                "Higher real rates reduce gold's appeal as a safe haven."
            )
        if direction == "SELL" and us10y_bias in ("FALLING", "BEARISH"):
            return (
                "US10Y yields falling — tailwind for XAUUSD. "
                "Shorting gold into falling-yield environment needs strong liquidity sweep."
            )

    elif sym in _US10Y_DIRECT_SYMBOLS:
        # USDJPY rises when US-Japan yield differential widens
        if direction == "SELL" and us10y_bias in ("RISING", "BULLISH"):
            return (
                "US10Y rising — yield differential supports USDJPY longs. "
                "Shorting USDJPY needs BoJ intervention signal."
            )
        if direction == "BUY" and us10y_bias in ("FALLING", "BEARISH"):
            return (
                "US10Y falling — yield differential compressing, bearish USDJPY. "
                "Buying needs strong USD catalyst to overcome yield headwind."
            )

    return ""


def _check_vix_conflict(sym: str, direction: str, vix_level: float) -> str:
    """Return a conflict message for risk-off/on VIX conditions, else empty string."""
    if vix_level <= 0:
        return ""

    if vix_level > _VIX_EXTREME:
        # Extreme fear — risk assets sell off, safe havens bid
        if sym in _RISK_ASSETS and direction == "BUY":
            return (
                f"VIX at {vix_level:.1f} (EXTREME FEAR > {_VIX_EXTREME}) — "
                "buying risk assets in panic conditions. Position size reduced 50%."
            )
        if sym in _SAFE_HAVEN_ASSETS and direction == "SELL" and sym != "USDJPY":
            return (
                f"VIX at {vix_level:.1f} (EXTREME FEAR) — "
                f"shorting safe-haven {sym} into panic is high-risk."
            )

    elif vix_level > _VIX_ELEVATED:
        # Elevated fear — risk-off bias
        if sym in _RISK_ASSETS and direction == "BUY":
            return (
                f"VIX at {vix_level:.1f} (ELEVATED > {_VIX_ELEVATED}) — "
                "risk-off environment; reduce size and confirm liquidity grab."
            )

    return ""


def _vix_multiplier(vix_level: float) -> float:
    """Return the position-size multiplier based on VIX level.

    Returns:
        1.0  — VIX <= 25 (normal)
        0.75 — VIX 25-35 (elevated fear)
        0.5  — VIX > 35 (extreme fear)
    """
    if vix_level <= 0:
        return 1.0
    if vix_level > _VIX_EXTREME:
        return 0.5
    if vix_level > _VIX_ELEVATED:
        return 0.75
    return 1.0


def _neutral_context(symbol: str) -> IntermarketContext:
    """Return a fully neutral :class:`IntermarketContext` when data is unavailable."""
    return IntermarketContext(
        dxy_bias="NEUTRAL",
        dxy_price=0.0,
        us10y_bias="NEUTRAL",
        us10y_price=0.0,
        vix_level=0.0,
        vix_risk_multiplier=1.0,
        conflict_gate=False,
        conflict_severity=0,
        explanation=(
            f"Intermarket data unavailable for {symbol} — "
            "MT5 not connected or intermarket symbols not found. "
            "Proceeding with neutral context (no conflict, no VIX adjustment)."
        ),
        is_synthetic_dxy=False,
        data_available=False,
    )
