"""
Counter-D1-CRT trade-gate evidence bench (Followup D — evidence-first).

Question this answers: do trades that enter OPPOSITE to an active D1 CRT
underperform aligned trades by enough margin to justify a hard gate?

Method:
1. Read closed ICT_Bridge trades from ~/.tradingview-mcp/trading_ledger.db.
2. For each trade with M15 cache coverage at entry_time:
   a. Slice cache to bars strictly before entry_time.
   b. Resample to D1.
   c. Match the bridge's call site: detect_crt(df_d1[:-1], lookback=1, tf_label="D1").
   d. Take the MOST RECENT D1 CRT setup (if any) as the "active" one.
   e. Classify:
      - aligned: trade direction == CRT direction
      - counter: trade direction != CRT direction
      - no_crt:  no active D1 CRT
3. Compute WR + mean R-multiple + total P&L per bucket.
4. Decision per next-session prompt:
   - SHIP gate iff N(counter) >= 30 AND WR(counter) <= WR(all) - 10pp
   - Else archive result with current N + remaining-to-threshold count.

Usage:
    PYTHONUTF8=1 python scripts/bench_counter_d1_crt.py
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

# trading-ai-v2 import path
sys.path.insert(0, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from analysis.ict.advanced import detect_crt  # noqa: E402

LEDGER_PATH = Path.home() / ".tradingview-mcp" / "trading_ledger.db"
CACHE_ROOT = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache")

# Symbol normalisation: ledger names → cache directory names. Drop entries
# whose symbol has no cache file (DOGEUSD/UKOIL/YM1!/DAX/USDJPY/etc).
SYM_TO_CACHE = {
    "BTCUSD": "BTCUSD",
    "ETHUSD": "ETHUSD",
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "SOLUSD": "SOLUSD",
    "US500":  "US500.cash",
    "US500.cash": "US500.cash",
    "XAUUSD": "XAUUSD",
}

# Decision thresholds
N_MIN = 30                       # minimum sample size to ship the gate
WR_DELTA_MIN_PP = 10.0           # counter-WR must be >= 10pp lower than overall WR


def _load_m15(sym: str) -> pd.DataFrame | None:
    cache_sym = SYM_TO_CACHE.get(sym)
    if not cache_sym:
        return None
    p = CACHE_ROOT / cache_sym / "M15" / "data.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def _resample_d1(df_m15: pd.DataFrame) -> pd.DataFrame:
    return df_m15.resample("1D").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()


def _classify_trade(trade: dict, df_m15: pd.DataFrame) -> dict:
    """
    Replay D1 CRT detection at trade entry_time. Returns trade dict
    enriched with 'crt_status' ∈ {aligned, counter, no_crt} and
    'active_crt_direction' (or None).
    """
    entry_ts = pd.Timestamp(trade["entry_time"])
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.tz_localize("UTC")
    else:
        entry_ts = entry_ts.tz_convert("UTC")

    # Slice cache to bars STRICTLY before entry. Avoid look-ahead.
    df_pre = df_m15[df_m15.index < entry_ts]
    if len(df_pre) < 80:  # need >= ~3 days of M15 to resample 3 D1 bars
        return {**trade, "crt_status": "skip_insufficient", "active_crt_direction": None}

    df_d1 = _resample_d1(df_pre)
    if len(df_d1) < 3:
        return {**trade, "crt_status": "skip_insufficient", "active_crt_direction": None}

    # Match the bridge's call: detect_crt(df_d1[:-1], lookback=1, tf_label="D1").
    # df_d1[:-1] excludes the live (still-forming) D1 bar.
    setups = detect_crt(df_d1.iloc[:-1], lookback=1, tf_label="D1")
    if not setups:
        return {**trade, "crt_status": "no_crt", "active_crt_direction": None}

    # Most recent setup = "active" (the bridge does the same — emits the count
    # and the most recent setup dominates the directional reading).
    latest = setups[-1]
    crt_dir = latest.direction.value.upper()  # "BULLISH" or "BEARISH"
    trade_dir = trade["direction"].upper()
    if (trade_dir == "BUY" and crt_dir == "BULLISH") or (
        trade_dir == "SELL" and crt_dir == "BEARISH"
    ):
        status = "aligned"
    else:
        status = "counter"

    return {**trade, "crt_status": status, "active_crt_direction": crt_dir}


def _bucket_stats(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "wins": 0, "losses": 0, "scratch": 0,
                "wr_pct": 0.0, "mean_r": 0.0, "total_pnl": 0.0}
    wins = sum(1 for r in rows if (r.get("pnl_usd") or 0) > 0)
    losses = sum(1 for r in rows if (r.get("pnl_usd") or 0) < 0)
    rs = [r.get("r_multiple") or 0.0 for r in rows]
    pnls = [r.get("pnl_usd") or 0.0 for r in rows]
    n_decided = wins + losses
    wr_pct = (100.0 * wins / n_decided) if n_decided else 0.0
    return {
        "n": len(rows),
        "wins": wins,
        "losses": losses,
        "scratch": len(rows) - wins - losses,
        "wr_pct": wr_pct,
        "mean_r": sum(rs) / len(rs) if rs else 0.0,
        "total_pnl": sum(pnls),
    }


def main() -> None:
    print("=" * 72)
    print("Counter-D1-CRT trade-gate evidence bench")
    print("=" * 72)

    if not LEDGER_PATH.exists():
        print(f"ERROR: ledger not found at {LEDGER_PATH}")
        sys.exit(1)

    con = sqlite3.connect(LEDGER_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute(
        "SELECT ticket, symbol, direction, entry_price, exit_price, "
        "entry_time, exit_time, pnl_usd, r_multiple, signal_grade, status "
        "FROM trades WHERE status='closed' AND strategy_name='ICT_Bridge' "
        "ORDER BY entry_time"
    ).fetchall()
    con.close()
    raw_trades = [dict(r) for r in rows]
    print(f"Closed ICT_Bridge trades: {len(raw_trades)}")

    # Cache + classify
    classified: list[dict] = []
    skipped_no_cache = 0
    skipped_insufficient = 0
    df_cache: dict[str, pd.DataFrame | None] = {}
    for t in raw_trades:
        sym = t["symbol"]
        if sym not in df_cache:
            df_cache[sym] = _load_m15(sym)
        df = df_cache[sym]
        if df is None:
            skipped_no_cache += 1
            continue
        c = _classify_trade(t, df)
        if c["crt_status"] == "skip_insufficient":
            skipped_insufficient += 1
            continue
        classified.append(c)

    print(f"  skipped (no cache for symbol):   {skipped_no_cache}")
    print(f"  skipped (insufficient history): {skipped_insufficient}")
    print(f"  classified:                      {len(classified)}")

    # Bucket
    aligned = [t for t in classified if t["crt_status"] == "aligned"]
    counter = [t for t in classified if t["crt_status"] == "counter"]
    no_crt = [t for t in classified if t["crt_status"] == "no_crt"]

    s_all = _bucket_stats(classified)
    s_aligned = _bucket_stats(aligned)
    s_counter = _bucket_stats(counter)
    s_no_crt = _bucket_stats(no_crt)

    print()
    print("Bucket breakdown")
    print("-" * 72)
    fmt = "{:14s} N={:3d} W={:3d} L={:3d} sc={:2d} WR={:5.1f}% meanR={:+.2f} P&L=${:+.2f}"
    print(fmt.format("ALL classified", s_all["n"], s_all["wins"], s_all["losses"],
                     s_all["scratch"], s_all["wr_pct"], s_all["mean_r"], s_all["total_pnl"]))
    print(fmt.format("aligned", s_aligned["n"], s_aligned["wins"], s_aligned["losses"],
                     s_aligned["scratch"], s_aligned["wr_pct"], s_aligned["mean_r"], s_aligned["total_pnl"]))
    print(fmt.format("counter", s_counter["n"], s_counter["wins"], s_counter["losses"],
                     s_counter["scratch"], s_counter["wr_pct"], s_counter["mean_r"], s_counter["total_pnl"]))
    print(fmt.format("no_crt", s_no_crt["n"], s_no_crt["wins"], s_no_crt["losses"],
                     s_no_crt["scratch"], s_no_crt["wr_pct"], s_no_crt["mean_r"], s_no_crt["total_pnl"]))

    print()
    print("Counter sample (entry_time, symbol, dir, active_CRT, R, P&L):")
    for t in counter:
        print(f"  {t['entry_time'][:19]} {t['symbol']:8s} {t['direction']:4s} "
              f"vs {t['active_crt_direction']:8s}  R={t['r_multiple']:+.2f}  ${t['pnl_usd']:+.2f}")

    print()
    print("Decision")
    print("-" * 72)
    n_counter = s_counter["n"]
    wr_delta = s_all["wr_pct"] - s_counter["wr_pct"]
    print(f"Sample size required (N_MIN):           {N_MIN}")
    print(f"Counter-CRT trades observed:             {n_counter}")
    print(f"Min WR delta required (pp):              {WR_DELTA_MIN_PP}")
    print(f"Observed WR delta (all - counter):       {wr_delta:+.1f} pp")
    print()
    if n_counter < N_MIN:
        remaining = N_MIN - n_counter
        print(f"DEFER: sample size insufficient. Need {remaining} more counter-CRT")
        print(f"trades before this bench has the statistical power to ship a gate.")
        print(f"Re-run after every ~10 new live trades.")
    elif wr_delta < WR_DELTA_MIN_PP:
        print(f"DEFER: counter-CRT trades do NOT underperform by {WR_DELTA_MIN_PP}pp.")
        print(f"No asymmetry → no gate. Re-bench in next quarter.")
    else:
        print(f"SHIP: counter-CRT N>={N_MIN} AND WR delta >= {WR_DELTA_MIN_PP}pp.")
        print(f"Add a -5 penalty gate in synergy_scorer._GATE_CHECKS.")


if __name__ == "__main__":
    main()
