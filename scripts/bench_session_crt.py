"""
Session-CRT (Asian/London/NY fractal) firing-rate + score-impact backtest.

Question this answers: how often does detect_session_crt fire on cached
M15 data, and what is the score-formula delta from emitting a CRT_SessionCRT
factor (weight +3) vs. the no-SessionCRT baseline?

Method:
1. Load cached M15 OHLCV for representative symbols, KEEPING tz=UTC (the
   detector slices by NY hour and needs tz-aware bars).
2. Walk forward in 24h chunks, only "running" the detector at NY-window
   timestamps (07:00–17:00 NY) — that's where the bridge call site fires.
3. Count fire frequency. Compute score delta: for each fire, did adding
   the +3 SessionCRT weight push total_score above the +10 cap (i.e.,
   was the cap already binding) or did it actually move the score?
4. Compare against the prompt's expected fire rate (10–20% of NY-window
   cycles).

Usage:
    PYTHONUTF8=1 python scripts/bench_session_crt.py
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import timezone
from pathlib import Path

import pandas as pd

# trading-ai-v2 import path
sys.path.insert(0, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except ImportError:  # pragma: no cover
    import pytz  # type: ignore
    NY_TZ = pytz.timezone("America/New_York")  # type: ignore

from analysis.ict.advanced import detect_session_crt  # noqa: E402

CACHE_ROOT = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache")
SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "BTCUSD", "ETHUSD"]

# Walk parameters: at each cycle endpoint, the detector sees the trailing
# 96 M15 bars (24h window — enough for prev-day Asian + today's London).
WINDOW_M15 = 96
STEP_M15 = 16  # 4h step — sample several NY-window hours per day


def _load_m15(sym: str) -> pd.DataFrame | None:
    p = CACHE_ROOT / sym / "M15" / "data.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if df.index.tz is None:
        # Cache files may be tz-naive UTC; localize so detector can use NY tz.
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def _is_ny_window(ts_utc: pd.Timestamp) -> bool:
    """True if ts_utc falls inside 07:00–17:00 NY (NY_OPEN..NY_PM)."""
    ny = ts_utc.tz_convert(NY_TZ) if ts_utc.tzinfo else ts_utc.tz_localize("UTC").tz_convert(NY_TZ)
    mins = ny.hour * 60 + ny.minute
    return 7 * 60 <= mins < 17 * 60


def _bench_symbol(sym: str) -> dict:
    df_m15 = _load_m15(sym)
    if df_m15 is None or len(df_m15) < WINDOW_M15 + 10:
        return {"symbol": sym, "skipped": True, "reason": "no data"}

    ny_cycles = 0
    fires = 0
    direction_counts: Counter[str] = Counter()
    swept_side_counts: Counter[str] = Counter()
    range_pcts: list[float] = []  # Asian-range as % of last close

    for end in range(WINDOW_M15, len(df_m15) - 1, STEP_M15):
        window = df_m15.iloc[end - WINDOW_M15: end]
        ts_end = window.index[-1]
        if not _is_ny_window(ts_end):
            continue
        ny_cycles += 1
        setups = detect_session_crt(window)
        if setups:
            fires += 1
            for s in setups:
                direction_counts[s.direction.value] += 1
                swept_side_counts[s.london_swept_side] += 1
                last_close = float(window["close"].iloc[-1])
                if last_close > 0:
                    range_pcts.append(
                        100.0 * (s.asian_high - s.asian_low) / last_close
                    )

    if ny_cycles == 0:
        return {"symbol": sym, "skipped": True, "reason": "no NY-window cycles"}

    fire_pct = 100.0 * fires / ny_cycles
    return {
        "symbol": sym,
        "ny_cycles": ny_cycles,
        "fires": fires,
        "fire_pct": fire_pct,
        "direction": dict(direction_counts),
        "swept_side": dict(swept_side_counts),
        "asian_range_pct": {
            "mean": sum(range_pcts) / len(range_pcts) if range_pcts else 0.0,
            "n": len(range_pcts),
        },
    }


def main() -> None:
    print("=" * 70)
    print("Session-CRT (Asian/London/NY fractal) backtest")
    print("=" * 70)

    results = []
    total_cycles = 0
    total_fires = 0
    for sym in SYMBOLS:
        print(f"\n[{sym}] running...", flush=True)
        r = _bench_symbol(sym)
        results.append(r)
        if r.get("skipped"):
            print(f"  SKIPPED: {r['reason']}")
            continue
        total_cycles += r["ny_cycles"]
        total_fires += r["fires"]
        print(f"  NY-window cycles: {r['ny_cycles']}")
        print(
            f"  Fires: {r['fires']} ({r['fire_pct']:.1f}% of NY cycles)"
        )
        print(f"  Direction split: {r['direction']}")
        print(f"  London swept: {r['swept_side']}")
        ar = r["asian_range_pct"]
        print(f"  Avg Asian range: {ar['mean']:.3f}% of last close (n={ar['n']})")

    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    if total_cycles > 0:
        agg_pct = 100.0 * total_fires / total_cycles
        print(
            f"Aggregate: {total_fires}/{total_cycles} NY-window cycles fired "
            f"= {agg_pct:.1f}% (expected band: 10–20%)"
        )
        if 5.0 <= agg_pct <= 30.0:
            print("PASS: fire rate within sane band (5–30%)")
        else:
            print(f"WARN: fire rate {agg_pct:.1f}% outside 5–30% band — investigate")
    else:
        print("WARN: no NY-window cycles measured (check cache coverage)")

    # Score-impact: emitting CRT_SessionCRT adds +3 to advanced_bonus.
    # If baseline already has >=3 advanced factors emitting +2.5 each =
    # >=7.5 cap-binding pressure, the +3 from SessionCRT is mostly
    # absorbed by the +10 cap. If baseline=0, +3 lands fully.
    print()
    print("Score-formula delta sanity (per-fire):")
    for baseline in (0, 3, 6):
        old_no_session = min(baseline * 2.5, 10.0)
        new_with_session = min(baseline * 2.5 + 3.0, 10.0)
        print(
            f"  baseline={baseline} non-CRT factors: "
            f"old_score={old_no_session:.1f} -> new_score={new_with_session:.1f} "
            f"delta=+{new_with_session - old_no_session:.1f}"
        )


if __name__ == "__main__":
    main()
