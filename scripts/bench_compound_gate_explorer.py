"""Compound-gate explorer.

Goal: find a 2- or 3-feature AND combination that blocks ZERO (or
near-zero) winner dollars while catching multiple loser dollars.

Method:
  1. Load all 53 closed ICT_Bridge trades.
  2. For each, compute ~12 boolean features per trade — bias, trend,
     forming-bar, sweep, displacement, IPDA, key-level proximity,
     M15-vs-H4 alignment, range-position.
  3. Iterate over ALL pairs of features. For each pair AND combination:
     - Count winners that fire it
     - Count losers that fire it
     - Compute dollar impact (winners blocked, losers saved, net)
  4. Rank by net dollar impact. Print top 30.
  5. Critical filter: only show pairs where winners blocked $ <= $200.
     The winners bench failure on the single forming-H4 gate was driven
     by ~$500 of blocked winners. Compound gates that stay under that
     ceiling have a chance of passing the winners bench.

Usage:
  PYTHONUTF8=1 python scripts/bench_compound_gate_explorer.py
"""
from __future__ import annotations

import sys
from itertools import combinations
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import MetaTrader5 as mt5

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(1, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from analysis.structure import detect_swings, classify_structure, get_current_bias
from analysis.fvg import detect_fvgs
from analysis.liquidity import scan_sweeps
from core.types import Direction, FVGQuality

MT5_LOGIN = 1513140458
MT5_PASSWORD = "L!$q1k@4Z"
MT5_SERVER = "FTMO-Demo"
MT5_PATH = "C:/Program Files/METATRADER5.1/terminal64.exe"

TF_MAP = {
    'M15': mt5.TIMEFRAME_M15,
    'H1': mt5.TIMEFRAME_H1,
    'H4': mt5.TIMEFRAME_H4,
    'D1': mt5.TIMEFRAME_D1,
    'W1': mt5.TIMEFRAME_W1,
}


def fetch_at(symbol: str, tf: str, end_ts: datetime, count: int = 200):
    rates = mt5.copy_rates_range(symbol, TF_MAP[tf],
                                  end_ts - timedelta(days=120), end_ts)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
    df = df.set_index('time').rename(columns={'tick_volume': 'volume'})
    return df[['open', 'high', 'low', 'close', 'volume']].tail(count)


def bias_str(df, lookback):
    if df is None or len(df) < lookback + 2:
        return 'INSUF'
    try:
        sw = detect_swings(df, lookback=lookback)
        _, ev = classify_structure(sw)
        return get_current_bias(ev).name
    except Exception:
        return 'ERR'


def compute_features(trade, df_w1, df_d1, df_h4, df_h1, df_m15):
    """Return dict of boolean features for this trade."""
    side = trade['side']
    entry = trade['entry_price']
    ets = trade['entry_time']
    trade_dir = 'BULLISH' if side == 'BUY' else 'BEARISH'

    f = {}

    # Biases
    bw1 = bias_str(df_w1, 2)
    bd1 = bias_str(df_d1, 3)
    bh4 = bias_str(df_h4, 5)
    bh1 = bias_str(df_h1, 5)
    bm15 = bias_str(df_m15, 5)

    f['bias_w1_oppose'] = bw1 != trade_dir and bw1 in ('BULLISH', 'BEARISH')
    f['bias_d1_oppose'] = bd1 != trade_dir and bd1 in ('BULLISH', 'BEARISH')
    f['bias_h4_oppose'] = bh4 != trade_dir and bh4 in ('BULLISH', 'BEARISH')
    f['bias_h1_oppose'] = bh1 != trade_dir and bh1 in ('BULLISH', 'BEARISH')
    # MTF disagreement counts
    opposing_count = sum([f['bias_w1_oppose'], f['bias_d1_oppose'], f['bias_h4_oppose']])
    f['mtf_2plus_oppose'] = opposing_count >= 2
    f['mtf_all_oppose'] = opposing_count >= 3

    # Trend strength
    if df_d1 is not None and len(df_d1) >= 21:
        td1 = (float(df_d1['close'].iloc[-1]) - float(df_d1['close'].iloc[-20])) / float(df_d1['close'].iloc[-20]) * 100
        f['d1_trend_oppose_strong'] = ((side == 'BUY' and td1 < -1.5)
                                        or (side == 'SELL' and td1 > 1.5))
    else:
        f['d1_trend_oppose_strong'] = False
    if df_h4 is not None and len(df_h4) >= 21:
        th4 = (float(df_h4['close'].iloc[-1]) - float(df_h4['close'].iloc[-20])) / float(df_h4['close'].iloc[-20]) * 100
        f['h4_trend_oppose_strong'] = ((side == 'BUY' and th4 < -1.5)
                                        or (side == 'SELL' and th4 > 1.5))
        f['h4_trend_oppose_mild'] = ((side == 'BUY' and th4 < -0.5)
                                      or (side == 'SELL' and th4 > 0.5))
    else:
        f['h4_trend_oppose_strong'] = False
        f['h4_trend_oppose_mild'] = False
    if df_h1 is not None and len(df_h1) >= 21:
        th1 = (float(df_h1['close'].iloc[-1]) - float(df_h1['close'].iloc[-20])) / float(df_h1['close'].iloc[-20]) * 100
        f['h1_trend_oppose_strong'] = ((side == 'BUY' and th1 < -1.5)
                                        or (side == 'SELL' and th1 > 1.5))
    else:
        f['h1_trend_oppose_strong'] = False

    # Forming H4 bar
    if df_h4 is not None and len(df_h4) >= 15:
        candidates = df_h4[df_h4.index <= ets]
        if len(candidates) > 0:
            bar = candidates.iloc[-1]
            bar_o, bar_c, bar_h, bar_l = float(bar['open']), float(bar['close']), float(bar['high']), float(bar['low'])
            bar_range = bar_h - bar_l
            atr_h4 = float((df_h4['high'] - df_h4['low']).iloc[-14:].mean())
            bar_bull = bar_c > bar_o
            f['forming_h4_against'] = (
                (side == 'BUY' and not bar_bull and bar_range >= 0.5 * atr_h4) or
                (side == 'SELL' and bar_bull and bar_range >= 0.5 * atr_h4)
            )
            f['forming_h4_strong_against'] = (
                (side == 'BUY' and not bar_bull and bar_range >= 1.0 * atr_h4) or
                (side == 'SELL' and bar_bull and bar_range >= 1.0 * atr_h4)
            )
        else:
            f['forming_h4_against'] = False
            f['forming_h4_strong_against'] = False
    else:
        f['forming_h4_against'] = False
        f['forming_h4_strong_against'] = False

    # IPDA
    if df_d1 is not None and len(df_d1) >= 20:
        sub = df_d1.iloc[-20:]
        rng_lo, rng_hi = float(sub['low'].min()), float(sub['high'].max())
        if rng_hi > rng_lo:
            ipda = (entry - rng_lo) / (rng_hi - rng_lo) * 100
            f['ipda_extreme_fade'] = ((side == 'BUY' and ipda > 80)
                                       or (side == 'SELL' and ipda < 20))
            f['ipda_extreme_chase'] = ((side == 'BUY' and ipda > 90)
                                        or (side == 'SELL' and ipda < 10))
        else:
            f['ipda_extreme_fade'] = False
            f['ipda_extreme_chase'] = False
    else:
        f['ipda_extreme_fade'] = False
        f['ipda_extreme_chase'] = False

    # Stacked opposing FVGs within 0.5%
    f['stacked_opposing_fvg'] = False
    f['stacked_opposing_fvg_3plus'] = False
    if df_h4 is not None and len(df_h4) >= 30:
        try:
            fvgs = detect_fvgs(df_h4.tail(200), max_age_bars=120, min_quality=FVGQuality.AGGRESSIVE)
            opposing = []
            for fvg in fvgs:
                if side == 'SELL' and fvg.direction == Direction.BULLISH and fvg.top < entry:
                    dist = (entry - fvg.top) / entry * 100
                    if dist < 0.5:
                        opposing.append(dist)
                elif side == 'BUY' and fvg.direction == Direction.BEARISH and fvg.bottom > entry:
                    dist = (fvg.bottom - entry) / entry * 100
                    if dist < 0.5:
                        opposing.append(dist)
            f['stacked_opposing_fvg'] = len(opposing) >= 2
            f['stacked_opposing_fvg_3plus'] = len(opposing) >= 3
        except Exception:
            pass

    # Recent M15 sweep present (within last 12 M15 bars before entry)
    f['m15_sweep_recent'] = False
    if df_m15 is not None and len(df_m15) >= 30:
        try:
            sub = df_m15.tail(60)
            sweeps = scan_sweeps(sub, lookback=20)
            recent = [s for s in (sweeps or []) if s.bar_index >= len(sub) - 12]
            f['m15_sweep_recent'] = len(recent) > 0
        except Exception:
            pass

    # Symbol class
    base = trade['symbol'].split(':')[-1].split('.')[0]
    f['is_jpy_pair'] = 'JPY' in base
    f['is_index'] = base in ('US500', 'US100', 'GER40', 'YM1!', 'US30')
    f['is_crypto'] = base in ('BTCUSD', 'ETHUSD', 'SOLUSD', 'DOGEUSD')

    # Direction
    f['is_buy'] = side == 'BUY'
    f['is_sell'] = side == 'SELL'

    return f


def fetch_trades_with_pnl():
    if not mt5.initialize(path=MT5_PATH, login=MT5_LOGIN,
                          password=MT5_PASSWORD, server=MT5_SERVER):
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    deals = mt5.history_deals_get(
        datetime(2026, 4, 1, tzinfo=timezone.utc),
        datetime.now(timezone.utc) + timedelta(hours=1)
    ) or []
    by_pos = defaultdict(list)
    for d in deals:
        by_pos[d.position_id].append(d)
    out = []
    for pid, ds in by_pos.items():
        opens = [d for d in ds if d.entry == 0]
        closes = [d for d in ds if d.entry == 1]
        if not opens or not closes:
            continue
        od = opens[0]
        if "ICT_Bridge" not in (od.comment or ""):
            continue
        side = 'BUY' if od.type == 0 else 'SELL'
        pnl = sum(d.profit for d in ds)
        out.append({
            'symbol': od.symbol,
            'side': side,
            'entry_price': od.price,
            'entry_time': datetime.fromtimestamp(od.time, tz=timezone.utc),
            'pnl_usd': pnl,
        })
    out.sort(key=lambda t: t['entry_time'])
    return out


def main():
    print("=" * 100)
    print("Compound-gate explorer — find 2-feature AND gates that block 0$ winners")
    print("=" * 100)

    trades = fetch_trades_with_pnl()
    print(f"\nClosed ICT_Bridge trades: {len(trades)}")

    print("\nExtracting features per trade (this fetches OHLCV)...")
    rows = []
    for t in trades:
        sym = t['symbol']
        ets = t['entry_time']
        df_w1 = fetch_at(sym, 'W1', ets, 60)
        df_d1 = fetch_at(sym, 'D1', ets, 100)
        df_h4 = fetch_at(sym, 'H4', ets, 200)
        df_h1 = fetch_at(sym, 'H1', ets, 200)
        df_m15 = fetch_at(sym, 'M15', ets, 200)
        if df_h4 is None or df_d1 is None:
            continue
        feats = compute_features(t, df_w1, df_d1, df_h4, df_h1, df_m15)
        rows.append({
            'sym': sym, 'side': t['side'], 'entry_ts': ets, 'pnl': t['pnl_usd'],
            'is_winner': t['pnl_usd'] > 0,
            **feats,
        })
    mt5.shutdown()
    print(f"Feature rows built: {len(rows)}")

    winners = [r for r in rows if r['is_winner']]
    losers = [r for r in rows if not r['is_winner']]
    print(f"Winners: {len(winners)} (total $+{sum(r['pnl'] for r in winners):.2f})")
    print(f"Losers:  {len(losers)} (total ${sum(r['pnl'] for r in losers):+.2f})")

    # Get all feature keys
    feature_keys = sorted(set(k for r in rows for k in r.keys()
                              if k not in {'sym', 'side', 'entry_ts', 'pnl', 'is_winner'}
                              and isinstance(r.get(k), bool)))
    print(f"Boolean features: {len(feature_keys)}")

    # Single feature scoring
    print("\n" + "=" * 100)
    print("SINGLE FEATURES — fire rates + dollar impact")
    print("=" * 100)
    print(f"\n{'feature':<35} {'winN':>5} {'win$':>9} {'losN':>5} {'los$':>9} {'NET$':>9}")
    print("-" * 80)
    single = []
    for k in feature_keys:
        wn = sum(1 for r in winners if r.get(k))
        ln = sum(1 for r in losers if r.get(k))
        wd = sum(r['pnl'] for r in winners if r.get(k))
        ld = -sum(r['pnl'] for r in losers if r.get(k))
        net = -wd + ld
        single.append((k, wn, ln, wd, ld, net))
    single.sort(key=lambda x: -x[5])
    for k, wn, ln, wd, ld, net in single:
        print(f"{k:<35} {wn:>5} {wd:>+9.0f} {ln:>5} {ld:>+9.0f} {net:>+9.0f}")

    # 2-feature AND combinations
    print("\n" + "=" * 100)
    print("2-FEATURE AND combinations — top 25 by NET dollar impact")
    print("(filter: winner $ blocked <= $200 to give it a chance vs winners-bench)")
    print("=" * 100)

    pair_results = []
    for a, b in combinations(feature_keys, 2):
        wn = sum(1 for r in winners if r.get(a) and r.get(b))
        ln = sum(1 for r in losers if r.get(a) and r.get(b))
        if ln < 2:
            continue  # need at least 2 losers
        wd = sum(r['pnl'] for r in winners if r.get(a) and r.get(b))
        ld = -sum(r['pnl'] for r in losers if r.get(a) and r.get(b))
        net = -wd + ld
        pair_results.append((a, b, wn, ln, wd, ld, net))

    pair_results.sort(key=lambda x: -x[6])

    # Filter: winners blocked dollars <= 200 (passes winners bench discipline)
    print(f"\n{'pair':<70} {'wN':>3} {'win$':>7} {'lN':>3} {'lose$':>7} {'NET$':>8}")
    print("-" * 105)
    shown = 0
    for a, b, wn, ln, wd, ld, net in pair_results:
        if wd > 200:
            continue
        label = f"{a} AND {b}"
        print(f"{label:<70} {wn:>3} {wd:>+7.0f} {ln:>3} {ld:>+7.0f} {net:>+8.0f}")
        shown += 1
        if shown >= 25:
            break

    # 3-feature AND
    print("\n" + "=" * 100)
    print("3-FEATURE AND combinations — top 15 by NET (winners blocked <= $50)")
    print("=" * 100)
    triple_results = []
    for a, b, c in combinations(feature_keys, 3):
        wn = sum(1 for r in winners if r.get(a) and r.get(b) and r.get(c))
        ln = sum(1 for r in losers if r.get(a) and r.get(b) and r.get(c))
        if ln < 2:
            continue
        wd = sum(r['pnl'] for r in winners if r.get(a) and r.get(b) and r.get(c))
        ld = -sum(r['pnl'] for r in losers if r.get(a) and r.get(b) and r.get(c))
        net = -wd + ld
        triple_results.append((a, b, c, wn, ln, wd, ld, net))
    triple_results.sort(key=lambda x: -x[7])
    print(f"\n{'triple':<90} {'wN':>3} {'win$':>7} {'lN':>3} {'lose$':>7} {'NET$':>8}")
    print("-" * 125)
    shown = 0
    for a, b, c, wn, ln, wd, ld, net in triple_results:
        if wd > 50:
            continue
        label = f"{a} AND {b} AND {c}"
        print(f"{label:<90} {wn:>3} {wd:>+7.0f} {ln:>3} {ld:>+7.0f} {net:>+8.0f}")
        shown += 1
        if shown >= 15:
            break

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
