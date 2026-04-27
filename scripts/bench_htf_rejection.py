"""
HTF FVG/OB rejection detector — historical-catch bench.

Question this answers: at the moment of each historical broker trade
entry, was an HTF rejection visible on M15 / H4 / D1 zones such that
the new detector would have emitted a signal in the trade's direction?

Method (mirrors bench_winners_not_blocked.py):
1. Pull every closed ICT_Bridge position from FTMO broker history.
2. Filter restart-cluster contamination (same threshold as winners
   bench: 4+ entries within 30 minutes).
3. For each clean trade:
   - Slice cached M15 to bars STRICTLY BEFORE entry_time.
   - Compute H4/D1 FVGs and OBs from existing detectors.
   - Compute ATR(14) on M15.
   - Run detect_htf_rejection() against the H4 zones, then the D1 zones.
   - Bucket by:
       Caught:  rejection in trade direction within `LOOKBACK_BARS` bars
                before entry.
       Late:    rejection in trade direction *after* entry — would have
                been a re-entry filter rather than a proactive signal.
                (Bench fairness: we only consider bars <= entry_time, so
                this bucket is empty by construction. Reserved for the
                Phase-2 bench that replays full pipeline.)
       Wrong:   detector fired but in the OPPOSITE direction (would
                actively block the trade if Phase-2 wires bias override).
       Missed:  no rejection detected.
4. Report per-bucket counts and broker-truth P&L.

Caveats:
- Cache covers 7 symbols. Other-symbol trades are skipped.
- "Caught" means the detector fires; it does NOT prove the trade
  would have been Claude-approved post-Phase-2.
- This is a one-shot historical replay; results frozen alongside the
  detector ship per the evidence-first-gates discipline (codified in
  feedback_evidence_first_gates.md).

Usage:
    PYTHONUTF8=1 python scripts/bench_htf_rejection.py
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

import MetaTrader5 as mt5  # noqa: E402

from analysis.fvg import detect_fvgs, FVGZone  # noqa: E402
from analysis.order_blocks import detect_order_blocks, OrderBlock  # noqa: E402
from analysis.structure import detect_swings  # noqa: E402
from analysis.htf_rejection import (  # noqa: E402
    detect_htf_rejection,
    HTFRejection,
    strongest_rejection_direction,
)
from core.types import Direction, FVGQuality  # noqa: E402

MT5_LOGIN = 1513140458
MT5_PASSWORD = "L!$q1k@4Z"
MT5_SERVER = "FTMO-Demo"
MT5_PATH = "C:/Program Files/METATRADER5.1/terminal64.exe"

CACHE_ROOT = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache")

BROKER_TO_CACHE = {
    "BTCUSD":     "BTCUSD",
    "ETHUSD":     "ETHUSD",
    "SOLUSD":     "SOLUSD",
    "EURUSD":     "EURUSD",
    "GBPUSD":     "GBPUSD",
    "XAUUSD":     "XAUUSD",
    "US500.cash": "US500.cash",
}

# How recent must the rejection be to count as "caught"?
# 6 M15 bars = 90 minutes. Mirrors the plan's ETH 2026-04-27 cycle window.
LOOKBACK_BARS = 6

# Bench parameters — match the detector defaults in the plan
DETECTOR_LOOKBACK = 12
DISPLACEMENT_MIN = 1.5
BODY_MIN_PCT = 0.55


# ---------------------------------------------------------------------------
# Broker history (copied from bench_winners_not_blocked.py to keep parity)
# ---------------------------------------------------------------------------


def load_broker_trades(start: datetime, end: datetime) -> list[dict]:
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
            continue
        od = opens[0]
        if "ICT_Bridge" not in (od.comment or ""):
            continue
        net_profit = sum(d.profit for d in ds)
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


CLUSTER_WINDOW = pd.Timedelta(minutes=30)
CLUSTER_THRESHOLD = 4


def is_restart_cluster(trade: dict, all_trades: list[dict]) -> bool:
    t0 = trade["entry_time"]
    nearby = sum(
        1 for other in all_trades
        if abs(other["entry_time"] - t0) <= CLUSTER_WINDOW
    )
    return nearby >= CLUSTER_THRESHOLD


def filter_clean_trades(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    clean: list[dict] = []
    dropped: list[dict] = []
    for t in trades:
        if is_restart_cluster(t, trades):
            dropped.append(t)
        else:
            clean.append(t)
    return clean, dropped


# ---------------------------------------------------------------------------
# Cache + per-trade replay
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


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.0
    highs = df["high"].astype(float)
    lows = df["low"].astype(float)
    closes = df["close"].astype(float)
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return float(tr.iloc[-period:].mean())


def replay_one(trade: dict) -> dict:
    """Run the HTF rejection detector against M15 data sliced to entry_time.

    Returns dict with caught/wrong/missed bucket and which timeframe fired.
    """
    sym = trade["symbol"]
    cache_name = BROKER_TO_CACHE.get(sym)
    if cache_name is None:
        return {"trade": trade, "skipped": True, "reason": "no cache mapping"}

    df_m15_full = load_m15_cache(cache_name)
    if df_m15_full is None:
        return {"trade": trade, "skipped": True, "reason": "cache missing"}

    entry_ts = pd.Timestamp(trade["entry_time"])
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.tz_localize("UTC")
    if entry_ts < df_m15_full.index[0] or entry_ts > df_m15_full.index[-1]:
        return {"trade": trade, "skipped": True, "reason": "entry outside cache range"}

    # Slice strictly before entry
    df_m15 = df_m15_full.loc[:entry_ts]
    if len(df_m15) >= 1 and df_m15.index[-1] >= entry_ts:
        df_m15 = df_m15.iloc[:-1]
    if len(df_m15) < 200:
        return {"trade": trade, "skipped": True, "reason": "insufficient bars"}

    df_h4 = resample_ohlc(df_m15, "4h")
    df_d1 = resample_ohlc(df_m15, "1D")
    if len(df_h4) < 10 or len(df_d1) < 10:
        return {"trade": trade, "skipped": True, "reason": "insufficient HTF bars"}

    # ATR on M15
    atr_m15 = compute_atr(df_m15.tail(200), period=14)
    if atr_m15 <= 0:
        return {"trade": trade, "skipped": True, "reason": "zero ATR"}

    # FVGs and OBs from H4 and D1
    try:
        h4_fvgs = detect_fvgs(df_h4.tail(200), max_age_bars=30, min_quality=FVGQuality.AGGRESSIVE)
        d1_fvgs = detect_fvgs(df_d1.tail(120), max_age_bars=30, min_quality=FVGQuality.AGGRESSIVE)
    except Exception as e:
        return {"trade": trade, "skipped": True, "reason": f"fvg: {e}"}

    try:
        h4_swings = detect_swings(df_h4.tail(200), lookback=3)
        d1_swings = detect_swings(df_d1.tail(120), lookback=2)
        h4_obs = detect_order_blocks(
            df_h4.tail(200), fvgs=h4_fvgs, swings=h4_swings, lookback=20,
            require_fvg=False, require_bos=False,
        )
        d1_obs = detect_order_blocks(
            df_d1.tail(120), fvgs=d1_fvgs, swings=d1_swings, lookback=20,
            require_fvg=False, require_bos=False,
        )
    except Exception as e:
        return {"trade": trade, "skipped": True, "reason": f"ob: {e}"}

    # The plan calls for tag-and-reject visible in the LAST LOOKBACK_BARS
    # M15 bars before entry. We use a fresh detector window of LOOKBACK_BARS
    # to require recent freshness.
    rejections_h4 = detect_htf_rejection(
        df_m15=df_m15.tail(60),
        htf_fvgs=h4_fvgs, htf_obs=h4_obs,
        atr_m15=atr_m15,
        lookback_m15=LOOKBACK_BARS,
        displacement_min=DISPLACEMENT_MIN,
        body_min_pct=BODY_MIN_PCT,
        htf_timeframe="H4",
    )
    rejections_d1 = detect_htf_rejection(
        df_m15=df_m15.tail(60),
        htf_fvgs=d1_fvgs, htf_obs=d1_obs,
        atr_m15=atr_m15,
        lookback_m15=LOOKBACK_BARS,
        displacement_min=DISPLACEMENT_MIN,
        body_min_pct=BODY_MIN_PCT,
        htf_timeframe="D1",
    )

    all_rej = rejections_h4 + rejections_d1
    fired_dir = strongest_rejection_direction(all_rej)
    trade_dir = Direction.BULLISH if trade["direction"] == "BUY" else Direction.BEARISH

    if fired_dir is None:
        bucket = "MISSED"
    elif fired_dir == trade_dir:
        bucket = "CAUGHT"
    else:
        bucket = "WRONG"

    tf = "-"
    if fired_dir is not None:
        # Pick which timeframe contributed the strongest rejection
        relevant = [r for r in all_rej if r.direction == fired_dir]
        if relevant:
            best = max(relevant, key=lambda r: r.rejection_displacement)
            tf = best.htf_timeframe

    return {
        "trade": trade,
        "skipped": False,
        "bucket": bucket,
        "fired_dir": fired_dir.value if fired_dir else None,
        "n_rejections": len(all_rej),
        "tf": tf,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 78)
    print("HTF rejection detector — historical-catch bench (broker-truth)")
    print("=" * 78)
    print()

    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 4, 28, tzinfo=timezone.utc)
    print(f"Pulling FTMO broker history {start.date()} -> {end.date()}...", flush=True)
    raw_trades = load_broker_trades(start, end)
    print(f"Closed ICT_Bridge positions on broker: {len(raw_trades)}")

    trades, dropped = filter_clean_trades(raw_trades)
    print(f"  Restart-cluster trades dropped: {len(dropped)}")
    print(f"  Clean trades for analysis:      {len(trades)}")
    print()

    longs = [t for t in trades if t["direction"] == "BUY"]
    shorts = [t for t in trades if t["direction"] == "SELL"]
    print(f"  Longs:  {len(longs)}  total=${sum(t['pnl_usd'] for t in longs):+.2f}")
    print(f"  Shorts: {len(shorts)}  total=${sum(t['pnl_usd'] for t in shorts):+.2f}")
    print()
    print(f"Detector params: lookback_m15={LOOKBACK_BARS}, displacement_min={DISPLACEMENT_MIN}, "
          f"body_min_pct={BODY_MIN_PCT}")
    print()

    eth_2026_04_27_seen = False

    long_results: list[dict] = []
    short_results: list[dict] = []

    for label, bucket, results_list in (
        ("LONGS",  longs,  long_results),
        ("SHORTS", shorts, short_results),
    ):
        print(f"--- {label} ---", flush=True)
        for i, t in enumerate(bucket, 1):
            try:
                r = replay_one(t)
            except Exception as e:
                r = {"trade": t, "skipped": True, "reason": f"crash: {type(e).__name__}: {e}"}
            results_list.append(r)
            if r.get("skipped"):
                tag = "SKIP"
                extra = f"({r['reason']})"
            else:
                tag = r["bucket"]
                extra = f"fired={r['fired_dir']} tf={r['tf']} n={r['n_rejections']}"
            ent = t["entry_time"].strftime("%Y-%m-%dT%H:%M:%S")
            line = (
                f"  [{i:>2}/{len(bucket)}] {ent} "
                f"{t['symbol']:<12} {t['direction']:<5} "
                f"pnl={t['pnl_usd']:>+9.2f} -> {tag} {extra}"
            )
            print(line, flush=True)
            # Spotlight ETH 2026-04-27 short — the motivating case
            if (
                t["symbol"] == "ETHUSD"
                and t["direction"] == "SELL"
                and t["entry_time"].date() == datetime(2026, 4, 27).date()
            ):
                eth_2026_04_27_seen = True

    # ---- Aggregate ----
    print()
    print("=" * 78)
    print("Aggregate")
    print("=" * 78)

    def _summarize(label: str, results: list[dict]) -> dict:
        total = len(results)
        skipped = sum(1 for r in results if r.get("skipped"))
        replayed = total - skipped
        buckets: Counter[str] = Counter()
        bucket_pnl: dict[str, float] = defaultdict(float)
        for r in results:
            if r.get("skipped"):
                continue
            buckets[r["bucket"]] += 1
            bucket_pnl[r["bucket"]] += r["trade"]["pnl_usd"]

        print(f"\n{label}: total={total} skipped={skipped} replayed={replayed}")
        for b in ("CAUGHT", "WRONG", "MISSED"):
            n = buckets.get(b, 0)
            pct = (n / replayed * 100) if replayed else 0.0
            pnl = bucket_pnl.get(b, 0.0)
            print(f"  {b:<8} {n:>3}/{replayed} ({pct:5.1f}%)  pnl=${pnl:+.2f}")
        return {
            "total": total, "skipped": skipped, "replayed": replayed,
            "buckets": dict(buckets), "bucket_pnl": dict(bucket_pnl),
        }

    long_summary = _summarize("LONGS",  long_results)
    short_summary = _summarize("SHORTS", short_results)

    # Headline numbers used for the Phase 1 ship gate
    print()
    print("=" * 78)
    print("Phase 1 gate metrics")
    print("=" * 78)
    for label, summary in (("LONGS", long_summary), ("SHORTS", short_summary)):
        replayed = summary["replayed"]
        caught = summary["buckets"].get("CAUGHT", 0)
        wrong = summary["buckets"].get("WRONG", 0)
        missed = summary["buckets"].get("MISSED", 0)
        catch_pct = (caught / replayed * 100) if replayed else 0.0
        wrong_pct = (wrong / replayed * 100) if replayed else 0.0
        print(
            f"  {label:<7} catch_rate={catch_pct:5.1f}%  "
            f"wrong_rate={wrong_pct:5.1f}%  "
            f"caught={caught}  wrong={wrong}  missed={missed}  N={replayed}"
        )

    print()
    if eth_2026_04_27_seen:
        print("  ETH 2026-04-27 SHORT: present in dataset (see SHORTS bucket above for fire result).")
    else:
        print("  ETH 2026-04-27 SHORT: NOT in broker history "
              "(may have been considered but not entered, or filtered by restart cluster).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
