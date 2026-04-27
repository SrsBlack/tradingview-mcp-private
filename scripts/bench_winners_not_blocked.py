"""
Winner-blocking regression test — BROKER-TRUTH + RESTART-CLUSTER FILTER.

Question this answers: would today's bridge pre-gates have blocked any
of our actual considered-decision winners? Sources P&L from FTMO broker
history (mt5.history_deals_get) NOT the trade ledger because the ledger
is known-corrupt — see feedback_ledger_unreliable.md and
scripts/_audit_ledger_vs_broker.py.

Also filters out restart-cluster contamination: 2026-04-21 had 22+
ICT_Bridge entries in 4 hours caused by repeatedly restarting the
bridge while adding KB knowledge. Those weren't considered decisions —
they were adoption + reconciliation artifacts. See
filter_clean_trades() for the threshold.

Method:
1. Pull every closed ICT_Bridge position from MT5 broker history.
   A "trade" = open-deal + close-deal(s) sharing position_id, where
   the open-deal comment contains "ICT_Bridge".
2. Compute broker net P&L = sum(deal.profit for deal in position).
   Bucket: winner if net > 0, loser if net <= 0.
3. For each cacheable trade, replay today's pipeline at entry_time:
   - Slice cached M15 to bars STRICTLY BEFORE entry_time.
   - Resample to D1/H4/H1/W1.
   - Inject via _StubMT5Data into ICTPipeline.
   - Run analyze_symbol() -> SymbolAnalysis.
   - Run ClaudeDecisionMaker._pre_gate(analysis) -> reason or None.
4. Bucket: PASS / BLOCK. Real-block requires trade direction to agree
   with gate's bias direction (avoid mis-counting bias-mismatch cases).
5. Report winner-block rate and loser-block rate.

Caveats:
- Cache covers only 7 symbols. Trades on DOGEUSD/UKOIL/JPY-pairs/DAX/
  YM/GER40/US100 are skipped.
- Pre-gate is deterministic; Claude's final decision is NOT replayed.
  PASS here means today's gates would have let the trade reach Claude.

Decision rule:
- 0 real-blocked winners -> PASS (no regression).
- 1+ real-blocked winners -> FAIL — list each, propose fix.

Usage:
    PYTHONUTF8=1 python scripts/bench_winners_not_blocked.py
"""

from __future__ import annotations

import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# Suppress the pandas tz/PeriodArray warning that floods the output —
# fires from analysis/liquidity.py for every trade replayed.
warnings.filterwarnings(
    "ignore",
    message="Converting to PeriodArray/Index representation will drop timezone information",
    category=UserWarning,
)

