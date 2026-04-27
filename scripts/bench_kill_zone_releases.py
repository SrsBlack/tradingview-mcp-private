"""
Verify Claude's downstream gates catch the 3 losers KILL_ZONE used to
catch. Companion to bench_winners_not_blocked.py.

Question: when we softened KILL_ZONE to let Grade-A + (sweep OR
displacement) through, 3 historical losers that the old gate would have
hard-blocked now reach Claude. Does Claude's reasoning step still
reject them, or does it rubber-stamp them?

Method: for each of the 3 specific losers, replay full
ClaudeDecisionMaker.evaluate() — pre-gate + Claude API + post-gate
reasoning checks. Report final action (SKIP vs ENTRY) and reasoning.

Cost: ~3 Anthropic API calls. Should be a few cents.

Usage:
    PYTHONUTF8=1 python scripts/bench_kill_zone_releases.py
"""

from __future__ import annotations

import sys
import warnings
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

from bridge.config import get_bridge_config  # noqa: E402
from bridge.ict_pipeline import ICTPipeline  # noqa: E402
from bridge.claude_decision import ClaudeDecisionMaker  # noqa: E402

CACHE_ROOT = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache")

# The 3 losers that used to be blocked by KILL_ZONE and now PASS the
# pre-gate after commit be905bf. Sourced from the broker-truth bench
# output scripts/bench_winners_not_blocked_2026-04-26.txt — the trades
# that flipped from BLOCK [KILL_ZONE] in the pre-fix run to PASS in
# the post-fix run.
TARGET_TRADES = [
    {
        "symbol": "XAUUSD",
        "tv_symbol": "OANDA:XAUUSD",
        "cache_name": "XAUUSD",
        "direction": "BUY",
        "entry_time": pd.Timestamp("2026-04-20T05:22:20", tz="UTC"),
        "actual_pnl": -628.24,
    },
    {
        "symbol": "GBPUSD",
        "tv_symbol": "OANDA:GBPUSD",
        "cache_name": "GBPUSD",
        "direction": "BUY",
        "entry_time": pd.Timestamp("2026-04-20T10:24:08", tz="UTC"),
        "actual_pnl": -151.90,
    },
    {
        "symbol": "US500",
        "tv_symbol": "CAPITALCOM:US500",
        "cache_name": "US500.cash",
        "direction": "BUY",
        "entry_time": pd.Timestamp("2026-04-21T18:46:24", tz="UTC"),
        "actual_pnl": -13.65,
    },
]


def load_m15(cache_name: str) -> pd.DataFrame:
    df = pd.read_parquet(CACHE_ROOT / cache_name / "M15" / "data.parquet")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()


def build_dfs_at(df_m15_full: pd.DataFrame, entry_ts: pd.Timestamp) -> dict[str, pd.DataFrame]:
    df_m15 = df_m15_full.loc[:entry_ts]
    if df_m15.index[-1] >= entry_ts:
        df_m15 = df_m15.iloc[:-1]
    return {
        "M15": df_m15.tail(500).copy(),
        "H1":  resample_ohlc(df_m15, "1h").tail(500).copy(),
        "H4":  resample_ohlc(df_m15, "4h").tail(500).copy(),
        "D1":  resample_ohlc(df_m15, "1D").tail(200).copy(),
        "W1":  resample_ohlc(df_m15, "1W-MON").tail(60).copy(),
    }


class _StubMT5Data:
    def __init__(self, dfs: dict[str, pd.DataFrame]):
        self._dfs = dfs

    def collect_data(self, symbol: str) -> dict[str, pd.DataFrame]:
        return {k: v.copy() for k, v in self._dfs.items()}


