"""
Multi-TF CRT score-formula backtest harness.

Question this answers: does adding D1+H4 CRT factors on top of M15 inflate
total_score under the current +2.5/factor cap=+10 formula vs. a per-TF
weighted formula (D1=4, H4=3, M15=2 cap=+10)?

Method:
1. Load cached M15 OHLCV for representative symbols.
2. Resample to D1 and H4.
3. Walk forward in 100-bar chunks. At each chunk endpoint, call detect_crt on
   each TF independently (matching the bridge's step 8f behavior).
4. Synthesize a representative pre-CRT advanced_factors list (3 generic factors)
   to model a typical mid-cap cycle.
5. Compute total_score under both formulas and report deltas.

Usage:
    PYTHONUTF8=1 python scripts/bench_multi_tf_crt.py
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

# trading-ai-v2 import path
sys.path.insert(0, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from analysis.ict.advanced import detect_crt  # noqa: E402

CACHE_ROOT = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache")

# Representative symbols across asset classes
SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "BTCUSD", "ETHUSD"]

# Walk parameters
WINDOW_M15 = 200          # M15 bars per cycle (≈50h history)
STEP_M15 = 96             # advance 24h between cycles
MIN_CYCLES_PER_SYMBOL = 50

# Synthesized "typical" non-CRT advanced_factor count to model cap pressure.
# Real cycles emit 0–8 advanced factors before CRT. Test 3 baseline counts.
BASELINE_FACTOR_COUNTS = [0, 3, 6]


def _load_m15(sym: str) -> pd.DataFrame | None:
    p = CACHE_ROOT / sym / "M15" / "data.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)
    return df


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()


def _score_old(n_advanced_factors: int) -> float:
    """+2.5 per factor capped at +10."""
    return min(n_advanced_factors * 2.5, 10.0)


def _score_new(crt_by_tf: dict[str, int], n_other_factors: int) -> float:
    """Per-TF weighted CRT (D1=4, H4=3, M15=2) + non-CRT factors at +2.5 each, cap=+10."""
    weights = {"D1": 4.0, "H4": 3.0, "M15": 2.0}
    crt_bonus = 0.0
    for tf, n in crt_by_tf.items():
        if n > 0:
            crt_bonus += weights.get(tf, 2.0)
    other_bonus = n_other_factors * 2.5
    return min(crt_bonus + other_bonus, 10.0)


def _bench_symbol(sym: str) -> dict:
    df_m15 = _load_m15(sym)
    if df_m15 is None or len(df_m15) < WINDOW_M15 + 10:
        return {"symbol": sym, "skipped": True, "reason": "no data"}

    df_h4 = _resample(df_m15, "4h")
    df_d1 = _resample(df_m15, "1D")

    cycles = 0
    fire_counts = Counter()  # how many cycles emit each TF combo
    deltas = {f"baseline={n}": [] for n in BASELINE_FACTOR_COUNTS}

    for end in range(WINDOW_M15, len(df_m15) - 1, STEP_M15):
        m15_window = df_m15.iloc[end - WINDOW_M15: end]
        ts_end = m15_window.index[-1]

        # H4 / D1 windows up through the same timestamp
        h4_window = df_h4.loc[:ts_end].iloc[-50:]
        d1_window = df_d1.loc[:ts_end].iloc[-30:]

        if len(h4_window) < 3 or len(d1_window) < 3:
            continue

        # Match bridge: H4 uses [:-1] closed bars; D1 uses [:-1] closed bars
        d1_setups = detect_crt(d1_window.iloc[:-1], lookback=1, tf_label="D1")
        h4_setups = detect_crt(h4_window.iloc[:-1], lookback=1, tf_label="H4")
        m15_setups = detect_crt(m15_window, lookback=1, tf_label="M15")

        crt_by_tf = {
            "D1": len(d1_setups),
            "H4": len(h4_setups),
            "M15": len(m15_setups),
        }
        active = tuple(tf for tf in ("D1", "H4", "M15") if crt_by_tf[tf] > 0)
        fire_counts[active] += 1
        cycles += 1

        # CRT factor count = 1 per TF that fires (one factor name like CRT_D1(N))
        n_crt_factors = sum(1 for v in crt_by_tf.values() if v > 0)

        for n_baseline in BASELINE_FACTOR_COUNTS:
            old = _score_old(n_baseline + n_crt_factors)
            new = _score_new(crt_by_tf, n_baseline)
            deltas[f"baseline={n_baseline}"].append(new - old)

    if cycles < MIN_CYCLES_PER_SYMBOL:
        return {"symbol": sym, "skipped": True, "reason": f"only {cycles} cycles"}

    return {
        "symbol": sym,
        "cycles": cycles,
        "fire_counts": dict(fire_counts),
        "deltas": {k: {
            "n": len(v),
            "mean": sum(v) / len(v) if v else 0.0,
            "max_pos": max(v) if v else 0.0,
            "max_neg": min(v) if v else 0.0,
            "nonzero_pct": 100.0 * sum(1 for x in v if x != 0) / len(v) if v else 0.0,
        } for k, v in deltas.items()},
    }


def main() -> None:
    print("=" * 70)
    print("Multi-TF CRT score-formula backtest")
    print("=" * 70)

    results = []
    for sym in SYMBOLS:
        print(f"\n[{sym}] running...", flush=True)
        r = _bench_symbol(sym)
        results.append(r)
        if r.get("skipped"):
            print(f"  SKIPPED: {r['reason']}")
            continue
        print(f"  cycles: {r['cycles']}")
        print(f"  CRT firing combos (top): {sorted(r['fire_counts'].items(), key=lambda x: -x[1])[:6]}")
        for baseline_label, stats in r["deltas"].items():
            print(f"  {baseline_label:14s}: mean={stats['mean']:+.2f} max+={stats['max_pos']:+.1f} max-={stats['max_neg']:+.1f} nonzero={stats['nonzero_pct']:.0f}%")

    # Aggregate decision gate
    print("\n" + "=" * 70)
    print("DECISION GATE (Phase 3 ships if avg |delta| <= 2.0 across all baselines)")
    print("=" * 70)
    overall_means = []
    for baseline_label in (f"baseline={n}" for n in BASELINE_FACTOR_COUNTS):
        means = [r["deltas"][baseline_label]["mean"]
                 for r in results if not r.get("skipped")]
        if not means:
            continue
        agg_mean = sum(means) / len(means)
        max_abs = max(abs(m) for m in means)
        verdict = "PASS" if max_abs <= 2.0 else "FAIL"
        overall_means.append((baseline_label, agg_mean, max_abs, verdict))
        print(f"  {baseline_label:14s}: avg_mean={agg_mean:+.2f}  max|symbol_mean|={max_abs:.2f}  → {verdict}")

    overall_pass = all(v == "PASS" for *_, v in overall_means)
    print(f"\nOVERALL: {'PASS — safe to ship per-TF weighting' if overall_pass else 'FAIL — tune weights down'}")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    main()
