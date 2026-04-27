"""
HTF rejection detector — parameter sweep on broker history.

Companion to bench_htf_rejection.py: loads the same trades, but runs
the detector across a grid of (lookback_m15, displacement_min,
body_min_pct) parameter combinations to see whether tuning shifts the
catch rate up without inflating wrong-rate.

Output is a matrix per direction (LONGS / SHORTS) with one row per
parameter combo. The "best" combo for each side maximises:

    score = catch_count - wrong_count

(wrong-blocks are equally bad as catch-misses for our purposes —
they would actively veto a correct trade if Phase 2 wires bias
override.)

Usage:
    PYTHONUTF8=1 python scripts/bench_htf_rejection_sweep.py
"""

from __future__ import annotations

import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

warnings.filterwarnings(
    "ignore",
    message="Converting to PeriodArray/Index representation will drop timezone information",
    category=UserWarning,
)

_BRIDGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BRIDGE_ROOT))
sys.path.insert(1, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

# Reuse loader / cache helpers from the baseline bench
from scripts.bench_htf_rejection import (  # noqa: E402
    BROKER_TO_CACHE,
    compute_atr,
    filter_clean_trades,
    load_broker_trades,
    load_m15_cache,
    resample_ohlc,
)
from analysis.fvg import detect_fvgs  # noqa: E402
from analysis.order_blocks import detect_order_blocks  # noqa: E402
from analysis.structure import detect_swings  # noqa: E402
from analysis.htf_rejection import (  # noqa: E402
    detect_htf_rejection,
    strongest_rejection_direction,
)
from core.types import Direction, FVGQuality  # noqa: E402


# Sweep grid
LOOKBACK_GRID = [6, 12, 24]
DISPLACEMENT_GRID = [1.0, 1.5]
BODY_GRID = [0.40, 0.45, 0.55]

# HTF zone universe — widened so multi-day swing structures survive (the
# April 22-23 ETH bearish OB was outside the original 30-bar H4 window).
H4_MAX_AGE_BARS = 120
D1_MAX_AGE_BARS = 60


def replay_one(trade: dict, lookback: int, disp_min: float, body_min: float) -> str:
    """Return CAUGHT / WRONG / MISSED / SKIP for a single trade + param combo."""
    sym = trade["symbol"]
    cache_name = BROKER_TO_CACHE.get(sym)
    if cache_name is None:
        return "SKIP"
    df_m15_full = load_m15_cache(cache_name)
    if df_m15_full is None:
        return "SKIP"
    entry_ts = pd.Timestamp(trade["entry_time"])
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.tz_localize("UTC")
    if entry_ts < df_m15_full.index[0] or entry_ts > df_m15_full.index[-1]:
        return "SKIP"
    df_m15 = df_m15_full.loc[:entry_ts]
    if len(df_m15) >= 1 and df_m15.index[-1] >= entry_ts:
        df_m15 = df_m15.iloc[:-1]
    if len(df_m15) < 200:
        return "SKIP"

    df_h4 = resample_ohlc(df_m15, "4h")
    df_d1 = resample_ohlc(df_m15, "1D")
    if len(df_h4) < 10 or len(df_d1) < 10:
        return "SKIP"

    atr_m15 = compute_atr(df_m15.tail(200), period=14)
    if atr_m15 <= 0:
        return "SKIP"

    try:
        h4_fvgs = detect_fvgs(df_h4.tail(200), max_age_bars=H4_MAX_AGE_BARS, min_quality=FVGQuality.AGGRESSIVE)
        d1_fvgs = detect_fvgs(df_d1.tail(120), max_age_bars=D1_MAX_AGE_BARS, min_quality=FVGQuality.AGGRESSIVE)
        h4_swings = detect_swings(df_h4.tail(200), lookback=3)
        d1_swings = detect_swings(df_d1.tail(120), lookback=2)
        h4_obs = detect_order_blocks(
            df_h4.tail(200), fvgs=h4_fvgs, swings=h4_swings, lookback=H4_MAX_AGE_BARS,
            require_fvg=False, require_bos=False,
        )
        d1_obs = detect_order_blocks(
            df_d1.tail(120), fvgs=d1_fvgs, swings=d1_swings, lookback=D1_MAX_AGE_BARS,
            require_fvg=False, require_bos=False,
        )
    except Exception:
        return "SKIP"

    rejections = (
        detect_htf_rejection(
            df_m15=df_m15.tail(60), htf_fvgs=h4_fvgs, htf_obs=h4_obs,
            atr_m15=atr_m15, lookback_m15=lookback,
            displacement_min=disp_min, body_min_pct=body_min,
            htf_timeframe="H4",
            as_of_ts=entry_ts, max_displacement_age_minutes=60,
        )
        + detect_htf_rejection(
            df_m15=df_m15.tail(60), htf_fvgs=d1_fvgs, htf_obs=d1_obs,
            atr_m15=atr_m15, lookback_m15=lookback,
            displacement_min=disp_min, body_min_pct=body_min,
            htf_timeframe="D1",
            as_of_ts=entry_ts, max_displacement_age_minutes=60,
        )
    )
    fired = strongest_rejection_direction(rejections)
    trade_dir = Direction.BULLISH if trade["direction"] == "BUY" else Direction.BEARISH
    if fired is None:
        return "MISSED"
    if fired == trade_dir:
        return "CAUGHT"
    return "WRONG"


def main() -> int:
    print("=" * 78)
    print("HTF rejection — parameter sweep")
    print("=" * 78)

    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 28, tzinfo=timezone.utc)
    print(f"Pulling FTMO broker history {start.date()} -> {end.date()}...", flush=True)
    raw_trades = load_broker_trades(start, end)
    trades, _ = filter_clean_trades(raw_trades)
    longs = [t for t in trades if t["direction"] == "BUY"]
    shorts = [t for t in trades if t["direction"] == "SELL"]
    print(f"Clean: {len(trades)} (longs={len(longs)} shorts={len(shorts)})\n")

    grid = [
        (lb, disp, body)
        for lb in LOOKBACK_GRID
        for disp in DISPLACEMENT_GRID
        for body in BODY_GRID
    ]
    print(f"Sweep grid size: {len(grid)} combos\n")

    rows: list[dict] = []

    for lb, disp, body in grid:
        long_buckets: Counter[str] = Counter()
        short_buckets: Counter[str] = Counter()
        for t in longs:
            long_buckets[replay_one(t, lb, disp, body)] += 1
        for t in shorts:
            short_buckets[replay_one(t, lb, disp, body)] += 1

        l_replayed = sum(long_buckets[k] for k in ("CAUGHT", "WRONG", "MISSED"))
        s_replayed = sum(short_buckets[k] for k in ("CAUGHT", "WRONG", "MISSED"))

        l_catch_pct = (long_buckets["CAUGHT"] / l_replayed * 100) if l_replayed else 0.0
        l_wrong_pct = (long_buckets["WRONG"] / l_replayed * 100) if l_replayed else 0.0
        s_catch_pct = (short_buckets["CAUGHT"] / s_replayed * 100) if s_replayed else 0.0
        s_wrong_pct = (short_buckets["WRONG"] / s_replayed * 100) if s_replayed else 0.0

        rows.append({
            "lookback": lb,
            "disp_min": disp,
            "body_min": body,
            "l_caught": long_buckets["CAUGHT"],
            "l_wrong":  long_buckets["WRONG"],
            "l_missed": long_buckets["MISSED"],
            "l_n":      l_replayed,
            "l_catch_pct": l_catch_pct,
            "l_wrong_pct": l_wrong_pct,
            "s_caught": short_buckets["CAUGHT"],
            "s_wrong":  short_buckets["WRONG"],
            "s_missed": short_buckets["MISSED"],
            "s_n":      s_replayed,
            "s_catch_pct": s_catch_pct,
            "s_wrong_pct": s_wrong_pct,
        })

    # Print table
    hdr = (
        f"{'lb':>3} {'disp':>5} {'body':>5} | "
        f"{'L_C':>3} {'L_W':>3} {'L_M':>3} {'L_n':>3} {'L%C':>5} {'L%W':>5} | "
        f"{'S_C':>3} {'S_W':>3} {'S_M':>3} {'S_n':>3} {'S%C':>5} {'S%W':>5} | "
        f"{'score':>6}"
    )
    print(hdr)
    print("-" * len(hdr))

    def _score(row: dict) -> int:
        return (row["l_caught"] - row["l_wrong"]) + (row["s_caught"] - row["s_wrong"])

    for r in sorted(rows, key=_score, reverse=True):
        print(
            f"{r['lookback']:>3} {r['disp_min']:>5.2f} {r['body_min']:>5.2f} | "
            f"{r['l_caught']:>3} {r['l_wrong']:>3} {r['l_missed']:>3} {r['l_n']:>3} "
            f"{r['l_catch_pct']:>5.1f} {r['l_wrong_pct']:>5.1f} | "
            f"{r['s_caught']:>3} {r['s_wrong']:>3} {r['s_missed']:>3} {r['s_n']:>3} "
            f"{r['s_catch_pct']:>5.1f} {r['s_wrong_pct']:>5.1f} | "
            f"{_score(r):>+6}"
        )

    # Headline best combos
    print()
    print("=" * 78)
    print("Best combos (score = (L_C - L_W) + (S_C - S_W); higher = better)")
    print("=" * 78)
    best = max(rows, key=_score)
    print(f"  best: lookback={best['lookback']} disp={best['disp_min']} body={best['body_min']}  "
          f"score={_score(best):+d}  shorts caught={best['s_caught']}/{best['s_n']}")
    short_best = max(rows, key=lambda r: r["s_caught"] - r["s_wrong"])
    print(f"  best for SHORTS: lookback={short_best['lookback']} disp={short_best['disp_min']} body={short_best['body_min']}  "
          f"shorts caught={short_best['s_caught']}/{short_best['s_n']}  wrong={short_best['s_wrong']}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
