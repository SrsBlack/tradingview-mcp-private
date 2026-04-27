"""
HTF rejection detector — cycle-log replay bench.

Question this answers: of the bridge-analysis cycles where the bridge
emitted a direction, would the new HTF rejection detector have FLIPPED
that direction — and would the flip have been correct?

The broker-history bench (bench_htf_rejection.py) can only see trades
the bridge ACTUALLY entered. The motivating bug — ETH 2026-04-27, where
the bridge stayed BULLISH through a clear bearish HTF rejection and
never entered — is invisible to broker history. The cycle-log replay
fixes that by parsing every emitted Grade line from logs/trading.log
and replaying the detector at that cycle's M15 timestamp.

Method:
1. Parse logs/trading.log for cycle markers like:
       [CYCLE N] Starting analysis @ HH:MM:SS UTC
   plus per-symbol Grade lines like:
       [SYM] Grade A (NN/100) <DIRECTION> | ...
2. Reconstruct each cycle's UTC timestamp by combining the most recent
   "Started : YYYY-MM-DD HH:MM UTC" session marker date with the cycle
   time.
3. For each (symbol, cycle_ts, direction) tuple where the symbol maps
   to our M15 cache, replay detect_htf_rejection() with cycle_ts as
   the cutoff.
4. Score:
     FLIP_AGREE      — detector fires opposite direction AND forward
                       price moves agree with the flip (correct flip).
     FLIP_DISAGREE   — detector fires opposite direction BUT forward
                       price moves AGAINST the flip (incorrect flip).
     CONFIRM         — detector fires same direction (no flip needed).
     QUIET           — detector did not fire.
5. Report per-symbol counts, plus a spotlight on ETH 2026-04-27 cycles
   covering the 11:15 UTC chart event.

Forward-price ground truth: 4h forward close vs cycle close.
  if forward_close < cycle_close - 0.5 * atr_m15 -> direction=BEARISH
  if forward_close > cycle_close + 0.5 * atr_m15 -> direction=BULLISH
  otherwise NEUTRAL (treated as ambiguous; not scored)

Usage:
    PYTHONUTF8=1 python scripts/bench_htf_rejection_cycles.py
"""

from __future__ import annotations

import re
import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
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

from analysis.fvg import detect_fvgs  # noqa: E402
from analysis.order_blocks import detect_order_blocks  # noqa: E402
from analysis.structure import detect_swings  # noqa: E402
from analysis.htf_rejection import (  # noqa: E402
    detect_htf_rejection,
    strongest_rejection_direction,
)
from core.types import Direction, FVGQuality  # noqa: E402

LOG_PATH = _BRIDGE_ROOT / "logs" / "trading.log"
CACHE_ROOT = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache")

# Map bridge-internal symbol (as it appears in [SYM] lines) -> cache dir name.
SYM_TO_CACHE = {
    "BITSTAMP:BTCUSD":  "BTCUSD",
    "COINBASE:ETHUSD":  "ETHUSD",
    "COINBASE:SOLUSD":  "SOLUSD",
    "OANDA:EURUSD":     "EURUSD",
    "OANDA:GBPUSD":     "GBPUSD",
    "OANDA:XAUUSD":     "XAUUSD",
    "CAPITALCOM:US500": "US500.cash",
}

# Detector params (Phase 1 defaults; sweep separately via bench_htf_rejection_sweep.py)
LOOKBACK_M15 = 12
DISPLACEMENT_MIN = 1.5
BODY_MIN_PCT = 0.55

FORWARD_BARS = 16  # 16 M15 bars = 4h forward
ATR_MULT = 0.5     # forward move threshold


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------


SESSION_RX = re.compile(
    r"^\s*Started\s*:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2})\s+UTC"
)
CYCLE_RX = re.compile(
    r"^\[CYCLE\s+\d+\]\s+Starting analysis @\s+(\d{2}):(\d{2}):(\d{2})\s+UTC"
)
GRADE_RX = re.compile(
    r"^\s*\[([A-Z0-9_:.\!]+)\]\s+Grade\s+([A-D])\s+\((\d+)/100\)\s+(BULLISH|BEARISH|NEUTRAL)"
)


