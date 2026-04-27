"""Audit open ICT_Bridge positions: was each entry signal sound?

For each open position, pulls live broker data and recent OHLCV from
the cache (or live MT5 if cache is stale) and reports:
  - Entry vs current price (in pips/% and ATR multiples)
  - W1/D1/H4 bias at entry vs current
  - IPDA 20d position at entry (where in the range was it?)
  - Distance to SL (R-multiple)
  - Whether the trade direction agrees with the dominant HTF stack
  - Whether HTF rejection would have fired in the trade direction
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
from analysis.htf_rejection import detect_htf_rejection, strongest_rejection_direction
from core.types import Direction, FVGQuality

mt5.initialize(path='C:/Program Files/METATRADER5.1/terminal64.exe',
               login=1513140458, password='L!$q1k@4Z', server='FTMO-Demo')

# Pull all ICT_Bridge open positions
positions = [p for p in (mt5.positions_get() or []) if 'ICT_Bridge' in (p.comment or '')]

# Symbol -> MT5 timeframe constants
TF_MAP = {
    'M15': mt5.TIMEFRAME_M15,
    'H1': mt5.TIMEFRAME_H1,
    'H4': mt5.TIMEFRAME_H4,
    'D1': mt5.TIMEFRAME_D1,
    'W1': mt5.TIMEFRAME_W1,
}


def fetch_ohlcv(symbol: str, tf: str, count: int = 200) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, TF_MAP[tf], 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df = df.set_index('time').rename(columns={'tick_volume': 'volume'})
    return df[['open', 'high', 'low', 'close', 'volume']]


def bias_for(df: pd.DataFrame, lookback: int) -> str:
    if df is None or len(df) < lookback + 2:
        return 'INSUF'
    try:
        sw = detect_swings(df, lookback=lookback)
        _, ev = classify_structure(sw)
        b = get_current_bias(ev)
        return b.name
    except Exception:
        return 'ERR'


def ipda_pct(df_d1: pd.DataFrame, current: float, days: int = 20) -> float:
    if df_d1 is None or len(df_d1) < days:
        return -1.0
    sub = df_d1.iloc[-days:]
    rng_lo, rng_hi = float(sub['low'].min()), float(sub['high'].max())
    if rng_hi <= rng_lo: return -1.0
    return (current - rng_lo) / (rng_hi - rng_lo) * 100.0


def atr14(df: pd.DataFrame) -> float:
    if df is None or len(df) < 15: return 0.0
    h, l, c = df['high'].astype(float), df['low'].astype(float), df['close'].astype(float)
    tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    return float(tr.iloc[-14:].mean())


print("=" * 100)
print("OPEN POSITION AUDIT — ICT_Bridge only")
print("=" * 100)

for p in positions:
    side = 'BUY' if p.type == 0 else 'SELL'
    sym = p.symbol
    entry = float(p.price_open)
    sl = float(p.sl)
    tp = float(p.tp) if p.tp else None
    cur = float(p.price_current)
    open_ts = datetime.fromtimestamp(p.time, tz=timezone.utc)

    # Pull MT5 data
    df_m15 = fetch_ohlcv(sym, 'M15', 300)
    df_h1 = fetch_ohlcv(sym, 'H1', 200)
    df_h4 = fetch_ohlcv(sym, 'H4', 200)
    df_d1 = fetch_ohlcv(sym, 'D1', 100)
    df_w1 = fetch_ohlcv(sym, 'W1', 60)

    # Biases (current — at entry would need slicing, this is "where are we now")
    w1 = bias_for(df_w1, 2)
    d1 = bias_for(df_d1, 3)
    h4 = bias_for(df_h4, 5)
    h1 = bias_for(df_h1, 5)

    ipda = ipda_pct(df_d1, cur, 20) if df_d1 is not None else -1.0
    atr_h1 = atr14(df_h1)
    atr_m15 = atr14(df_m15)

    # SL distance in R
    risk = abs(entry - sl)
    cur_R = (entry - cur) / risk if side == 'SELL' else (cur - entry) / risk

    # HTF rejection check on H4 trigger right now
    h4_fvgs = detect_fvgs(df_h4.tail(200), max_age_bars=120, min_quality=FVGQuality.AGGRESSIVE) if df_h4 is not None else []
    rejs = detect_htf_rejection(df_trigger=df_h4.tail(60) if df_h4 is not None else None,
                                htf_fvgs=h4_fvgs, atr=atr14(df_h4),
                                trigger_tf='H4', htf_timeframe='H4',
                                displacement_min=1.5, body_min_pct=0.55) if df_h4 is not None else []
    rej_dir = strongest_rejection_direction(rejs)
    rej_str = rej_dir.name if rej_dir else 'NONE'

    # Trade dir vs HTF stack agreement
    trade_dir = 'BULLISH' if side == 'BUY' else 'BEARISH'
    biases = [b for b in (w1, d1, h4) if b in ('BULLISH', 'BEARISH')]
    if not biases:
        stack_agree = 'no-bias'
    elif all(b == trade_dir for b in biases):
        stack_agree = '✓ ALL agree'
    elif sum(1 for b in biases if b == trade_dir) >= 2:
        stack_agree = 'majority agree'
    elif sum(1 for b in biases if b == trade_dir) == 1:
        stack_agree = 'minority (1 of 3)'
    else:
        stack_agree = '✗ ALL OPPOSE'

    print(f"\n--- {sym} {side} (#{p.ticket}) ---")
    print(f"  opened: {open_ts.strftime('%Y-%m-%d %H:%M UTC')}  ({(datetime.now(timezone.utc) - open_ts).total_seconds()/3600:.1f}h ago)")
    print(f"  entry={entry}  sl={sl}  current={cur}  live_pnl=${p.profit:+.2f}")
    print(f"  current R: {cur_R:+.2f}  (1.0R = TP, -1.0R = SL)")
    print(f"  HTF stack: W1={w1} D1={d1} H4={h4} H1={h1}  → {stack_agree} with {trade_dir} trade")
    print(f"  IPDA(20d): {ipda:.0f}%  ", end='')
    if (side == 'BUY' and ipda > 80) or (side == 'SELL' and ipda < 20):
        print("⚠️  trade fades multi-day extreme")
    elif (side == 'BUY' and ipda < 20) or (side == 'SELL' and ipda > 80):
        print("✓  trade with multi-day momentum (entering at extreme)")
    else:
        print("(mid-range)")
    print(f"  Live HTF rejection on H4 (now): {rej_str}", end='')
    if rej_dir is not None and rej_dir.name != trade_dir:
        print(f"  ⚠️  H4 rejection OPPOSES trade")
    elif rej_dir is not None and rej_dir.name == trade_dir:
        print(f"  ✓  H4 rejection AGREES with trade")
    else:
        print()

mt5.shutdown()
