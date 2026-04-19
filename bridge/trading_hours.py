"""
Trading hours, session windows, and time-related utilities.

Pure functions with no bridge dependencies — only stdlib.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def load_strategy_knowledge() -> dict:
    """Load strategy knowledge files for backtest confidence multipliers."""
    knowledge_dir = Path(__file__).parent / "strategy_knowledge"
    result = {"symbol_profiles": {}, "mt5_insights": {}, "session_routing": {}}

    for name, key in [("symbol_profiles.json", "symbol_profiles"),
                      ("mt5_insights.json", "mt5_insights"),
                      ("session_routing.json", "session_routing")]:
        path = knowledge_dir / name
        if path.exists():
            try:
                result[key] = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    return result


def get_backtest_confidence(symbol: str, knowledge: dict) -> float:
    """Get backtest confidence multiplier for a symbol from MT5 data."""
    profiles = knowledge.get("symbol_profiles", {})
    profile = profiles.get(symbol, {})
    if profile:
        mt5 = profile.get("mt5_metrics", {})
        sharpe = mt5.get("sharpe_ratio")
        if sharpe is not None:
            if sharpe > 15:
                return 1.4
            elif sharpe > 10:
                return 1.2
            elif sharpe > 5:
                return 1.1
        pf = mt5.get("profit_factor")
        if pf is not None and pf > 2.0:
            return 1.1
        conf = profile.get("risk_profile", {}).get("backtest_confidence_multiplier")
        if conf is not None:
            return conf

    insights = knowledge.get("mt5_insights", {})
    tiers = insights.get("performance_tiers", {})
    for tier_name, tier_data in tiers.items():
        if symbol in tier_data.get("symbols", []):
            return tier_data.get("confidence_multiplier", 1.0)

    return 1.0  # no data = neutral


def get_symbol_risk_override(symbol: str, grade: str, rules: dict) -> float | None:
    """Get per-symbol risk override from rules.json."""
    profiles = rules.get("symbol_profiles", {})
    profile = profiles.get(symbol, {})
    overrides = profile.get("risk_overrides", {})
    key = f"grade_{grade.lower()}"
    return overrides.get(key)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_m15_boundary(dt: datetime) -> bool:
    """Check if current time is within 30s of a 15-minute boundary."""
    return dt.minute % 15 == 0 and dt.second < 30


def ny_hour(dt: datetime) -> int:
    """Get current hour in New York time (handles DST automatically)."""
    from zoneinfo import ZoneInfo
    ny = dt.astimezone(ZoneInfo("America/New_York"))
    return ny.hour


def is_lunch_pause(dt: datetime) -> bool:
    """12:00-13:00 NY = low-volume lunch hour."""
    h = ny_hour(dt)
    return h == 12


def is_high_impact_news_window(dt: datetime) -> tuple[bool, str]:
    """
    Check if we're within 15 minutes of a known high-impact news event.

    Returns (is_near_news, event_name).
    Uses static schedule for recurring monthly events (FOMC, NFP, CPI).
    """
    from zoneinfo import ZoneInfo
    ny = dt.astimezone(ZoneInfo("America/New_York"))
    day = ny.day
    weekday = ny.weekday()  # 0=Mon
    hour, minute = ny.hour, ny.minute
    current_min = hour * 60 + minute

    # NFP: First Friday of month at 8:30 AM ET
    if weekday == 4 and day <= 7:
        nfp_min = 8 * 60 + 30
        if abs(current_min - nfp_min) <= 15:
            return True, "NFP (Non-Farm Payrolls)"

    # CPI: Usually 2nd Tuesday-Thursday of month at 8:30 AM ET
    if 10 <= day <= 14 and weekday in (1, 2, 3):
        cpi_min = 8 * 60 + 30
        if abs(current_min - cpi_min) <= 15:
            return True, "CPI (Consumer Price Index)"

    # FOMC: Usually Wed at 2:00 PM ET, roughly every 6 weeks
    fomc_dates = {
        (1, 29), (3, 19), (5, 7), (6, 18), (7, 30), (9, 17), (11, 5), (12, 17)
    }
    if (ny.month, day) in fomc_dates:
        fomc_min = 14 * 60  # 2:00 PM ET
        if abs(current_min - fomc_min) <= 15:
            return True, "FOMC Rate Decision"

    return False, ""


def utc_hour(dt: datetime) -> int:
    return dt.hour


# Per-symbol trading windows (UTC hours, inclusive start, exclusive end)
SYMBOL_SESSIONS: dict[str, list[tuple[int, int]]] = {
    # Indices — London open + NY session only (futures market hours)
    "CBOT:YM1!":  [(7, 22)],
    # US indices CFDs — nearly 24/7 (daily maintenance break 5-6pm ET)
    "CAPITALCOM:US500": [(0, 21), (22, 24)],
    "CAPITALCOM:US100": [(0, 21), (22, 24)],
    # Forex — London + NY (7am-5pm UTC)
    "OANDA:EURUSD": [(7, 17)],
    # Crypto — 24/7
    "BITSTAMP:BTCUSD":  [(0, 24)],
    "COINBASE:ETHUSD":  [(0, 24)],
    "COINBASE:SOLUSD":  [(0, 24)],
    "COINBASE:DOGEUSD": [(0, 24)],
    # Gold — Asia + London + NY
    "OANDA:XAUUSD": [(2, 12), (13, 17)],
    # Oil — London + NY only
    "TVC:UKOIL":  [(7, 17)],
}

# Symbols that trade 24/7
ALWAYS_ON = {
    "BITSTAMP:BTCUSD", "COINBASE:ETHUSD", "COINBASE:SOLUSD",
    "COINBASE:DOGEUSD",
}


def symbol_is_active(symbol: str, dt: datetime) -> bool:
    """Check if a symbol should be analyzed at the given UTC time."""
    if symbol in ALWAYS_ON:
        return True
    sessions = SYMBOL_SESSIONS.get(symbol)
    if not sessions:
        h = utc_hour(dt)
        return 7 <= h < 21
    h = utc_hour(dt)
    return any(start <= h < end for start, end in sessions)


def is_trading_hours(dt: datetime) -> bool:
    """At least one symbol is tradeable right now."""
    return any(symbol_is_active(s, dt) for s in SYMBOL_SESSIONS)
