"""Post-mortem on today's 3 losses: UKOIL SELL, GBPJPY SELL, SOLUSD BUY.

For each loss, pull from MT5:
  - Entry/SL/exit prices and times
  - H4/H1/M15 OHLCV around entry — what was price doing?
  - HTF biases (W1, D1, H4 swing structure) AT ENTRY TIME (not now)
  - Whether the trade fought a clear trend or fought a clear range
  - Distance from entry to nearest HTF FVG/OB

Goal: name the SPECIFIC pattern the bridge missed for each loss.
Then look across all 3 — what's common?
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import MetaTrader5 as mt5

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(1, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from analysis.structure import detect_swings, classify_structure, get_current_bias
from analysis.fvg import detect_fvgs
from analysis.order_blocks import detect_order_blocks
from core.types import Direction, FVGQuality

mt5.initialize(path='C:/Program Files/METATRADER5.1/terminal64.exe',
               login=1513140458, password='L!$q1k@4Z', server='FTMO-Demo')

LOSSES = [
    ("UKOIL.cash",  434626685, "SELL", 107.685, 108.415, "2026-04-27 07:01"),
    ("GBPJPY",      434637439, "SELL", 215.543, 215.911, "2026-04-27 07:30"),
    ("SOLUSD",      434664607, "BUY",  85.270,  84.370,  "2026-04-27 08:08"),
]

TF_MAP = {
    'M15': mt5.TIMEFRAME_M15,
    'H1': mt5.TIMEFRAME_H1,
    'H4': mt5.TIMEFRAME_H4,
    'D1': mt5.TIMEFRAME_D1,
    'W1': mt5.TIMEFRAME_W1,
}


def fetch_at(symbol: str, tf: str, end_ts: datetime, count: int = 200) -> pd.DataFrame | None:
    rates = mt5.copy_rates_range(symbol, TF_MAP[tf], end_ts - timedelta(days=120), end_ts)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df = df.set_index('time').rename(columns={'tick_volume': 'volume'})
    return df[['open', 'high', 'low', 'close', 'volume']].tail(count)


def bias(df: pd.DataFrame, lookback: int) -> str:
    if df is None or len(df) < lookback + 2:
        return 'INSUF'
    try:
        sw = detect_swings(df, lookback=lookback)
        _, ev = classify_structure(sw)
        return get_current_bias(ev).name
    except Exception as e:
        return f'ERR:{type(e).__name__}'


def trend_score(df: pd.DataFrame, n: int = 20) -> str:
    """Simple trend test: % change over last N bars."""
    if df is None or len(df) < n + 1:
        return '?'
    last = float(df['close'].iloc[-1])
    earlier = float(df['close'].iloc[-n])
    pct = (last - earlier) / earlier * 100
    if pct > 1.5:
        return f'STRONG-UP ({pct:+.1f}%)'
    if pct > 0.3:
        return f'UP ({pct:+.1f}%)'
    if pct < -1.5:
        return f'STRONG-DOWN ({pct:+.1f}%)'
    if pct < -0.3:
        return f'DOWN ({pct:+.1f}%)'
    return f'FLAT ({pct:+.1f}%)'


def ipda_pct(df_d1: pd.DataFrame, ref_price: float, days: int = 20) -> float:
    if df_d1 is None or len(df_d1) < days:
        return -1.0
    sub = df_d1.iloc[-days:]
    rng_lo, rng_hi = float(sub['low'].min()), float(sub['high'].max())
    if rng_hi <= rng_lo: return -1.0
    return (ref_price - rng_lo) / (rng_hi - rng_lo) * 100.0


for sym, ticket, side, entry, exit_p, entry_time_str in LOSSES:
    entry_ts = pd.Timestamp(entry_time_str, tz='UTC')
    print("=" * 90)
    print(f"## {sym} {side} #{ticket}  entry={entry} -> SL exit={exit_p}")
    print(f"   entered: {entry_ts}  ({(datetime.now(timezone.utc) - entry_ts).total_seconds()/3600:.1f}h ago)")
    print("=" * 90)

    df_w1 = fetch_at(sym, 'W1', entry_ts.to_pydatetime(), 60)
    df_d1 = fetch_at(sym, 'D1', entry_ts.to_pydatetime(), 100)
    df_h4 = fetch_at(sym, 'H4', entry_ts.to_pydatetime(), 200)
    df_h1 = fetch_at(sym, 'H1', entry_ts.to_pydatetime(), 200)
    df_m15 = fetch_at(sym, 'M15', entry_ts.to_pydatetime(), 200)

    # Biases AT ENTRY
    w1_b = bias(df_w1, 2)
    d1_b = bias(df_d1, 3)
    h4_b = bias(df_h4, 5)
    h1_b = bias(df_h1, 5)
    m15_b = bias(df_m15, 5)

    print(f"\nBIAS AT ENTRY:")
    print(f"  W1={w1_b}  D1={d1_b}  H4={h4_b}  H1={h1_b}  M15={m15_b}")

    # Trend strength
    print(f"\nTREND STRENGTH (last 20 bars % change):")
    print(f"  D1: {trend_score(df_d1, 20)}")
    print(f"  H4: {trend_score(df_h4, 20)}")
    print(f"  H1: {trend_score(df_h1, 20)}")

    # IPDA
    if df_d1 is not None:
        print(f"\nIPDA POSITION at entry: {ipda_pct(df_d1, entry):.0f}% of 20d range")

    # Last 6 H4 candles before entry
    if df_h4 is not None:
        print(f"\nLAST 6 H4 CANDLES leading up to entry (newest last):")
        for ts, row in df_h4.tail(6).iterrows():
            color = '🟢' if row['close'] > row['open'] else '🔴'
            print(f"  {ts.strftime('%m-%d %H:%M')}  O={row['open']:>8.3f} H={row['high']:>8.3f} "
                  f"L={row['low']:>8.3f} C={row['close']:>8.3f}  {color}")

    # Distance from entry to nearest opposing HTF FVG
    if df_h4 is not None:
        h4_fvgs = detect_fvgs(df_h4.tail(200), max_age_bars=120, min_quality=FVGQuality.AGGRESSIVE)
        opposing = []
        for f in h4_fvgs:
            if side == 'SELL' and f.direction == Direction.BULLISH:
                # SELL into bullish FVG = into support
                if f.top < entry:
                    dist = (entry - f.top) / entry * 100
                    opposing.append((dist, f, "SUPPORT BELOW"))
            elif side == 'BUY' and f.direction == Direction.BEARISH:
                # BUY into bearish FVG = into resistance
                if f.bottom > entry:
                    dist = (f.bottom - entry) / entry * 100
                    opposing.append((dist, f, "RESISTANCE ABOVE"))
        if opposing:
            opposing.sort()
            print(f"\nNEAREST OPPOSING H4 FVG ({len(opposing)} total):")
            for dist, f, where in opposing[:3]:
                print(f"  {where}: {f.bottom:.3f}-{f.top:.3f}  ({dist:.2f}% from entry, "
                      f"formed {f.timestamp.strftime('%m-%d')})")
        else:
            print(f"\nNEAREST OPPOSING H4 FVG: none in dataset")

    # Did price exceed SL with conviction (full body candles) or just wick?
    df_post = fetch_at(sym, 'M15', entry_ts.to_pydatetime() + timedelta(hours=24))
    if df_post is not None:
        post = df_post[df_post.index >= entry_ts]
        if len(post) > 0:
            if side == 'SELL':
                breach = post[post['high'] >= float(exit_p)]
            else:
                breach = post[post['low'] <= float(exit_p)]
            if len(breach) > 0:
                first = breach.iloc[0]
                t = breach.index[0]
                # Did the SL hit on a wick or a body close?
                if side == 'SELL':
                    wicked_only = first['close'] < exit_p
                else:
                    wicked_only = first['close'] > exit_p
                kind = 'WICK ONLY (recovered)' if wicked_only else 'BODY CLOSE BEYOND'
                print(f"\nSL BREACH: {t.strftime('%m-%d %H:%M')} M15 bar  "
                      f"H={first['high']:.3f} L={first['low']:.3f} C={first['close']:.3f}  → {kind}")
                # Mins between entry and breach
                mins = (t - entry_ts).total_seconds() / 60
                print(f"  Time from entry to SL: {mins:.0f} minutes")

    print()

mt5.shutdown()
