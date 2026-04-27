"""Bench the proposed counter-HTF-stack gate against historical broker trades.

Question: if we hard-skip any trade where the proposed direction opposes
ALL THREE of W1/D1/H4 bias, does that block more losers than winners?

Method: for each closed ICT_Bridge trade, slice cached M15 to bars
strictly before entry, compute W1/D1/H4 bias from existing detectors,
check whether trade direction opposes 3-of-3.

Bucket:
  PASS_NEW   — would still trade (HTF stack not 3-of-3 against, or
               trade direction agrees)
  BLOCKED    — would skip under new gate

Report winner-block vs loser-block rate; gate is safe to ship if
loser-block >> winner-block by a clear margin.

Usage:
    PYTHONUTF8=1 python scripts/bench_counter_htf_gate.py
"""

from __future__ import annotations

import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)

_BRIDGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BRIDGE))
sys.path.insert(1, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from scripts.bench_htf_rejection import (  # noqa: E402
    BROKER_TO_CACHE,
    filter_clean_trades,
    load_broker_trades,
    load_m15_cache,
    resample_ohlc,
)
from analysis.structure import detect_swings, classify_structure, get_current_bias  # noqa: E402
from core.types import Direction  # noqa: E402


def htf_stack_bias(df_m15_at: pd.DataFrame) -> tuple[Direction, Direction, Direction]:
    """Return (W1, D1, H4) bias as Direction triplet."""
    df_w1 = resample_ohlc(df_m15_at, "1W-MON")
    df_d1 = resample_ohlc(df_m15_at, "1D")
    df_h4 = resample_ohlc(df_m15_at, "4h")

    def _bias(df: pd.DataFrame, lookback: int) -> Direction:
        if len(df) < lookback + 2:
            return Direction.NEUTRAL
        try:
            sw = detect_swings(df, lookback=lookback)
            _, ev = classify_structure(sw)
            return get_current_bias(ev)
        except Exception:
            return Direction.NEUTRAL

    return (_bias(df_w1, 2), _bias(df_d1, 3), _bias(df_h4, 5))


def main() -> int:
    print("=" * 78)
    print("Counter-HTF-stack gate bench (proposed pre-gate)")
    print("=" * 78)
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 28, tzinfo=timezone.utc)
    raw = load_broker_trades(start, end)
    trades, _ = filter_clean_trades(raw)
    print(f"Clean trades: {len(trades)}")

    winners = [t for t in trades if t["pnl_usd"] > 0]
    losers = [t for t in trades if t["pnl_usd"] <= 0]
    print(f"Winners: {len(winners)}  Losers: {len(losers)}\n")

    rows = {"WIN": [], "LOSS": []}

    for trade in trades:
        cache_name = BROKER_TO_CACHE.get(trade["symbol"])
        if cache_name is None:
            continue
        df_full = load_m15_cache(cache_name)
        if df_full is None:
            continue
        ts = pd.Timestamp(trade["entry_time"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        if ts < df_full.index[0] or ts > df_full.index[-1]:
            continue
        df_at = df_full.loc[:ts]
        if len(df_at) >= 1 and df_at.index[-1] >= ts:
            df_at = df_at.iloc[:-1]
        if len(df_at) < 200:
            continue

        w1, d1, h4 = htf_stack_bias(df_at)
        trade_dir = Direction.BULLISH if trade["direction"] == "BUY" else Direction.BEARISH

        # Gate: 3-of-3 same direction AND trade is opposite -> BLOCKED
        biases = {w1, d1, h4}
        if len(biases) == 1 and Direction.NEUTRAL not in biases:
            stack_dir = next(iter(biases))
            blocked = stack_dir != trade_dir
        else:
            blocked = False

        bucket = "WIN" if trade["pnl_usd"] > 0 else "LOSS"
        rows[bucket].append({
            "ts": ts,
            "sym": trade["symbol"],
            "dir": trade["direction"],
            "pnl": trade["pnl_usd"],
            "w1": w1.value if hasattr(w1, "value") else str(w1),
            "d1": d1.value if hasattr(d1, "value") else str(d1),
            "h4": h4.value if hasattr(h4, "value") else str(h4),
            "blocked": blocked,
        })

    # Print
    for bucket in ("WIN", "LOSS"):
        rs = rows[bucket]
        n_total = len(rs)
        n_blocked = sum(1 for r in rs if r["blocked"])
        pnl_blocked = sum(r["pnl"] for r in rs if r["blocked"])
        pnl_total = sum(r["pnl"] for r in rs)
        print(f"\n--- {bucket}S ({n_total}) ---  total_pnl=${pnl_total:+.2f}")
        print(f"   would block: {n_blocked}/{n_total} = {(n_blocked/n_total*100 if n_total else 0):.1f}%  "
              f"blocked_pnl=${pnl_blocked:+.2f}")
        for r in rs:
            mark = "BLOCK" if r["blocked"] else "PASS "
            print(f"   {mark} {r['ts'].strftime('%m-%d %H:%M')} {r['sym']:<12} {r['dir']:<5} "
                  f"pnl=${r['pnl']:>+8.2f}  W1={r['w1']:<8} D1={r['d1']:<8} H4={r['h4']:<8}")

    # Headline
    print()
    print("=" * 78)
    w_blocked = sum(1 for r in rows["WIN"] if r["blocked"])
    l_blocked = sum(1 for r in rows["LOSS"] if r["blocked"])
    w_pnl = sum(r["pnl"] for r in rows["WIN"] if r["blocked"])
    l_pnl = sum(r["pnl"] for r in rows["LOSS"] if r["blocked"])
    print(f"Verdict: blocked {w_blocked} winners (${w_pnl:+.2f}) vs {l_blocked} losers (${l_pnl:+.2f})")
    print(f"Net dollar impact if gate had been live: ${-w_pnl - l_pnl:+.2f}  "
          f"(positive = gate would have made us money)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