def evaluate_one(t: dict) -> dict:
    df_full = load_m15(t["cache_name"])
    dfs = build_dfs_at(df_full, t["entry_time"])

    cfg = get_bridge_config()
    pipe = ICTPipeline(config=cfg)
    pipe.client = None
    pipe._use_mt5 = True
    pipe._mt5_data = _StubMT5Data(dfs)

    analysis = pipe.analyze_symbol(t["tv_symbol"])
    if analysis.error:
        return {"trade": t, "error": analysis.error}

    decision_maker = ClaudeDecisionMaker()
    # Bypass the cache so we get a fresh evaluation each call
    decision_maker._decision_cache = {}

    decision = decision_maker.evaluate(analysis)
    return {
        "trade": t,
        "analysis_grade": getattr(analysis, "grade", None),
        "analysis_score": getattr(analysis, "total_score", None),
        "analysis_direction": getattr(analysis, "direction", None),
        "decision_action": decision.action,
        "decision_confidence": getattr(decision, "confidence", None),
        "decision_model": getattr(decision, "model_used", None),
        "decision_reasoning": getattr(decision, "reasoning", None),
        "decision_entry": getattr(decision, "entry_price", None),
        "decision_sl": getattr(decision, "sl_price", None),
        "decision_tp": getattr(decision, "tp_price", None),
    }


def main() -> int:
    print("=" * 78)
    print("KILL_ZONE-released-losers downstream check")
    print("=" * 78)
    print()
    print("These 3 losers were blocked by KILL_ZONE pre-gate before commit be905bf.")
    print("After the softened bypass (Grade-A + (displacement OR sweep)) they reach")
    print("Claude. Does Claude's reasoning step still reject them?")
    print()

    results = []
    for i, t in enumerate(TARGET_TRADES, 1):
        print(f"[{i}/{len(TARGET_TRADES)}] {t['entry_time']} {t['symbol']} {t['direction']} actual_pnl=${t['actual_pnl']:+.2f}", flush=True)
        try:
            r = evaluate_one(t)
        except Exception as e:
            print(f"      CRASH: {type(e).__name__}: {e}", flush=True)
            results.append({"trade": t, "error": str(e)})
            continue
        results.append(r)
        if r.get("error"):
            print(f"      ERROR: {r['error']}", flush=True)
            continue
        print(f"      grade={r['analysis_grade']} score={r['analysis_score']} dir={r['analysis_direction']}", flush=True)
        print(f"      decision={r['decision_action']} via {r['decision_model']} confidence={r['decision_confidence']}", flush=True)
        reason = (r['decision_reasoning'] or "").replace("\n", " ")[:200]
        print(f"      reason: {reason}", flush=True)
        print(flush=True)

    # Aggregate
    print("=" * 78)
    print("Aggregate")
    print("=" * 78)
    n = len(results)
    skipped = sum(1 for r in results if not r.get("error") and r.get("decision_action") == "SKIP")
    entered = sum(1 for r in results if not r.get("error") and r.get("decision_action") in ("ENTRY", "BUY", "SELL"))
    errored = sum(1 for r in results if r.get("error"))
    print(f"  Total: {n}")
    print(f"  Claude rejected (SKIP):  {skipped}")
    print(f"  Claude approved (ENTRY): {entered}")
    print(f"  Errored:                 {errored}")
    print()

    # Verdict
    print("=" * 78)
    print("Verdict")
    print("=" * 78)
    saved_pnl = sum(-r["trade"]["actual_pnl"] for r in results
                    if not r.get("error") and r.get("decision_action") == "SKIP")
    leaked_pnl = sum(r["trade"]["actual_pnl"] for r in results
                     if not r.get("error") and r.get("decision_action") in ("ENTRY", "BUY", "SELL"))
    print(f"  Losses saved by Claude rejecting:  ${saved_pnl:+.2f}")
    print(f"  Losses leaked by Claude approving: ${leaked_pnl:+.2f}")
    print(f"  Net effect of softened KILL_ZONE:  ${saved_pnl + leaked_pnl:+.2f} on this 3-trade subset")
    print()
    if entered == 0:
        print("  ALL 3 losers caught by Claude downstream. KILL_ZONE softening is")
        print("  net-positive (winners pass + Claude still catches losers).")
        return 0
    if entered == n:
        print("  ALL 3 losers approved by Claude. KILL_ZONE was the only safety net for")
        print("  these trades — softening shifted losses through unchecked. RECONSIDER.")
        return 2
    print(f"  MIXED — Claude caught {skipped}/{n}. Acceptable if winners-saved > losers-leaked.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
