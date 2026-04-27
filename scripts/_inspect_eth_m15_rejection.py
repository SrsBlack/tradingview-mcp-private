"""Why didn't detect_htf_rejection fire on ETH around 04:00-08:00 UTC?"""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(1, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from analysis.htf_rejection import detect_htf_rejection, _find_rejection_bar_bearish  # noqa
from analysis.fvg import FVGZone
from core.types import Direction, FVGQuality
from datetime import datetime, timezone

CACHE = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache/ETHUSD/M15/data.parquet")
df = pd.read_parquet(CACHE)
if df.index.tz is None:
    df.index = df.index.tz_localize("UTC")

# Slice last 30 M15 bars
window = df.tail(40).copy()
print(f"M15 window: {window.index[0]} -> {window.index[-1]}")
print()
print("Last 20 M15 bars:")
for ts, row in window.tail(20).iterrows():
    body = abs(row["close"] - row["open"])
    rng = row["high"] - row["low"]
    body_pct = body / rng * 100 if rng > 0 else 0
    print(f"  {ts}  O={row['open']:7.2f} H={row['high']:7.2f} L={row['low']:7.2f} C={row['close']:7.2f}  range={rng:5.2f} body={body:5.2f} body_pct={body_pct:5.1f}%")
print()

# ATR(14)
highs = window["high"].astype(float)
lows = window["low"].astype(float)
closes = window["close"].astype(float)
tr = pd.concat([highs - lows, (highs - closes.shift(1)).abs(), (lows - closes.shift(1)).abs()], axis=1).max(axis=1)
atr = float(tr.iloc[-14:].mean())
print(f"M15 ATR(14): {atr:.2f}")
print()

# The H4 bearish OB the cycles bench would feed in (the 2386.73-2403.55 OB)
# Let's check rejection against this zone
zones = [
    ("OB 22-04 12:00", 2403.55, 2386.73),
    ("OB 17-04 16:00 (wide)", 2449.70, 2370.61),
    ("FVG 18-04 00:00", 2413.83, 2408.96),
    ("OB 18-04 04:00", 2405.98, 2403.97),
]

# Check the actual detector
from analysis.htf_rejection import detect_htf_rejection
fvgs = [
    FVGZone(direction=Direction.BEARISH, top=2413.83, bottom=2408.96, ce=2411.4, bar_index=0,
            timestamp=datetime(2026,4,18,0,tzinfo=timezone.utc), quality=FVGQuality.VERY_AGGRESSIVE),
]

for name, top, bot in zones:
    print(f"{name}  zone={bot:.2f}-{top:.2f}")
    test_fvg = FVGZone(direction=Direction.BEARISH, top=top, bottom=bot, ce=(top+bot)/2,
                       bar_index=0, timestamp=datetime(2026,4,1,tzinfo=timezone.utc),
                       quality=FVGQuality.VERY_AGGRESSIVE)
    last_close = float(window["close"].iloc[-1])
    print(f"  last M15 close: {last_close:.2f}, zone_bottom: {bot:.2f}, close < bottom? {last_close < bot}")
    # What's the highest high within last 12 bars
    last12 = window.tail(12)
    max_high = float(last12["high"].max())
    print(f"  max high in last 12 M15 bars: {max_high:.2f}, did it pierce zone_bottom? {max_high >= bot}")

    rejs = detect_htf_rejection(
        df_m15=window, htf_fvgs=[test_fvg], htf_obs=None,
        atr_m15=atr, lookback_m15=12, displacement_min=1.5, body_min_pct=0.55,
        htf_timeframe="H4",
    )
    print(f"  detector result: {len(rejs)} rejections fired")

    # Also try looser params
    rejs_loose = detect_htf_rejection(
        df_m15=window, htf_fvgs=[test_fvg], htf_obs=None,
        atr_m15=atr, lookback_m15=24, displacement_min=1.0, body_min_pct=0.40,
        htf_timeframe="H4",
    )
    print(f"  detector (loose params): {len(rejs_loose)} rejections fired")
    print()

# Manually walk the rejection-bar finder to see WHY it skipped
print("="*60)
print("Manual walk: which M15 bars in last 12 even tag the OB 22-04 (2386.73-2403.55)?")
zone_bot = 2386.73
last12 = window.tail(12)
for i, (ts, row) in enumerate(last12.iterrows()):
    pierced = row["high"] >= zone_bot
    closed_below = row["close"] < zone_bot
    rng = row["high"] - row["low"]
    body = abs(row["close"] - row["open"])
    body_pct = body/rng*100 if rng > 0 else 0
    disp_ratio = rng / atr if atr > 0 else 0
    print(f"  [{i:>2}] {ts} H={row['high']:7.2f} L={row['low']:7.2f} C={row['close']:7.2f}  "
          f"pierced={pierced}  closed_below_zone={closed_below}  range/ATR={disp_ratio:.2f}  body%={body_pct:.0f}")