# Import paths so `bridge.*` and `analysis.*` resolve even from scripts/
_BRIDGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BRIDGE_ROOT))
sys.path.insert(1, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from bridge.config import get_bridge_config  # noqa: E402
from bridge.ict_pipeline import ICTPipeline  # noqa: E402
from bridge.claude_decision import ClaudeDecisionMaker  # noqa: E402

import MetaTrader5 as mt5  # noqa: E402

# FTMO-Demo creds — same as live bridge (see refresh_cache.py for context)
MT5_LOGIN = 1513140458
MT5_PASSWORD = "L!$q1k@4Z"
MT5_SERVER = "FTMO-Demo"
MT5_PATH = "C:/Program Files/METATRADER5.1/terminal64.exe"

CACHE_ROOT = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache")

# Broker symbol (from broker deal.symbol) -> M15 cache directory name.
# Symbols not in this map are skipped (no replay possible).
BROKER_TO_CACHE = {
    "BTCUSD":     "BTCUSD",
    "ETHUSD":     "ETHUSD",
    "SOLUSD":     "SOLUSD",
    "EURUSD":     "EURUSD",
    "GBPUSD":     "GBPUSD",
    "XAUUSD":     "XAUUSD",
    "US500.cash": "US500.cash",
}

# Broker symbol -> TV-style symbol the pipeline expects in analyze_symbol().
BROKER_TO_TV = {
    "BTCUSD":     "BITSTAMP:BTCUSD",
    "ETHUSD":     "COINBASE:ETHUSD",
    "SOLUSD":     "COINBASE:SOLUSD",
    "EURUSD":     "OANDA:EURUSD",
    "GBPUSD":     "OANDA:GBPUSD",
    "XAUUSD":     "OANDA:XAUUSD",
    "US500.cash": "CAPITALCOM:US500",
}


# ---------------------------------------------------------------------------
# Broker history -> trade list
# ---------------------------------------------------------------------------

def load_broker_trades(start: datetime, end: datetime) -> list[dict]:
    """Pull every closed ICT_Bridge position from FTMO history.
    Returns list of dicts with broker-truth fields."""
    if not mt5.initialize(path=MT5_PATH, login=MT5_LOGIN,
                          password=MT5_PASSWORD, server=MT5_SERVER):
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    try:
        deals = mt5.history_deals_get(start, end) or []
    finally:
        mt5.shutdown()

    by_pos: dict[int, list] = defaultdict(list)
    for d in deals:
        by_pos[d.position_id].append(d)

    trades: list[dict] = []
    for pos_id, ds in by_pos.items():
        opens = [d for d in ds if d.entry == 0]
        closes = [d for d in ds if d.entry == 1]
        if not opens or not closes:
            continue  # still-open or no-open data
        od = opens[0]
        # ICT_Bridge filter — comment is on the OPENING deal
        if "ICT_Bridge" not in (od.comment or ""):
            continue
        net_profit = sum(d.profit for d in ds)
        # Direction comes from open deal's type: 0=BUY, 1=SELL
        direction = "BUY" if od.type == 0 else "SELL" if od.type == 1 else None
        if direction is None:
            continue
        trades.append({
            "position_id": pos_id,
            "symbol": od.symbol,
            "direction": direction,
            "entry_price": od.price,
            "entry_time": datetime.fromtimestamp(od.time, tz=timezone.utc),
            "exit_price": closes[-1].price,
            "exit_time": datetime.fromtimestamp(closes[-1].time, tz=timezone.utc),
            "volume": od.volume,
            "pnl_usd": net_profit,
            "comment": od.comment,
        })
    trades.sort(key=lambda t: t["entry_time"])
    return trades


# ---------------------------------------------------------------------------
# Restart-cluster filter
# ---------------------------------------------------------------------------
# 2026-04-26: discovered that the broker has 50 ICT_Bridge positions but the
# bridge only made ~22 considered decisions. The other ~27 are bridge-restart
# artifacts: each restart triggered fresh adoption + reconciliation passes
# that fired entries on the next cycle. User confirmed this was caused by
# repeatedly restarting the bridge while adding KB knowledge.
#
# Filter rule: any trade that's part of a tight cluster (>=CLUSTER_THRESHOLD
# entries within CLUSTER_WINDOW) is presumed restart contamination and
# excluded. Tuned to drop the 2026-04-21 17:00-22:00 cluster (22 entries in
# 4h) while keeping isolated trades.
CLUSTER_WINDOW = pd.Timedelta(minutes=30)
CLUSTER_THRESHOLD = 4  # 4+ entries in any 30-min window = cluster


def is_restart_cluster(trade: dict, all_trades: list[dict]) -> bool:
    """A trade is in a 'cluster' if there are >=CLUSTER_THRESHOLD trades
    (including itself) whose entry_time is within CLUSTER_WINDOW of it."""
    t0 = trade["entry_time"]
    nearby = sum(
        1 for other in all_trades
        if abs(other["entry_time"] - t0) <= CLUSTER_WINDOW
    )
    return nearby >= CLUSTER_THRESHOLD


def filter_clean_trades(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split into (clean, dropped). Dropped = restart-cluster contamination."""
    clean: list[dict] = []
    dropped: list[dict] = []
    for t in trades:
        if is_restart_cluster(t, trades):
            dropped.append(t)
        else:
            clean.append(t)
    return clean, dropped


# ---------------------------------------------------------------------------
# Cache helpers + replay
# ---------------------------------------------------------------------------

def load_m15_cache(cache_name: str) -> pd.DataFrame | None:
    p = CACHE_ROOT / cache_name / "M15" / "data.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def resample_ohlc(df_m15: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df_m15.resample(rule).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()


def build_dfs_at(df_m15_full: pd.DataFrame, entry_ts: pd.Timestamp) -> dict[str, pd.DataFrame] | None:
    df_m15 = df_m15_full.loc[:entry_ts]
    if len(df_m15) >= 1 and df_m15.index[-1] >= entry_ts:
        df_m15 = df_m15.iloc[:-1]
    if len(df_m15) < 200:
        return None
    df_h1 = resample_ohlc(df_m15, "1h")
    df_h4 = resample_ohlc(df_m15, "4h")
    df_d1 = resample_ohlc(df_m15, "1D")
    df_w1 = resample_ohlc(df_m15, "1W-MON")
    if len(df_d1) < 10 or len(df_h4) < 10:
        return None
    return {
        "M15": df_m15.tail(500).copy(),
        "H1":  df_h1.tail(500).copy(),
        "H4":  df_h4.tail(500).copy(),
        "D1":  df_d1.tail(200).copy(),
        "W1":  df_w1.tail(60).copy(),
    }


class _StubMT5Data:
    def __init__(self, dfs: dict[str, pd.DataFrame]):
        self._dfs = dfs

    def collect_data(self, symbol: str) -> dict[str, pd.DataFrame]:
        return {k: v.copy() for k, v in self._dfs.items()}


def replay_one(trade: dict) -> dict:
    sym = trade["symbol"]
    cache_name = BROKER_TO_CACHE.get(sym)
    tv_sym = BROKER_TO_TV.get(sym)
    if cache_name is None or tv_sym is None:
        return {"trade": trade, "skipped": True, "reason": "no cache mapping"}
    df_m15_full = load_m15_cache(cache_name)
    if df_m15_full is None:
        return {"trade": trade, "skipped": True, "reason": "cache file missing"}

    entry_ts = pd.Timestamp(trade["entry_time"])
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.tz_localize("UTC")
    if entry_ts < df_m15_full.index[0] or entry_ts > df_m15_full.index[-1]:
        return {"trade": trade, "skipped": True, "reason": "entry outside cache range"}

    dfs = build_dfs_at(df_m15_full, entry_ts)
    if dfs is None:
        return {"trade": trade, "skipped": True, "reason": "insufficient bars at entry_ts"}

    try:
        cfg = get_bridge_config()
        pipe = ICTPipeline(config=cfg)
        pipe.client = None
        pipe._use_mt5 = True
        pipe._mt5_data = _StubMT5Data(dfs)
        decision_maker = ClaudeDecisionMaker()
    except Exception as e:
        return {"trade": trade, "skipped": True, "reason": f"setup crashed: {e}"}

    try:
        analysis = pipe.analyze_symbol(tv_sym)
    except Exception as e:
        return {"trade": trade, "skipped": True, "reason": f"analyze_symbol crashed: {type(e).__name__}: {e}"}
    if analysis.error:
        return {"trade": trade, "skipped": True, "reason": f"analysis error: {analysis.error}"}

    try:
        skip_reason = decision_maker._pre_gate(analysis)
    except Exception as e:
        return {"trade": trade, "skipped": True, "reason": f"_pre_gate crashed: {type(e).__name__}: {e}"}

    return {
        "trade": trade,
        "skipped": False,
        "blocked": skip_reason is not None,
        "reason": skip_reason,
        "new_score": getattr(analysis, "total_score", None),
        "new_grade": getattr(analysis, "grade", None),
        "new_direction": getattr(analysis, "direction", None),
        "new_pd_zone": getattr(analysis, "pd_zone", None),
    }


def gate_category(reason: str | None) -> str:
    if reason is None:
        return "PASS"
    r = reason.upper()
    if "ZONE VIOLATION" in r and "HTF" in r:
        return "HTF_ZONE"
    if "ZONE VIOLATION" in r:
        return "PD_ZONE"
    if "KILL ZONE" in r:
        return "KILL_ZONE"
    if "HTF DATA" in r:
        return "HTF_DATA"
    if "HTF ALIGNMENT" in r or "HTF BIAS" in r:
        return "HTF_ALIGN"
    if "DOL" in r:
        return "DOL_FILTER"
    if "DISPLACEMENT" in r:
        return "DISPLACEMENT"
    if "REASONING" in r or "POST-GATE" in r:
        return "REASONING"
    return "OTHER"


def _is_real_block(r: dict) -> bool:
    """Real-block: trade direction agreed with gate's bias direction.
    A SELL trade blocked because the gate analyzed BULLISH bias is NOT
    a false-block — the bridge wouldn't have BUYed there, the trader did
    SELL on a different signal."""
    if r.get("skipped") or not r.get("blocked"):
        return False
    td = str(r["trade"]["direction"]).upper()
    gd = (r.get("new_direction") or "").upper()
    return (td == "BUY" and gd == "BULLISH") or (td == "SELL" and gd == "BEARISH")


def main() -> int:
    print("=" * 78)
    print("Winner-blocking regression test (broker-truth)")
    print("=" * 78)
    print()

    # Pull broker history covering the full ICT_Bridge live period
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 27, tzinfo=timezone.utc)
    print(f"Pulling FTMO broker history {start.date()} -> {end.date()}...", flush=True)
    raw_trades = load_broker_trades(start, end)
    print(f"Closed ICT_Bridge positions on broker: {len(raw_trades)}")

    # Filter restart-cluster contamination (see filter_clean_trades comment)
    trades, dropped = filter_clean_trades(raw_trades)
    print(f"  Restart-cluster trades dropped: {len(dropped)} "
          f"(>= {CLUSTER_THRESHOLD} entries within {CLUSTER_WINDOW})")
    if dropped:
        first_dropped = min(d["entry_time"] for d in dropped)
        last_dropped = max(d["entry_time"] for d in dropped)
        dropped_pnl = sum(d["pnl_usd"] for d in dropped)
        print(f"     dropped span: {first_dropped} -> {last_dropped}  net=${dropped_pnl:+.2f}")
    print(f"  Clean trades for analysis:      {len(trades)}")

    winners = [t for t in trades if t["pnl_usd"] > 0]
    losers = [t for t in trades if t["pnl_usd"] <= 0]
    print(f"  Clean winners (broker P&L > 0): {len(winners)}  total=${sum(t['pnl_usd'] for t in winners):+.2f}")
    print(f"  Clean losers  (broker P&L <=0): {len(losers)}  total=${sum(t['pnl_usd'] for t in losers):+.2f}")
    print(f"  Clean net broker P&L:           ${sum(t['pnl_usd'] for t in trades):+.2f}")
    print()
    print("Replaying each clean trade against today's pre-gates...\n", flush=True)

    win_results: list[dict] = []
    los_results: list[dict] = []

    for label, bucket, results_list in (
        ("WINNERS", winners, win_results),
        ("LOSERS",  losers,  los_results),
    ):
        print(f"--- {label} ---", flush=True)
        for i, t in enumerate(bucket, 1):
            try:
                r = replay_one(t)
            except Exception as e:
                r = {"trade": t, "skipped": True, "reason": f"outer crash: {type(e).__name__}: {e}"}
            results_list.append(r)
            tag = "SKIP" if r.get("skipped") else ("BLOCK" if r.get("blocked") else "PASS")
            extra = ""
            if r.get("skipped"):
                extra = f"({r['reason']})"
            elif r.get("blocked"):
                short = r["reason"][:80].replace("\n", " ")
                cat = gate_category(r["reason"])
                gate_dir = r.get("new_direction") or "?"
                false_block = " <-- REAL FALSE-BLOCK" if _is_real_block(r) else ""
                extra = f"[{cat}] gate_bias={gate_dir} | {short}{false_block}"
            else:
                ns = r.get("new_score")
                ng = r.get("new_grade")
                extra = f"new_score={ns} new_grade={ng}"
            print(
                f"  [{i:>2}/{len(bucket)}] {t['entry_time'].strftime('%Y-%m-%dT%H:%M:%S')} "
                f"{t['symbol']:<12} {t['direction']:<5} pnl={t['pnl_usd']:>+9.2f} -> {tag} {extra}",
                flush=True,
            )

    # ---- Aggregate ----
    print()
    print("=" * 78)
    print("Aggregate")
    print("=" * 78)

    def _summarize(label: str, results: list[dict]) -> dict:
        total = len(results)
        skipped = sum(1 for r in results if r.get("skipped"))
        replayed = total - skipped
        blocked = sum(1 for r in results if not r.get("skipped") and r.get("blocked"))
        real_blocked = sum(1 for r in results if _is_real_block(r))
        passed = replayed - blocked
        cats: Counter[str] = Counter()
        real_cats: Counter[str] = Counter()
        for r in results:
            if not r.get("skipped") and r.get("blocked"):
                cats[gate_category(r.get("reason"))] += 1
                if _is_real_block(r):
                    real_cats[gate_category(r.get("reason"))] += 1
        # Dollar impact: sum P&L of trades blocked
        blocked_pnl = sum(
            r["trade"]["pnl_usd"] for r in results
            if not r.get("skipped") and r.get("blocked")
        )
        real_blocked_pnl = sum(
            r["trade"]["pnl_usd"] for r in results if _is_real_block(r)
        )
        print(f"  {label}: total={total}  skipped={skipped}  replayed={replayed}  pass={passed}  blocked={blocked}  real_blocked={real_blocked}")
        if replayed:
            print(f"     all_block_rate  = {100*blocked/replayed:.1f}%")
            print(f"     real_block_rate = {100*real_blocked/replayed:.1f}%  (target for winners: 0%)")
        if cats:
            print(f"     all gates fired:  {dict(cats)}")
        if real_cats:
            print(f"     real-block gates: {dict(real_cats)}")
        print(f"     blocked P&L:       ${blocked_pnl:+.2f}  (real-block subset: ${real_blocked_pnl:+.2f})")
        return {
            "total": total, "skipped": skipped, "replayed": replayed,
            "passed": passed, "blocked": blocked, "real_blocked": real_blocked,
            "blocked_pnl": blocked_pnl, "real_blocked_pnl": real_blocked_pnl,
            "cats": dict(cats), "real_cats": dict(real_cats),
        }

    w = _summarize("WINNERS", win_results)
    print()
    l = _summarize("LOSERS ", los_results)

    # Net dollar impact
    print()
    net = -w["real_blocked_pnl"] - l["real_blocked_pnl"]
    print(f"  Net dollar impact if today's gates had been live:")
    print(f"    Lost (winners blocked):      ${-w['real_blocked_pnl']:+.2f}")
    print(f"    Saved (losers blocked):      ${-l['real_blocked_pnl']:+.2f}")
    print(f"    Net effect on real money:    ${net:+.2f}")

    print()
    print("=" * 78)
    print("Verdict")
    print("=" * 78)
    if w["replayed"] == 0:
        print("  CANNOT VERDICT — zero winners replayable. Cache coverage too thin.")
        return 1
    if w["real_blocked"] == 0:
        print(f"  PASS — 0 of {w['replayed']} replayable winners would have been REAL-blocked.")
        if w["blocked"] > 0:
            print(f"         ({w['blocked']} bias-mismatch-only blocks: trade went one way, gate's bias the other.)")
        if l["replayed"] > 0:
            print(f"         For reference: losers had {l['real_blocked']}/{l['replayed']} = "
                  f"{100*l['real_blocked']/l['replayed']:.0f}% real-block rate.")
        return 0
    print(f"  FAIL — {w['real_blocked']} of {w['replayed']} replayable winners would have been")
    print( "         REAL-blocked by today's pre-gates. Investigate each:")
    for r in win_results:
        if _is_real_block(r):
            t = r["trade"]
            print(
                f"         - {t['entry_time'].strftime('%Y-%m-%dT%H:%M:%S')} {t['symbol']} {t['direction']} "
                f"pnl=+{t['pnl_usd']:.2f} bias={r.get('new_direction')} | "
                f"gate={gate_category(r['reason'])} | {r['reason'][:120]}"
            )
    if l["replayed"] > 0:
        print(f"\n  For reference: losers had {l['real_blocked']}/{l['replayed']} = "
              f"{100*l['real_blocked']/l['replayed']:.0f}% real-block rate.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
