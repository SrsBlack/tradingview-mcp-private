"""Replay detector at the 04:03 UTC cycle — the first cycle the bridge
emitted Grade C BULLISH while the H4 was rolling over."""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(1, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from analysis.fvg import detect_fvgs
from analysis.order_blocks import detect_order_blocks
from analysis.structure import detect_swings
from analysis.htf_rejection import detect_htf_rejection, strongest_rejection_direction
from core.types import Direction, FVGQuality

CACHE = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache/ETHUSD/M15/data.parquet")
df_full = pd.read_parquet(CACHE)
if df_full.index.tz is None:
    df_full.index = df_full.index.tz_localize("UTC")

# Test at multiple cycle timestamps
TEST_TIMES = [
    "2026-04-27 03:48",
    "2026-04-27 04:03",
    "2026-04-27 04:56",
    "2026-04-27 05:42",
    "2026-04-27 06:43",
    "2026-04-27 07:29",
    "2026-04-27 08:15",  # post-displacement
    "2026-04-27 09:00",  # latest
]

for t in TEST_TIMES:
    ts = pd.Timestamp(t, tz="UTC")
    df_at = df_full.loc[:ts]
    if len(df_at) < 200:
        print(f"{t}: insufficient")
        continue

    df_h4 = df_at.resample("4h").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
    df_d1 = df_at.resample("1D").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()

    highs = df_at["high"].astype(float)
    lows = df_at["low"].astype(float)
    closes = df_at["close"].astype(float)
    tr = pd.concat([highs - lows, (highs - closes.shift(1)).abs(), (lows - closes.shift(1)).abs()], axis=1).max(axis=1)
    atr = float(tr.iloc[-14:].mean())

    h4_fvgs = detect_fvgs(df_h4.tail(200), max_age_bars=120, min_quality=FVGQuality.AGGRESSIVE)
    d1_fvgs = detect_fvgs(df_d1.tail(120), max_age_bars=60, min_quality=FVGQuality.AGGRESSIVE)
    h4_swings = detect_swings(df_h4.tail(200), lookback=3)
    h4_obs = detect_order_blocks(df_h4.tail(200), fvgs=h4_fvgs, swings=h4_swings, lookback=120, require_fvg=False, require_bos=False)
    d1_obs = detect_order_blocks(df_d1.tail(120), fvgs=d1_fvgs, swings=detect_swings(df_d1.tail(120), lookback=2), lookback=60, require_fvg=False, require_bos=False)

    rej_h4 = detect_htf_rejection(df_m15=df_at.tail(60), htf_fvgs=h4_fvgs, htf_obs=h4_obs, atr_m15=atr,
                                   lookback_m15=12, displacement_min=1.5, body_min_pct=0.55, htf_timeframe="H4")
    rej_d1 = detect_htf_rejection(df_m15=df_at.tail(60), htf_fvgs=d1_fvgs, htf_obs=d1_obs, atr_m15=atr,
                                   lookback_m15=12, displacement_min=1.5, body_min_pct=0.55, htf_timeframe="D1")
    fired = strongest_rejection_direction(rej_h4 + rej_d1)
    last_close = float(df_at["close"].iloc[-1])
    last_high_12 = float(df_at["high"].iloc[-12:].max())
    last_low_12 = float(df_at["low"].iloc[-12:].min())
    bear_obs = [o for o in h4_obs if o.direction == Direction.BEARISH and o.bottom > last_close]
    print(f"{t}: close={last_close:.2f}  M15 last12 H={last_high_12:.2f} L={last_low_12:.2f}  ATR={atr:.2f}  "
          f"H4_bear_OBs_above={len(bear_obs)}  fired={fired}  rej_count={len(rej_h4)+len(rej_d1)}")
    if bear_obs:
        for ob in bear_obs[-3:]:
            print(f"     OB above: {ob.timestamp} top={ob.top:.2f} bot={ob.bottom:.2f}")
