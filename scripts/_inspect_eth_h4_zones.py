"""Diagnostic: what H4 zones does the detector see for ETH right now?

Replays the same setup the cycles bench uses (cache slice up to last
M15 bar) and dumps:
  - All H4 FVGs detected with quality, age, top/bottom, status
  - All H4 OBs detected with direction, top/bottom
  - The 4h-by-4h candle sequence around 2026-04-22 to 2026-04-25 so we
    can manually verify whether a 3-candle bearish FVG at 2398-2414
    actually exists in the data

This lets us answer: did detect_fvgs miss it, or was it never a
strict-FVG to begin with (and is therefore an OB / breaker / structure
high — needing different detection)?
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)

_BRIDGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BRIDGE))
sys.path.insert(1, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from analysis.fvg import detect_fvgs  # noqa: E402
from analysis.order_blocks import detect_order_blocks  # noqa: E402
from analysis.structure import detect_swings  # noqa: E402
from core.types import FVGQuality  # noqa: E402

CACHE = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache/ETHUSD/M15/data.parquet")

df_m15 = pd.read_parquet(CACHE)
if df_m15.index.tz is None:
    df_m15.index = df_m15.index.tz_localize("UTC")
else:
    df_m15.index = df_m15.index.tz_convert("UTC")

print(f"M15 cache range: {df_m15.index[0]} -> {df_m15.index[-1]}  ({len(df_m15)} bars)")
print()

# Resample to H4
df_h4 = df_m15.resample("4h").agg({
    "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
}).dropna()
print(f"H4 bars: {len(df_h4)}  range: {df_h4.index[0]} -> {df_h4.index[-1]}")
print()

# Show H4 candles around 2026-04-22 to 2026-04-25 (the swing high zone the user identified)
print("H4 candles 2026-04-21 to 2026-04-26:")
window = df_h4.loc["2026-04-21":"2026-04-27"]
for ts, row in window.iterrows():
    print(f"  {ts}  O={row['open']:8.2f}  H={row['high']:8.2f}  L={row['low']:8.2f}  C={row['close']:8.2f}")
print()

# Detect FVGs across the full H4 series
fvgs = detect_fvgs(df_h4.tail(200), max_age_bars=200, min_quality=FVGQuality.VERY_AGGRESSIVE)
print(f"H4 FVGs detected (max_age=200, min_quality=VERY_AGGRESSIVE): {len(fvgs)}")
bear = [f for f in fvgs if f.direction.value == "bearish"]
print(f"  bearish: {len(bear)}")
for f in bear[-15:]:
    print(f"    {f.timestamp}  top={f.top:.2f}  bot={f.bottom:.2f}  quality={f.quality.value}  status={f.status.value}  age={f.age_bars}")
print()

# Detect OBs
swings = detect_swings(df_h4.tail(200), lookback=3)
obs = detect_order_blocks(
    df_h4.tail(200), fvgs=fvgs, swings=swings, lookback=120,
    require_fvg=False, require_bos=False,
)
bear_obs = [o for o in obs if o.direction.value == "bearish"]
print(f"H4 Order Blocks (bearish): {len(bear_obs)}")
for o in bear_obs[-15:]:
    print(f"    {o.timestamp}  top={o.top:.2f}  bot={o.bottom:.2f}  status={o.status.value}  validity={o.validity_score}")
print()

# Spotlight: any zone overlapping 2398-2414?
print("Zones overlapping the 2398-2414 band (the user's identified zone):")
target_lo, target_hi = 2398.0, 2414.0
hits = 0
for f in bear:
    if f.bottom <= target_hi and f.top >= target_lo:
        print(f"  FVG  {f.timestamp}  top={f.top:.2f} bot={f.bottom:.2f} quality={f.quality.value} status={f.status.value}")
        hits += 1
for o in bear_obs:
    if o.bottom <= target_hi and o.top >= target_lo:
        print(f"  OB   {o.timestamp}  top={o.top:.2f} bot={o.bottom:.2f} status={o.status.value}")
        hits += 1
if hits == 0:
    print("  (none — neither FVG nor OB detector emitted a zone in this band)")
