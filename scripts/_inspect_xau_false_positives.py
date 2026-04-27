"""Why does the detector fire BULLISH on XAU during bridge-BEARISH cycles?

Pulls the 21 FLIP_DISAGREE XAU cases from the cycle bench and traces:
  - Which timeframe and zone fired
  - The M15 bar sequence (tag bar -> displacement bar)
  - Whether the displacement bar's body + close was a "real" rejection
    or a routine pullback that incidentally satisfied the predicate
"""
from __future__ import annotations
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(1, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from analysis.fvg import detect_fvgs
from analysis.order_blocks import detect_order_blocks
from analysis.structure import detect_swings
from analysis.htf_rejection import detect_htf_rejection
from core.types import Direction, FVGQuality

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "trading.log"
CACHE = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache/XAUUSD/M15/data.parquet")

SESSION_RX = re.compile(r"^\s*Started\s*:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2})\s+UTC")
CYCLE_RX = re.compile(r"^\[CYCLE\s+\d+\]\s+Starting analysis @\s+(\d{2}):(\d{2}):(\d{2})\s+UTC")
GRADE_RX = re.compile(r"^\s*\[OANDA:XAUUSD\]\s+Grade\s+([A-D])\s+\((\d+)/100\)\s+(BULLISH|BEARISH|NEUTRAL)")


def parse_xau_cycles() -> list[dict]:
    out: list[dict] = []
    session_date: datetime | None = None
    cycle_dt: datetime | None = None
    with LOG_PATH.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = SESSION_RX.match(line)
            if m:
                d = m.group(1).split("-")
                session_date = datetime(int(d[0]), int(d[1]), int(d[2]),
                                        int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
                continue
            m = CYCLE_RX.match(line)
            if m and session_date is not None:
                hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
                base = session_date.replace(hour=hh, minute=mm, second=ss)
                if base < session_date:
                    base += timedelta(days=1)
                cycle_dt = base
                continue
            m = GRADE_RX.match(line)
            if m and cycle_dt is not None:
                d = m.group(3)
                if d == "NEUTRAL":
                    continue
                out.append({
                    "ts": cycle_dt,
                    "grade": m.group(1),
                    "score": int(m.group(2)),
                    "direction": Direction.BULLISH if d == "BULLISH" else Direction.BEARISH,
                })
    return out


def main() -> int:
    df_full = pd.read_parquet(CACHE)
    if df_full.index.tz is None:
        df_full.index = df_full.index.tz_localize("UTC")

    cycles = parse_xau_cycles()
    print(f"XAU cycles in log: {len(cycles)}")

    disagree_cases = []

    for c in cycles:
        ts = pd.Timestamp(c["ts"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        df_at = df_full.loc[:ts]
        if len(df_at) < 200:
            continue
        df_h4 = df_at.resample("4h").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
        df_d1 = df_at.resample("1D").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()

        highs = df_at["high"].astype(float)
        lows = df_at["low"].astype(float)
        closes = df_at["close"].astype(float)
        tr = pd.concat([highs - lows, (highs - closes.shift(1)).abs(), (lows - closes.shift(1)).abs()], axis=1).max(axis=1)
        atr = float(tr.iloc[-14:].mean())
        if atr <= 0:
            continue

        h4_fvgs = detect_fvgs(df_h4.tail(200), max_age_bars=120, min_quality=FVGQuality.AGGRESSIVE)
        d1_fvgs = detect_fvgs(df_d1.tail(120), max_age_bars=60, min_quality=FVGQuality.AGGRESSIVE)
        h4_swings = detect_swings(df_h4.tail(200), lookback=3)
        d1_swings = detect_swings(df_d1.tail(120), lookback=2)
        h4_obs = detect_order_blocks(df_h4.tail(200), fvgs=h4_fvgs, swings=h4_swings, lookback=120, require_fvg=False, require_bos=False)
        d1_obs = detect_order_blocks(df_d1.tail(120), fvgs=d1_fvgs, swings=d1_swings, lookback=60, require_fvg=False, require_bos=False)

        rej = (
            detect_htf_rejection(df_m15=df_at.tail(60), htf_fvgs=h4_fvgs, htf_obs=h4_obs, atr_m15=atr,
                                 lookback_m15=12, displacement_min=1.5, body_min_pct=0.55, htf_timeframe="H4")
            + detect_htf_rejection(df_m15=df_at.tail(60), htf_fvgs=d1_fvgs, htf_obs=d1_obs, atr_m15=atr,
                                   lookback_m15=12, displacement_min=1.5, body_min_pct=0.55, htf_timeframe="D1")
        )
        if not rej:
            continue
        # Forward 4h
        after = df_full.loc[ts:]
        if len(after) < 17:
            continue
        cycle_close = float(after["close"].iloc[0])
        fwd_close = float(after["close"].iloc[16])
        fwd_dir = Direction.BULLISH if fwd_close - cycle_close > 0.5 * atr else \
                  Direction.BEARISH if cycle_close - fwd_close > 0.5 * atr else None
        # Strongest direction
        from analysis.htf_rejection import strongest_rejection_direction
        fired = strongest_rejection_direction(rej)
        if fired is None or fired == c["direction"]:
            continue
        if fwd_dir is None or fwd_dir == fired:
            continue  # only collect FLIP_DISAGREE
        disagree_cases.append({
            "ts": ts, "bridge_dir": c["direction"], "fired": fired, "fwd_dir": fwd_dir,
            "rejections": rej, "atr": atr, "df": df_at.tail(20).copy(),
            "cycle_close": cycle_close, "fwd_close": fwd_close,
        })

    print(f"FLIP_DISAGREE cases: {len(disagree_cases)}")
    print()
    for i, case in enumerate(disagree_cases[:8], 1):
        print(f"=== Case {i}/{len(disagree_cases)}: {case['ts']} ===")
        print(f"  bridge={case['bridge_dir'].value}  detector_fired={case['fired'].value}  fwd4h={case['fwd_dir'].value}")
        print(f"  cycle_close={case['cycle_close']:.2f}  fwd_close_4h={case['fwd_close']:.2f}  ATR={case['atr']:.2f}")
        # Show the strongest rejection
        from analysis.htf_rejection import strongest_rejection_direction
        rels = [r for r in case["rejections"] if r.direction == case["fired"]]
        rels.sort(key=lambda r: r.rejection_displacement, reverse=True)
        for r in rels[:2]:
            print(f"  rej: {r.htf_timeframe} {r.htf_zone_type} zone={r.htf_zone_low:.2f}-{r.htf_zone_high:.2f}  disp={r.rejection_displacement:.2f}  bar_idx={r.rejection_bar_idx}")
        # Show last 12 M15 bars
        last12 = case["df"].tail(12)
        for ts2, row in last12.iterrows():
            body = abs(row["close"] - row["open"])
            rng = row["high"] - row["low"]
            body_pct = body / rng * 100 if rng > 0 else 0
            bull = row["close"] > row["open"]
            print(f"    {ts2.strftime('%H:%M')} O={row['open']:7.2f} H={row['high']:7.2f} L={row['low']:7.2f} C={row['close']:7.2f}  "
                  f"rng={rng:5.2f} body%={body_pct:4.0f} bull={bull}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