def parse_log(path: Path) -> list[dict]:
    """Walk the log producing one dict per (symbol, cycle, direction).

    Each dict contains: ts (UTC), symbol, direction, grade, score.
    """
    if not path.exists():
        return []

    out: list[dict] = []
    session_date: datetime | None = None
    cycle_time: tuple[int, int, int] | None = None
    cycle_dt: datetime | None = None

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = SESSION_RX.match(line)
            if m:
                d_str = m.group(1)
                hh = int(m.group(2))
                mm = int(m.group(3))
                session_date = datetime(
                    *[int(x) for x in d_str.split("-")],
                    hh, mm, tzinfo=timezone.utc,
                )
                continue
            m = CYCLE_RX.match(line)
            if m:
                hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
                cycle_time = (hh, mm, ss)
                # Roll the date forward if cycle time wrapped past midnight.
                if session_date is None:
                    cycle_dt = None
                    continue
                base = session_date.replace(hour=hh, minute=mm, second=ss)
                if base < session_date:
                    base = base + timedelta(days=1)
                cycle_dt = base
                continue
            m = GRADE_RX.match(line)
            if m and cycle_dt is not None:
                sym = m.group(1)
                grade = m.group(2)
                score = int(m.group(3))
                d = m.group(4)
                if d == "BULLISH":
                    direction = Direction.BULLISH
                elif d == "BEARISH":
                    direction = Direction.BEARISH
                else:
                    direction = None
                if direction is None:
                    continue
                out.append({
                    "ts": cycle_dt,
                    "symbol": sym,
                    "direction": direction,
                    "grade": grade,
                    "score": score,
                })
    return out


# ---------------------------------------------------------------------------
# Cache helpers (mirrors bench_htf_rejection.py)
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


# ---------------------------------------------------------------------------
# Per-cycle replay
# ---------------------------------------------------------------------------


def forward_direction(
    df_m15_full: pd.DataFrame,
    cycle_ts: pd.Timestamp,
    forward_bars: int,
    atr_m15: float,
) -> Direction | None:
    """Return BULLISH/BEARISH/None based on forward 4h price move."""
    after = df_m15_full.loc[cycle_ts:]
    if len(after) < forward_bars + 1:
        return None
    cycle_close = float(after["close"].iloc[0])
    fwd_close = float(after["close"].iloc[forward_bars])
    delta = fwd_close - cycle_close
    threshold = ATR_MULT * atr_m15
    if delta > threshold:
        return Direction.BULLISH
    if delta < -threshold:
        return Direction.BEARISH
    return None


