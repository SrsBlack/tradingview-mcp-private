"""
Economic calendar — macro event awareness for ICT trading system.

ICT methodology: stand aside 30–45 min before and 15–30 min after any
high-impact scheduled release. This module checks whether the current
moment falls inside such a blackout window for a given symbol.

Priority:
  1. Live Forex Factory JSON feed (cached 6 hours)
  2. Static recurring schedule (built-in)
  3. No blackout (safe fallback — never crashes the pipeline)

Usage::

    from bridge.economic_calendar import is_news_blackout, get_upcoming_events

    blackout, name, minutes = is_news_blackout("EURUSD")
    if blackout:
        log.warning("News blackout: %s (%d min)", name, minutes)

    upcoming = get_upcoming_events("XAUUSD", hours_ahead=4)
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc

# ---------------------------------------------------------------------------
# Blackout windows by impact level
# ---------------------------------------------------------------------------

_BLACKOUT_BEFORE: dict[str, int] = {
    "extreme": 45,
    "high": 30,
    "medium": 0,  # medium = no blackout by default
}
_BLACKOUT_AFTER: dict[str, int] = {
    "extreme": 30,
    "high": 15,
    "medium": 0,
}

# ---------------------------------------------------------------------------
# Symbol → asset-class categories
# ---------------------------------------------------------------------------

SYMBOL_CATEGORIES: dict[str, list[str]] = {
    # Forex
    "EURUSD": ["USD_pairs", "EUR_pairs", "forex", "all"],
    "GBPUSD": ["USD_pairs", "GBP_pairs", "forex", "all"],
    "USDJPY": ["USD_pairs", "JPY_pairs", "forex", "all"],
    "USDCHF": ["USD_pairs", "CHF_pairs", "forex", "all"],
    "AUDUSD": ["USD_pairs", "AUD_pairs", "forex", "all"],
    "NZDUSD": ["USD_pairs", "NZD_pairs", "forex", "all"],
    "USDCAD": ["USD_pairs", "CAD_pairs", "forex", "all"],
    "EURGBP": ["EUR_pairs", "GBP_pairs", "forex", "all"],
    # Commodities
    "XAUUSD": ["gold", "commodities", "USD_pairs", "all"],
    "UKOIL":  ["oil", "commodities", "all"],
    "USOIL":  ["oil", "commodities", "all"],
    # Indices
    "US500":  ["indices", "risk_on", "all"],
    "NAS100": ["indices", "risk_on", "all"],
    "US30":   ["indices", "risk_on", "all"],
    "GER40":  ["indices", "all"],
    "UK100":  ["indices", "all"],
    # Crypto
    "BTCUSD": ["crypto", "risk_on", "all"],
    "ETHUSD": ["crypto", "risk_on", "all"],
    "SOLUSD": ["crypto", "risk_on", "all"],
    "DOGEUSD": ["crypto", "risk_on", "all"],
}

# ---------------------------------------------------------------------------
# Static high-impact recurring events
# ---------------------------------------------------------------------------

_STATIC_EVENTS: list[dict] = [
    # --- extreme impact ---
    {
        "name": "NFP",
        "schedule": "first_friday",
        "time_et": "08:30",
        "impact": "extreme",
        "affects": ["all"],
    },
    {
        "name": "CPI",
        "schedule": "day_10_16",  # broadly ~10th–16th of month
        "time_et": "08:30",
        "impact": "extreme",
        "affects": ["all"],
    },
    {
        "name": "FOMC_decision",
        "schedule": "fomc",
        "time_et": "14:00",
        "impact": "extreme",
        "affects": ["all"],
    },
    {
        "name": "FOMC_minutes",
        "schedule": "fomc_minutes",
        "time_et": "14:00",
        "impact": "high",
        "affects": ["all"],
    },
    {
        "name": "ECB_decision",
        "schedule": "ecb",
        "time_et": "07:45",
        "impact": "extreme",
        "affects": ["EURUSD", "EUR_pairs", "GER40"],
    },
    {
        "name": "BOE_decision",
        "schedule": "boe",
        "time_et": "07:00",
        "impact": "extreme",
        "affects": ["GBPUSD", "GBP_pairs", "UK100"],
    },
    # --- high impact ---
    {
        "name": "PPI",
        "schedule": "day_10_16",
        "time_et": "08:30",
        "impact": "high",
        "affects": ["USD_pairs", "gold"],
    },
    {
        "name": "retail_sales",
        "schedule": "day_12_18",
        "time_et": "08:30",
        "impact": "high",
        "affects": ["USD_pairs", "indices"],
    },
    {
        "name": "ISM_manufacturing",
        "schedule": "first_business_day",
        "time_et": "10:00",
        "impact": "high",
        "affects": ["USD_pairs", "indices"],
    },
    # --- medium impact ---
    {
        "name": "jobless_claims",
        "schedule": "thursday",
        "time_et": "08:30",
        "impact": "medium",
        "affects": ["USD_pairs"],
    },
]

# Hardcoded FOMC decision dates (Wed at 14:00 ET).
# Minutes released ~3 weeks after each meeting (approx Wed at 14:00).
_FOMC_DATES_2026: set[tuple[int, int]] = {
    (1, 28), (3, 18), (5, 6), (6, 17), (7, 29), (9, 16), (11, 4), (12, 16),
}
_FOMC_DATES_2027: set[tuple[int, int]] = {
    (1, 27), (3, 17), (5, 5), (6, 16), (7, 28), (9, 15), (11, 3), (12, 15),
}
_FOMC_DATES: dict[int, set[tuple[int, int]]] = {
    2026: _FOMC_DATES_2026,
    2027: _FOMC_DATES_2027,
}
# FOMC minutes ≈ 3 weeks after meeting (Wednesday)
_FOMC_MINUTES_DATES: dict[int, set[tuple[int, int]]] = {
    2026: {(2, 18), (4, 8), (5, 27), (7, 8), (8, 19), (10, 7), (11, 25), (1, 6)},
    2027: {(2, 17), (4, 7), (5, 26), (7, 7), (8, 18), (10, 6), (11, 24), (1, 5)},
}

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class NewsEvent:
    """A single scheduled news event with its blackout parameters."""

    name: str
    time_utc: datetime
    impact: str          # "extreme" | "high" | "medium"
    affects: list[str]
    blackout_before_min: int = field(init=False)
    blackout_after_min: int = field(init=False)

    def __post_init__(self) -> None:
        self.blackout_before_min = _BLACKOUT_BEFORE.get(self.impact, 0)
        self.blackout_after_min = _BLACKOUT_AFTER.get(self.impact, 0)

    @property
    def blackout_start_utc(self) -> datetime:
        return self.time_utc - timedelta(minutes=self.blackout_before_min)

    @property
    def blackout_end_utc(self) -> datetime:
        return self.time_utc + timedelta(minutes=self.blackout_after_min)


# ---------------------------------------------------------------------------
# Live-feed cache
# ---------------------------------------------------------------------------

_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_CACHE_TTL_SECONDS = 6 * 3600  # 6 hours

_cache_events: list[NewsEvent] = []
_cache_fetched_at: datetime | None = None


def _cache_is_fresh() -> bool:
    if _cache_fetched_at is None:
        return False
    return (datetime.now(_UTC) - _cache_fetched_at).total_seconds() < _CACHE_TTL_SECONDS


def _ff_impact_to_internal(ff_impact: str) -> str | None:
    """Map Forex Factory impact string to internal level. Returns None to skip."""
    mapping = {
        "High": "high",
        "Medium": "medium",
        "Low": None,   # skip low-impact
        "Holiday": None,
        "": None,
    }
    return mapping.get(ff_impact)


def _ff_title_to_impact_override(title: str) -> str | None:
    """Override impact level for known extreme-impact events by title keyword."""
    title_upper = title.upper()
    extreme_keywords = ("NFP", "NON-FARM", "CPI", "FOMC", "FED RATE", "INTEREST RATE")
    for kw in extreme_keywords:
        if kw in title_upper:
            return "extreme"
    return None


def _ff_event_affects(title: str, country: str) -> list[str]:
    """Infer asset-class list from event title and country."""
    title_upper = title.upper()
    if country == "US":
        base = ["USD_pairs", "all"]
        if any(kw in title_upper for kw in ("NFP", "NON-FARM", "CPI", "FOMC", "RATE", "PPI",
                                             "RETAIL", "ISM", "GDP", "JOBS", "PAYROLL")):
            return base
        return base
    if country == "EU":
        return ["EUR_pairs", "GER40", "all"]
    if country == "UK" or country == "GB":
        return ["GBP_pairs", "UK100", "all"]
    if country == "JP":
        return ["JPY_pairs", "all"]
    if country == "AU":
        return ["AUD_pairs", "all"]
    if country == "CA":
        return ["CAD_pairs", "oil", "all"]
    if country == "NZ":
        return ["NZD_pairs", "all"]
    if country == "CH":
        return ["CHF_pairs", "all"]
    return ["all"]


def _parse_ff_datetime(date_str: str, time_str: str) -> datetime | None:
    """
    Parse Forex Factory date/time strings into UTC datetime.

    date_str: "01-27-2026"  (MM-DD-YYYY)
    time_str: "8:30am"      (no leading zero, 12-hour)
    """
    try:
        # Handle "All Day" or missing time
        if not time_str or time_str.lower() in ("all day", "tentative", ""):
            return None
        date_part = datetime.strptime(date_str, "%m-%d-%Y").date()
        # Normalize: "8:30am" → "8:30 AM"
        time_clean = time_str.strip().upper()
        if "AM" in time_clean or "PM" in time_clean:
            # May or may not have space before AM/PM
            if time_clean[-2:] in ("AM", "PM") and " " not in time_clean:
                time_clean = time_clean[:-2] + " " + time_clean[-2:]
            dt_et = datetime.strptime(f"{date_part} {time_clean}", "%Y-%m-%d %I:%M %p")
        else:
            return None
        # Attach ET timezone
        dt_et = dt_et.replace(tzinfo=_ET)
        return dt_et.astimezone(_UTC)
    except (ValueError, KeyError):
        return None


def _fetch_live_events() -> list[NewsEvent]:
    """
    Fetch this week's events from Forex Factory JSON feed.

    Returns empty list on any error — callers fall back to static schedule.
    """
    try:
        req = urllib.request.Request(
            _FF_URL,
            headers={"User-Agent": "Mozilla/5.0 (economic-calendar/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            TimeoutError, OSError) as exc:
        log.debug("FF calendar fetch failed: %s", exc)
        return []

    events: list[NewsEvent] = []
    for item in raw:
        try:
            ff_impact = item.get("impact", "")
            internal_impact = _ff_impact_to_internal(ff_impact)
            if internal_impact is None:
                continue

            title = item.get("title", "")
            country = item.get("country", "")

            # Override impact for well-known extreme events
            override = _ff_title_to_impact_override(title)
            if override:
                internal_impact = override

            # Skip medium unless it's a known category we care about
            if internal_impact == "medium" and country not in ("US", "EU", "UK", "GB"):
                continue

            date_str = item.get("date", "")
            time_str = item.get("time", "")
            time_utc = _parse_ff_datetime(date_str, time_str)
            if time_utc is None:
                continue

            affects = _ff_event_affects(title, country)

            events.append(
                NewsEvent(
                    name=title,
                    time_utc=time_utc,
                    impact=internal_impact,
                    affects=affects,
                )
            )
        except Exception:  # noqa: BLE001 — never crash on a single bad row
            continue

    log.debug("FF calendar: parsed %d events", len(events))
    return events


def _get_live_events() -> list[NewsEvent]:
    """Return cached live events, refreshing if stale."""
    global _cache_events, _cache_fetched_at
    if not _cache_is_fresh():
        fetched = _fetch_live_events()
        if fetched:
            _cache_events = fetched
            _cache_fetched_at = datetime.now(_UTC)
        else:
            # Keep old cache alive rather than clearing it
            if _cache_fetched_at is None:
                _cache_events = []
                _cache_fetched_at = datetime.now(_UTC)
    return _cache_events


# ---------------------------------------------------------------------------
# Static schedule generator
# ---------------------------------------------------------------------------

def _et_to_utc(et_naive: datetime) -> datetime:
    """Attach ET timezone to a naive datetime and convert to UTC."""
    return et_naive.replace(tzinfo=_ET).astimezone(_UTC)


def _parse_et_time(time_str: str) -> tuple[int, int]:
    """Parse "HH:MM" string into (hour, minute)."""
    h, m = time_str.split(":")
    return int(h), int(m)


def _first_weekday_of_month(year: int, month: int, weekday: int) -> int:
    """Return the day-of-month of the first occurrence of weekday (0=Mon)."""
    first = datetime(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return 1 + delta


def _first_business_day_of_month(year: int, month: int) -> int:
    """Return the day-of-month for the first Mon-Fri of the month."""
    first = datetime(year, month, 1)
    wd = first.weekday()
    if wd < 5:  # Mon–Fri
        return 1
    return 1 + (7 - wd)  # Monday


def _is_fomc_date(year: int, month: int, day: int) -> bool:
    known = _FOMC_DATES.get(year, set())
    if (month, day) in known:
        return True
    # Heuristic fallback for years not hardcoded: any Wed in the typical
    # FOMC cadence (roughly 8 per year, ~6-week spacing). We accept false
    # positives from the static schedule — live feed overrides anyway.
    dt = datetime(year, month, day)
    return dt.weekday() == 2 and month in (1, 3, 5, 6, 7, 9, 11, 12)


def _is_fomc_minutes_date(year: int, month: int, day: int) -> bool:
    known = _FOMC_MINUTES_DATES.get(year, set())
    return (month, day) in known


def _generate_static_events(window_start: datetime, window_end: datetime) -> list[NewsEvent]:
    """
    Generate NewsEvent objects from the static schedule for the given UTC window.

    Iterates day-by-day through the window and emits events whose scheduled
    time falls within [window_start, window_end].
    """
    events: list[NewsEvent] = []

    # Scan each calendar day covered by the window (in ET)
    start_et = window_start.astimezone(_ET).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_et = window_end.astimezone(_ET).replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    current = start_et
    while current <= end_et:
        y, m, d = current.year, current.month, current.day
        wd = current.weekday()  # 0=Mon … 6=Sun

        for spec in _STATIC_EVENTS:
            schedule = spec["schedule"]
            time_h, time_min = _parse_et_time(spec["time_et"])
            candidate_et = datetime(y, m, d, time_h, time_min)
            candidate_utc = _et_to_utc(candidate_et)

            if not (window_start <= candidate_utc <= window_end):
                current += timedelta(days=1)
                break  # move to next day — this spec check is per-day below

            matches = False
            if schedule == "first_friday":
                first_fri = _first_weekday_of_month(y, m, 4)  # 4=Fri
                matches = (wd == 4 and d == first_fri)

            elif schedule == "day_10_16":
                matches = (10 <= d <= 16 and wd in (1, 2, 3))  # Tue–Thu

            elif schedule == "day_12_18":
                matches = (12 <= d <= 18 and wd in (1, 2, 3))

            elif schedule == "first_business_day":
                fbd = _first_business_day_of_month(y, m)
                matches = (d == fbd)

            elif schedule == "thursday":
                matches = (wd == 3)  # 3=Thu

            elif schedule == "fomc":
                matches = _is_fomc_date(y, m, d)

            elif schedule == "fomc_minutes":
                matches = _is_fomc_minutes_date(y, m, d)

            elif schedule == "ecb":
                # ECB meets ~8 per year; rough heuristic — Thu, roughly
                # Jan/Mar/Apr/Jun/Jul/Sep/Oct/Dec — override by live feed
                matches = (wd == 3 and m in (1, 3, 4, 6, 7, 9, 10, 12)
                           and 5 <= d <= 20)

            elif schedule == "boe":
                # BOE meets ~8 per year; roughly same cadence as ECB
                matches = (wd == 3 and m in (2, 3, 5, 6, 8, 9, 11, 12)
                           and 3 <= d <= 20)

            if matches:
                events.append(
                    NewsEvent(
                        name=spec["name"],
                        time_utc=candidate_utc,
                        impact=spec["impact"],
                        affects=list(spec["affects"]),
                    )
                )

        current += timedelta(days=1)

    return events


def _schedule_is_valid_for_day(
    spec: dict, year: int, month: int, day: int, weekday: int
) -> bool:
    """Return True if a static spec fires on the given date."""
    schedule = spec["schedule"]
    if schedule == "first_friday":
        first_fri = _first_weekday_of_month(year, month, 4)
        return weekday == 4 and day == first_fri

    if schedule == "day_10_16":
        return 10 <= day <= 16 and weekday in (1, 2, 3)

    if schedule == "day_12_18":
        return 12 <= day <= 18 and weekday in (1, 2, 3)

    if schedule == "first_business_day":
        return day == _first_business_day_of_month(year, month)

    if schedule == "thursday":
        return weekday == 3

    if schedule == "fomc":
        return _is_fomc_date(year, month, day)

    if schedule == "fomc_minutes":
        return _is_fomc_minutes_date(year, month, day)

    if schedule == "ecb":
        return weekday == 3 and month in (1, 3, 4, 6, 7, 9, 10, 12) and 5 <= day <= 20

    if schedule == "boe":
        return weekday == 3 and month in (2, 3, 5, 6, 8, 9, 11, 12) and 3 <= day <= 20

    return False


def _get_static_events_for_window(
    window_start: datetime, window_end: datetime
) -> list[NewsEvent]:
    """Generate static events within the UTC window."""
    events: list[NewsEvent] = []
    start_et = window_start.astimezone(_ET).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # Advance by one day at a time
    days_in_window = int((window_end - window_start).total_seconds() // 86400) + 2
    for offset in range(days_in_window):
        current = start_et + timedelta(days=offset)
        y, m, d = current.year, current.month, current.day
        wd = current.weekday()

        for spec in _STATIC_EVENTS:
            if not _schedule_is_valid_for_day(spec, y, m, d, wd):
                continue
            time_h, time_min = _parse_et_time(spec["time_et"])
            candidate_et = datetime(y, m, d, time_h, time_min)
            candidate_utc = _et_to_utc(candidate_et)
            if not (window_start <= candidate_utc <= window_end):
                continue
            events.append(
                NewsEvent(
                    name=spec["name"],
                    time_utc=candidate_utc,
                    impact=spec["impact"],
                    affects=list(spec["affects"]),
                )
            )
    return events


# ---------------------------------------------------------------------------
# Symbol → category matching
# ---------------------------------------------------------------------------

def _symbol_categories(symbol: str) -> list[str]:
    """Return asset-class categories for symbol. Strips broker prefix if present."""
    bare = symbol.upper()
    if ":" in bare:
        bare = bare.split(":", 1)[1]
    return SYMBOL_CATEGORIES.get(bare, ["all"])


def _event_affects_symbol(event: NewsEvent, symbol: str) -> bool:
    """Return True if the event's affects list intersects the symbol's categories."""
    sym_cats = set(_symbol_categories(symbol))
    return bool(sym_cats.intersection(event.affects))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_news_blackout(
    symbol: str,
    now: datetime | None = None,
) -> tuple[bool, str, int]:
    """
    Check whether the current moment falls inside a news blackout window.

    Blackout windows:
    - Extreme events (NFP, FOMC, CPI, ECB, BOE): 45 min before → 30 min after
    - High events (PPI, Retail Sales, ISM, FOMC minutes): 30 min before → 15 min after
    - Medium events: no blackout (0/0)

    Parameters
    ----------
    symbol:
        Instrument to check, e.g. ``"EURUSD"`` or ``"OANDA:EURUSD"``.
    now:
        UTC datetime to evaluate (defaults to current time).

    Returns
    -------
    (is_blackout, event_name, minutes):
        - ``is_blackout``: True if we are inside a blackout window.
        - ``event_name``: Name of the triggering event, or ``""`` if none.
        - ``minutes``: Positive = minutes until event; negative = minutes since event.
          0 when not in blackout.
    """
    try:
        if now is None:
            now = datetime.now(_UTC)

        # Build a ±2-hour window around now to limit how many events we scan
        scan_start = now - timedelta(hours=2)
        scan_end = now + timedelta(hours=2)

        events = _get_events_for_window(scan_start, scan_end)

        for event in events:
            if not _event_affects_symbol(event, symbol):
                continue
            if event.blackout_before_min == 0 and event.blackout_after_min == 0:
                continue
            if event.blackout_start_utc <= now <= event.blackout_end_utc:
                delta_sec = (event.time_utc - now).total_seconds()
                minutes_rel = int(delta_sec / 60)  # positive = before, negative = after
                return True, event.name, minutes_rel

        return False, "", 0

    except Exception:  # noqa: BLE001 — absolute last resort; must not crash pipeline
        log.exception("is_news_blackout error — returning safe default")
        return False, "", 0


def get_upcoming_events(
    symbol: str,
    hours_ahead: int = 4,
    now: datetime | None = None,
) -> list[dict]:
    """
    Return upcoming news events affecting ``symbol`` within ``hours_ahead``.

    Parameters
    ----------
    symbol:
        Instrument, e.g. ``"XAUUSD"`` or ``"OANDA:XAUUSD"``.
    hours_ahead:
        How far forward to scan (default 4 hours).
    now:
        UTC reference time (defaults to current time).

    Returns
    -------
    List of dicts with keys:
    ``{"name", "time_utc", "time_et", "impact", "minutes_away", "affects"}``
    sorted by ``minutes_away`` ascending.
    """
    try:
        if now is None:
            now = datetime.now(_UTC)

        scan_end = now + timedelta(hours=hours_ahead)
        events = _get_events_for_window(now, scan_end)

        result: list[dict] = []
        for event in events:
            if not _event_affects_symbol(event, symbol):
                continue
            minutes_away = int((event.time_utc - now).total_seconds() / 60)
            if minutes_away < 0:
                continue  # already passed
            time_et = event.time_utc.astimezone(_ET).strftime("%H:%M ET")
            result.append(
                {
                    "name": event.name,
                    "time_utc": event.time_utc.isoformat(),
                    "time_et": time_et,
                    "impact": event.impact,
                    "minutes_away": minutes_away,
                    "affects": event.affects,
                }
            )

        result.sort(key=lambda x: x["minutes_away"])
        return result

    except Exception:  # noqa: BLE001
        log.exception("get_upcoming_events error — returning empty list")
        return []


# ---------------------------------------------------------------------------
# Internal: merge live + static, deduplicate
# ---------------------------------------------------------------------------

def _get_events_for_window(
    window_start: datetime, window_end: datetime
) -> list[NewsEvent]:
    """
    Return merged events from live feed (preferred) and static schedule.

    Live events take precedence; static events for the same time slot are
    dropped to avoid double-counting.
    """
    live = _get_live_events()

    # Filter live events to the window
    live_in_window = [
        e for e in live
        if window_start <= e.time_utc <= window_end
    ]

    if live_in_window:
        # Trust the live feed; supplement with static for types not covered
        # (e.g. static schedule knows about ECB/BOE which FF may not label
        # "extreme" — but keep only static entries whose exact time is NOT
        # already present in the live feed)
        live_times = {e.time_utc for e in live_in_window}
        static_in_window = _get_static_events_for_window(window_start, window_end)
        supplemental = [e for e in static_in_window if e.time_utc not in live_times]
        return live_in_window + supplemental

    # Live feed empty or failed → use static schedule exclusively
    return _get_static_events_for_window(window_start, window_end)
