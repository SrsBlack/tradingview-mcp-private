"""
Volume profile perf bench (Option 2 from KB next-session prompt).

Question this answers: is `build_volume_profile` cheap enough to run every
cycle, or do we need a content-hash cache keyed on (symbol, last_M15_bar_ts)?

Method:
1. Load M15 cache for the 7 cached symbols (BTCUSD, ETHUSD, EURUSD,
   GBPUSD, SOLUSD, US500.cash, XAUUSD).
2. For each symbol take the last 96 bars (matches the bridge call site:
   `df_primary.iloc[-96:] if len(df_primary) >= 96 else df_primary`).
3. Call `build_volume_profile(window, buckets=30)` 1000x per symbol with
   time.perf_counter_ns timing.
4. Report per-symbol mean / p50 / p95 / max in microseconds, plus the
   per-cycle aggregate (sum across 7 symbols = one bridge cycle).
5. cProfile a 100-cycle aggregate run for hot-path attribution.

Decision rule (per next-session prompt):
  - <5ms/cycle  → document, close finding, no caching
  - 5-50ms      → document; cache optional
  - >50ms/cycle → propose content-hash cache keyed on (symbol, last_bar_ts)

Usage:
    PYTHONUTF8=1 python scripts/bench_volume_profile_perf.py
"""

from __future__ import annotations

import cProfile
import io
import pstats
import sys
import time
from pathlib import Path
from statistics import mean

import pandas as pd

sys.path.insert(0, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from analysis.volume_profile import build_volume_profile  # noqa: E402

CACHE_ROOT = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache")
SYMBOLS = ["BTCUSD", "ETHUSD", "EURUSD", "GBPUSD", "SOLUSD", "US500.cash", "XAUUSD"]
WINDOW_BARS = 96
BUCKETS = 30
ITERATIONS = 1000


def load_window(symbol: str) -> pd.DataFrame | None:
    parquet = CACHE_ROOT / symbol / "M15" / "data.parquet"
    if not parquet.exists():
        return None
    df = pd.read_parquet(parquet)
    if len(df) >= WINDOW_BARS:
        return df.iloc[-WINDOW_BARS:].copy()
    return df.copy()


def time_one(window: pd.DataFrame, iterations: int) -> dict[str, float]:
    samples_ns: list[int] = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        build_volume_profile(window, buckets=BUCKETS)
        samples_ns.append(time.perf_counter_ns() - t0)
    samples_ns.sort()
    n = len(samples_ns)
    return {
        "mean_us": mean(samples_ns) / 1000.0,
        "p50_us": samples_ns[n // 2] / 1000.0,
        "p95_us": samples_ns[int(n * 0.95)] / 1000.0,
        "max_us": samples_ns[-1] / 1000.0,
    }


def aggregate_cycle(windows: dict[str, pd.DataFrame], cycles: int) -> None:
    for _ in range(cycles):
        for w in windows.values():
            build_volume_profile(w, buckets=BUCKETS)


def main() -> int:
    print("=" * 72)
    print("Volume profile perf bench")
    print(f"  workload: {len(SYMBOLS)} symbols x {WINDOW_BARS} M15 bars x "
          f"{BUCKETS} buckets, {ITERATIONS} iters per symbol")
    print("=" * 72)

    windows: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        w = load_window(sym)
        if w is None:
            print(f"  [skip] {sym}: cache missing")
            continue
        windows[sym] = w
        print(f"  [load] {sym}: {len(w)} bars")

    if not windows:
        print("ERROR: no symbols loaded")
        return 1

    print()
    print(f"{'symbol':<14} {'mean_us':>10} {'p50_us':>10} {'p95_us':>10} {'max_us':>10}")
    print("-" * 56)

    per_symbol: dict[str, dict[str, float]] = {}
    for sym, w in windows.items():
        stats = time_one(w, ITERATIONS)
        per_symbol[sym] = stats
        print(f"{sym:<14} {stats['mean_us']:>10.2f} {stats['p50_us']:>10.2f} "
              f"{stats['p95_us']:>10.2f} {stats['max_us']:>10.2f}")

    cycle_mean_us = sum(s["mean_us"] for s in per_symbol.values())
    cycle_p95_us = sum(s["p95_us"] for s in per_symbol.values())
    print("-" * 56)
    print(f"{'CYCLE TOTAL':<14} {cycle_mean_us:>10.2f} {'':>10} "
          f"{cycle_p95_us:>10.2f} {'':>10}")
    print()
    print(f"Per-cycle mean: {cycle_mean_us / 1000.0:.3f} ms "
          f"(p95: {cycle_p95_us / 1000.0:.3f} ms)")

    print()
    print("=" * 72)
    print("cProfile (100 cycles aggregate)")
    print("=" * 72)
    pr = cProfile.Profile()
    pr.enable()
    aggregate_cycle(windows, cycles=100)
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(15)
    print(s.getvalue())

    print("=" * 72)
    print("Decision")
    print("=" * 72)
    if cycle_mean_us < 5_000:
        print(f"  cycle_mean = {cycle_mean_us / 1000.0:.3f} ms < 5 ms threshold")
        print("  -> NO caching needed. Document and close the finding.")
    elif cycle_mean_us < 50_000:
        print(f"  cycle_mean = {cycle_mean_us / 1000.0:.3f} ms in 5-50ms band")
        print("  -> Caching optional. Document and decide based on amortized cost.")
    else:
        print(f"  cycle_mean = {cycle_mean_us / 1000.0:.3f} ms > 50 ms threshold")
        print("  -> SHIP content-hash cache keyed on (symbol, last_M15_bar_ts).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
