"""
Phase 4 backtest: how often would MultiTF_CRT (D1+H4 both fire) trigger?

If it fires too often (>30% of cycles), +5 is just flat inflation.
If it fires rarely (<5%), +5 is appropriate conviction premium.
Sweet spot: 5-25% of cycles.

Method: walk M15 bars forward; at each cycle endpoint, ask "does the most
recent CLOSED D1 bar fire CRT, AND does the most recent CLOSED H4 bar
fire CRT, in the lookback=1 sense the bridge actually uses?"
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from analysis.ict.advanced import detect_crt  # noqa: E402

CACHE_ROOT = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache")
SYMBOLS = ["XAUUSD", "EURUSD", "GBPUSD", "BTCUSD", "ETHUSD"]
STEP_M15 = 96  # one cycle per 24h


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
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()


def _bench(sym: str) -> dict:
    df_m15 = _load_m15(sym)
    if df_m15 is None or len(df_m15) < 1000:
        return {"symbol": sym, "skipped": True}

    df_h4 = _resample(df_m15, "4h")
    df_d1 = _resample(df_m15, "1D")

    counts = Counter()
    cycles = 0

    # Walk M15 in 24h steps starting from when we have >=10 D1 bars
    for end in range(960, len(df_m15) - 1, STEP_M15):
        ts = df_m15.index[end]

        # Bridge uses df_d1[:-1] and df_htf_closed (H4[:-1])
        # We want only the last completed CRT setup at the latest closed bar
        d1_window = df_d1.loc[:ts]
        h4_window = df_h4.loc[:ts]
        if len(d1_window) < 5 or len(h4_window) < 5:
            continue

        # Pass only the LAST 3 bars (the bridge passes more, but only the
        # final i==len-1 setup is "fresh" for current decision).
        d1_setups = detect_crt(d1_window.iloc[:-1].iloc[-3:], lookback=1, tf_label="D1")
        h4_setups = detect_crt(h4_window.iloc[:-1].iloc[-3:], lookback=1, tf_label="H4")

        # Did either fire AT the last bar of the window (the freshest)?
        d1_fresh = bool(d1_setups and d1_setups[-1].sweep_bar_index == 2)
        h4_fresh = bool(h4_setups and h4_setups[-1].sweep_bar_index == 2)

        cycles += 1
        if d1_fresh and h4_fresh:
            counts["both"] += 1
        elif d1_fresh:
            counts["d1_only"] += 1
        elif h4_fresh:
            counts["h4_only"] += 1
        else:
            counts["none"] += 1

    return {"symbol": sym, "cycles": cycles, "counts": dict(counts)}


def main() -> None:
    print("MultiTF_CRT firing frequency (fresh setups on most-recent closed bar)")
    print("=" * 70)
    grand = Counter()
    grand_cycles = 0
    for sym in SYMBOLS:
        r = _bench(sym)
        if r.get("skipped"):
            print(f"[{sym}] SKIPPED")
            continue
        c = r["counts"]
        n = r["cycles"]
        both_pct = 100 * c.get("both", 0) / n if n else 0.0
        d1_pct = 100 * c.get("d1_only", 0) / n if n else 0.0
        h4_pct = 100 * c.get("h4_only", 0) / n if n else 0.0
        none_pct = 100 * c.get("none", 0) / n if n else 0.0
        print(f"[{sym}] n={n} both={both_pct:.1f}% d1_only={d1_pct:.1f}% h4_only={h4_pct:.1f}% none={none_pct:.1f}%")
        for k, v in c.items():
            grand[k] += v
        grand_cycles += n

    print("-" * 70)
    if grand_cycles:
        bp = 100 * grand.get("both", 0) / grand_cycles
        print(f"AGGREGATE: n={grand_cycles}  D1+H4 both fresh = {bp:.1f}% of cycles")
        if 5.0 <= bp <= 25.0:
            print(f"  → SWEET SPOT — +5 synergy is appropriate conviction premium")
        elif bp < 5.0:
            print(f"  → RARE — synergy fires too seldom; consider +7-10 weight")
        else:
            print(f"  → TOO COMMON — synergy is flat inflation; consider +2-3 weight or skip")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    main()