def replay_cycle(cycle: dict) -> dict:
    sym = cycle["symbol"]
    cache_name = SYM_TO_CACHE.get(sym)
    if cache_name is None:
        return {"cycle": cycle, "skipped": True, "reason": "no cache mapping"}
    df_m15_full = load_m15_cache(cache_name)
    if df_m15_full is None:
        return {"cycle": cycle, "skipped": True, "reason": "cache missing"}

    ts = pd.Timestamp(cycle["ts"])
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")

    # Snap to nearest M15 bar at-or-before cycle_ts
    df_at = df_m15_full.loc[:ts]
    if len(df_at) < 200:
        return {"cycle": cycle, "skipped": True, "reason": "insufficient bars"}

    df_h4 = resample_ohlc(df_at, "4h")
    df_d1 = resample_ohlc(df_at, "1D")
    if len(df_h4) < 10 or len(df_d1) < 10:
        return {"cycle": cycle, "skipped": True, "reason": "insufficient HTF bars"}

    atr_m15 = compute_atr(df_at.tail(200), period=14)
    if atr_m15 <= 0:
        return {"cycle": cycle, "skipped": True, "reason": "zero ATR"}

    try:
        h4_fvgs = detect_fvgs(df_h4.tail(200), max_age_bars=30, min_quality=FVGQuality.AGGRESSIVE)
        d1_fvgs = detect_fvgs(df_d1.tail(120), max_age_bars=30, min_quality=FVGQuality.AGGRESSIVE)
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
        return {"cycle": cycle, "skipped": True, "reason": f"detect: {e}"}

    rejections = (
        detect_htf_rejection(
            df_m15=df_at.tail(60), htf_fvgs=h4_fvgs, htf_obs=h4_obs,
            atr_m15=atr_m15, lookback_m15=LOOKBACK_M15,
            displacement_min=DISPLACEMENT_MIN, body_min_pct=BODY_MIN_PCT,
            htf_timeframe="H4",
        )
        + detect_htf_rejection(
            df_m15=df_at.tail(60), htf_fvgs=d1_fvgs, htf_obs=d1_obs,
            atr_m15=atr_m15, lookback_m15=LOOKBACK_M15,
            displacement_min=DISPLACEMENT_MIN, body_min_pct=BODY_MIN_PCT,
            htf_timeframe="D1",
        )
    )
    fired = strongest_rejection_direction(rejections)

    bridge_dir: Direction = cycle["direction"]
    fwd_dir = forward_direction(df_m15_full, ts, FORWARD_BARS, atr_m15)

    if fired is None:
        bucket = "QUIET"
    elif fired == bridge_dir:
        bucket = "CONFIRM"
    else:
        # detector flips the bridge's direction. is the flip correct?
        if fwd_dir is None:
            bucket = "FLIP_UNKNOWN"   # forward move ambiguous
        elif fwd_dir == fired:
            bucket = "FLIP_AGREE"
        else:
            bucket = "FLIP_DISAGREE"

    return {
        "cycle": cycle,
        "skipped": False,
        "bucket": bucket,
        "fired_dir": fired.value if fired else None,
        "bridge_dir": bridge_dir.value,
        "fwd_dir": fwd_dir.value if fwd_dir else None,
        "atr_m15": atr_m15,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 78)
    print("HTF rejection detector — cycle-log replay bench")
    print("=" * 78)
    print()

    print(f"Parsing log: {LOG_PATH}")
    cycles = parse_log(LOG_PATH)
    print(f"Parsed cycle x symbol records: {len(cycles)}")

    # Filter to cacheable symbols only
    cycles = [c for c in cycles if c["symbol"] in SYM_TO_CACHE]
    print(f"Cacheable (one of {len(SYM_TO_CACHE)} symbols): {len(cycles)}")

    if not cycles:
        print("No cycles found — log empty or format mismatch.")
        return 1

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for c in cycles:
        by_sym[c["symbol"]].append(c)

    print()
    print(f"Detector params: lookback={LOOKBACK_M15}, disp={DISPLACEMENT_MIN}, body={BODY_MIN_PCT}")
    print(f"Forward-price window: {FORWARD_BARS} M15 bars (4h), threshold={ATR_MULT}*ATR")
    print()

    results: list[dict] = []
    eth_27_results: list[dict] = []

    for sym in sorted(by_sym):
        sym_cycles = by_sym[sym]
        print(f"--- {sym} (n={len(sym_cycles)}) ---", flush=True)
        bucket_counts: Counter[str] = Counter()
        skipped_reasons: Counter[str] = Counter()
        for c in sym_cycles:
            try:
                r = replay_cycle(c)
            except Exception as e:
                r = {"cycle": c, "skipped": True, "reason": f"crash: {type(e).__name__}: {e}"}
            results.append(r)
            if r.get("skipped"):
                skipped_reasons[r["reason"][:40]] += 1
            else:
                bucket_counts[r["bucket"]] += 1
            # ETH 2026-04-27 spotlight collection
            if (
                sym == "COINBASE:ETHUSD"
                and c["ts"].date() == datetime(2026, 4, 27).date()
            ):
                eth_27_results.append(r)
        print(f"   buckets: {dict(bucket_counts)}")
        if skipped_reasons:
            print(f"   skipped: {dict(skipped_reasons)}")

    # Aggregate
    print()
    print("=" * 78)
    print("Aggregate")
    print("=" * 78)
    total = len(results)
    skipped = sum(1 for r in results if r.get("skipped"))
    replayed = total - skipped
    overall: Counter[str] = Counter()
    for r in results:
        if r.get("skipped"):
            continue
        overall[r["bucket"]] += 1
    print(f"Total: {total}  skipped={skipped}  replayed={replayed}")
    for b in ("FLIP_AGREE", "FLIP_DISAGREE", "FLIP_UNKNOWN", "CONFIRM", "QUIET"):
        n = overall.get(b, 0)
        pct = (n / replayed * 100) if replayed else 0.0
        print(f"  {b:<13} {n:>4}/{replayed} ({pct:5.1f}%)")

    flips = overall.get("FLIP_AGREE", 0) + overall.get("FLIP_DISAGREE", 0)
    if flips:
        accuracy = overall.get("FLIP_AGREE", 0) / flips * 100
        print(f"\n  Flip accuracy (FLIP_AGREE / (FLIP_AGREE+FLIP_DISAGREE)): {accuracy:.1f}%")
    else:
        print("\n  No flips fired — detector confirmed bridge direction or stayed quiet.")

    # ETH 2026-04-27 spotlight
    print()
    print("=" * 78)
    print("Spotlight: COINBASE:ETHUSD cycles on 2026-04-27")
    print("=" * 78)
    if not eth_27_results:
        print("  No ETH cycles on 2026-04-27 in log (bridge may have been offline,")
        print("  or cycle was not yet logged when bench was run).")
    else:
        for r in eth_27_results:
            c = r["cycle"]
            ts = c["ts"].strftime("%H:%M:%S")
            if r.get("skipped"):
                print(f"  {ts} bridge={c['direction'].value:<8} grade={c['grade']} -> SKIP ({r['reason']})")
            else:
                print(
                    f"  {ts} bridge={r['bridge_dir']:<8} grade={c['grade']} "
                    f"-> {r['bucket']:<14} fired={r['fired_dir']} fwd={r['fwd_dir']}"
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
