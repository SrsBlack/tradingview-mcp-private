"""Map loss/win patterns across all closed ICT_Bridge trades.

For each closed trade, compute the same diagnostic features we
extracted for today's 3 losses:

  bias_w1, bias_d1, bias_h4, bias_h1, bias_m15
    Direction (BULLISH/BEARISH/NEUTRAL) at entry, from swing structure.

  trend_d1_pct, trend_h4_pct, trend_h1_pct
    % change of close over last 20 bars on each TF.

  trade_vs_d1, trade_vs_h4, trade_vs_h1
    Does trade direction agree with the timeframe's bias and trend?

  ipda_pct                    Where in 20d range was entry?
  htf_opposing_fvg_dist_pct   Distance to nearest HTF FVG that opposes trade.
  htf_opposing_fvg_count_05   Count of opposing HTF FVGs within 0.5% of entry
                               (proxy for "stacked support/resistance against").

  forming_bar_kind  How was the H4 bar that contained the entry shaping?
                   - "with_trade"   = bar going trade direction at entry
                   - "against"      = bar going opposite (the SOLUSD pattern)
                   - "indecisive"   = small range
                   - "n/a"          = entry on bar boundary

  sl_breach_kind    "wick_only" or "body_close" (where applicable)

Then print a side-by-side comparison: how often does each pattern fire
on losers vs winners? Patterns with high loser_rate AND low winner_rate
are gate candidates.

Usage:
  PYTHONUTF8=1 python scripts/bench_loss_pattern_map.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import MetaTrader5 as mt5

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(1, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from analysis.structure import detect_swings, classify_structure, get_current_bias
from analysis.fvg import detect_fvgs
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


def fetch_at(symbol: str, tf: str, end_ts: datetime, count: int = 200) -> pd.DataFrame | None:
    rates = mt5.copy_rates_range(symbol, TF_MAP[tf],
                                  end_ts - timedelta(days=120), end_ts)
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
    except Exception:
        return 'ERR'


def trend_pct(df: pd.DataFrame, n: int = 20) -> float | None:
    if df is None or len(df) < n + 1:
        return None
    last = float(df['close'].iloc[-1])
    earlier = float(df['close'].iloc[-n])
    return (last - earlier) / earlier * 100


def ipda_pct(df_d1: pd.DataFrame, ref_price: float, days: int = 20) -> float | None:
    if df_d1 is None or len(df_d1) < days:
        return None
    sub = df_d1.iloc[-days:]
    rng_lo, rng_hi = float(sub['low'].min()), float(sub['high'].max())
    if rng_hi <= rng_lo: return None
    return (ref_price - rng_lo) / (rng_hi - rng_lo) * 100.0


def opposing_fvg_metrics(df_h4: pd.DataFrame, side: str, entry: float):
    """Return (nearest_dist_pct, count_within_0.5pct)."""
    if df_h4 is None or len(df_h4) < 30:
        return None, 0
    fvgs = detect_fvgs(df_h4.tail(200), max_age_bars=120,
                        min_quality=FVGQuality.AGGRESSIVE)
    opposing = []
    for f in fvgs:
        if side == 'SELL' and f.direction == Direction.BULLISH and f.top < entry:
            dist = (entry - f.top) / entry * 100
            opposing.append(dist)
        elif side == 'BUY' and f.direction == Direction.BEARISH and f.bottom > entry:
            dist = (f.bottom - entry) / entry * 100
            opposing.append(dist)
    if not opposing:
        return None, 0
    opposing.sort()
    count_close = sum(1 for d in opposing if d < 0.5)
    return opposing[0], count_close


def forming_bar_kind(df_h4: pd.DataFrame, side: str, entry_ts: datetime, entry: float) -> str:
    """The H4 bar containing the entry — was it trending with or against the trade?

    df_h4 fetched should include the bar containing entry_ts (since fetch_at goes
    up to end_ts).
    """
    if df_h4 is None or len(df_h4) == 0:
        return 'n/a'
    # Find the bar whose START is closest at-or-before entry_ts
    candidates = df_h4[df_h4.index <= entry_ts]
    if len(candidates) == 0:
        return 'n/a'
    bar = candidates.iloc[-1]
    bar_open = float(bar['open'])
    bar_close = float(bar['close'])
    bar_high = float(bar['high'])
    bar_low = float(bar['low'])
    bar_range = bar_high - bar_low
    if bar_range <= 0:
        return 'indecisive'
    # Use ATR proxy: if range is small, indecisive
    atr_proxy = float((df_h4['high'] - df_h4['low']).iloc[-14:].mean())
    if bar_range < 0.5 * atr_proxy:
        return 'indecisive'
    # Direction of the bar so far
    bar_bull = bar_close > bar_open
    if side == 'BUY' and bar_bull:
        return 'with_trade'
    if side == 'SELL' and not bar_bull:
        return 'with_trade'
    return 'against'


def fetch_trades_with_pnl():
    if not mt5.initialize(path=MT5_PATH, login=MT5_LOGIN,
                          password=MT5_PASSWORD, server=MT5_SERVER):
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    try:
        deals = mt5.history_deals_get(
            datetime(2026, 4, 1, tzinfo=timezone.utc),
            datetime.now(timezone.utc) + timedelta(hours=1)
        ) or []
    finally:
        pass  # Keep MT5 open for OHLCV fetches
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
            'exit_price': closes[-1].price,
            'exit_time': datetime.fromtimestamp(closes[-1].time, tz=timezone.utc),
            'pnl_usd': pnl,
        })
    out.sort(key=lambda t: t['entry_time'])
    return out


def main() -> int:
    print("=" * 100)
    print("Loss-pattern map — diagnostic features per closed trade")
    print("=" * 100)

    trades = fetch_trades_with_pnl()
    print(f"\nTotal closed ICT_Bridge trades: {len(trades)}")

    # Filter to cacheable symbols (skip JPY pairs without M15 cache, etc.)
    rows = []
    for t in trades:
        sym = t['symbol']
        entry = t['entry_price']
        side = t['side']
        ets = t['entry_time']
        # Fetch up to entry time
        df_w1 = fetch_at(sym, 'W1', ets, 60)
        df_d1 = fetch_at(sym, 'D1', ets, 100)
        df_h4 = fetch_at(sym, 'H4', ets, 200)
        df_h1 = fetch_at(sym, 'H1', ets, 200)
        df_m15 = fetch_at(sym, 'M15', ets, 200)

        if df_h4 is None or df_d1 is None:
            continue

        row = {
            'sym': sym,
            'side': side,
            'entry_ts': ets,
            'pnl': t['pnl_usd'],
            'is_winner': t['pnl_usd'] > 0,
            'bias_w1': bias(df_w1, 2),
            'bias_d1': bias(df_d1, 3),
            'bias_h4': bias(df_h4, 5),
            'bias_h1': bias(df_h1, 5),
            'bias_m15': bias(df_m15, 5),
            'trend_d1': trend_pct(df_d1, 20),
            'trend_h4': trend_pct(df_h4, 20),
            'trend_h1': trend_pct(df_h1, 20),
            'ipda': ipda_pct(df_d1, entry, 20),
        }
        nearest, count_close = opposing_fvg_metrics(df_h4, side, entry)
        row['htf_opposing_dist'] = nearest
        row['htf_opposing_count_05'] = count_close
        row['forming_bar'] = forming_bar_kind(df_h4, side, ets, entry)

        # Pattern flags
        trade_dir = 'BULLISH' if side == 'BUY' else 'BEARISH'
        row['vs_d1_disagree'] = (row['bias_d1'] != trade_dir
                                 and row['bias_d1'] in ('BULLISH', 'BEARISH'))
        row['vs_h1_disagree'] = (row['bias_h1'] != trade_dir
                                 and row['bias_h1'] in ('BULLISH', 'BEARISH'))
        # Strong opposing trend (HTF moved >1.5% against trade in last 20 bars)
        if row['trend_d1'] is not None:
            if side == 'BUY' and row['trend_d1'] < -1.5:
                row['strong_opposing_d1_trend'] = True
            elif side == 'SELL' and row['trend_d1'] > 1.5:
                row['strong_opposing_d1_trend'] = True
            else:
                row['strong_opposing_d1_trend'] = False
        if row['trend_h4'] is not None:
            if side == 'BUY' and row['trend_h4'] < -1.5:
                row['strong_opposing_h4_trend'] = True
            elif side == 'SELL' and row['trend_h4'] > 1.5:
                row['strong_opposing_h4_trend'] = True
            else:
                row['strong_opposing_h4_trend'] = False
        # Stacked opposing FVG within 0.5%
        row['stacked_opposing_fvg'] = row['htf_opposing_count_05'] >= 2
        # Forming bar against the trade
        row['forming_against'] = row['forming_bar'] == 'against'
        # Extreme IPDA fade
        if row['ipda'] is not None:
            if side == 'BUY' and row['ipda'] > 80:
                row['ipda_fade_extreme'] = True
            elif side == 'SELL' and row['ipda'] < 20:
                row['ipda_fade_extreme'] = True
            else:
                row['ipda_fade_extreme'] = False

        rows.append(row)

    mt5.shutdown()

    # ---------- Per-trade table ----------
    print(f"\nAnalyzed: {len(rows)} trades")
    print(f"\n{'tag':<5} {'sym':<14} {'side':<5} {'pnl':>9} "
          f"{'bias_d1':<9} {'bias_h1':<9} {'trnd_d1':>8} {'trnd_h4':>8} "
          f"{'ipda':>5} {'opp_d':>6} {'opp_n':>3} {'forming':<12}")
    print("-" * 110)
    for r in rows:
        tag = '✓' if r['is_winner'] else '✗'
        d1 = (r['trend_d1'] if r['trend_d1'] is not None else 0)
        h4 = (r['trend_h4'] if r['trend_h4'] is not None else 0)
        ipda = (r['ipda'] if r['ipda'] is not None else 0)
        opp_d = (r['htf_opposing_dist'] if r['htf_opposing_dist'] is not None else 99)
        print(f" {tag}    {r['sym']:<14} {r['side']:<5} "
              f"${r['pnl']:>+8.2f} {r['bias_d1']:<9} {r['bias_h1']:<9} "
              f"{d1:>+7.1f}% {h4:>+7.1f}% "
              f"{ipda:>4.0f}% {opp_d:>5.2f}% {r['htf_opposing_count_05']:>3} "
              f"{r['forming_bar']:<12}")

    # ---------- Pattern-fire summary ----------
    winners = [r for r in rows if r['is_winner']]
    losers = [r for r in rows if not r['is_winner']]
    print(f"\nWinners: {len(winners)}  Losers: {len(losers)}")

    patterns = [
        ('vs_d1_disagree',          'Trade direction disagrees with D1 bias'),
        ('vs_h1_disagree',          'Trade direction disagrees with H1 bias'),
        ('strong_opposing_d1_trend', 'D1 trend strongly opposes trade (>1.5%)'),
        ('strong_opposing_h4_trend', 'H4 trend strongly opposes trade (>1.5%)'),
        ('stacked_opposing_fvg',    'Stacked opposing HTF FVG within 0.5%'),
        ('forming_against',         'Entry H4 bar forming AGAINST trade dir'),
        ('ipda_fade_extreme',       'Trade fades IPDA 20d extreme (>80% or <20%)'),
    ]

    print(f"\n{'pattern':<55} {'win-fire':>10} {'lose-fire':>10} {'edge':>6}")
    print("-" * 90)
    for key, label in patterns:
        w_fire = sum(1 for r in winners if r.get(key, False))
        l_fire = sum(1 for r in losers if r.get(key, False))
        w_pct = w_fire / len(winners) * 100 if winners else 0
        l_pct = l_fire / len(losers) * 100 if losers else 0
        edge = l_pct - w_pct  # positive = pattern fires more on losers (good gate)
        marker = '  ★' if edge >= 20 and l_fire >= 3 else ''
        print(f"{label:<55} {w_fire}/{len(winners)} ({w_pct:>4.0f}%) "
              f"{l_fire}/{len(losers)} ({l_pct:>4.0f}%) {edge:>+5.0f}%{marker}")
    print("\n★ = candidate gate (>=20% loser-edge, >=3 loser hits)")

    # ---------- Dollar impact of each pattern as a hard gate ----------
    print("\n" + "=" * 100)
    print("Dollar impact if each pattern was used as a HARD-SKIP gate")
    print("=" * 100)
    print(f"{'pattern':<55} {'winners $ blocked':>18} {'losers $ saved':>16} {'NET':>10}")
    print("-" * 105)
    for key, label in patterns:
        w_blocked = sum(r['pnl'] for r in winners if r.get(key, False))
        l_saved = -sum(r['pnl'] for r in losers if r.get(key, False))
        net = -w_blocked + l_saved
        print(f"{label:<55} {w_blocked:>+18.2f} {l_saved:>+16.2f} {net:>+10.2f}")

    # ---------- Combined patterns ----------
    print("\n" + "=" * 100)
    print("Combined patterns — multiple conditions firing together")
    print("=" * 100)
    combos = [
        ('vs_h1_disagree + forming_against',
         lambda r: r.get('vs_h1_disagree') and r.get('forming_against')),
        ('strong_opposing_d1_trend + stacked_opposing_fvg',
         lambda r: r.get('strong_opposing_d1_trend') and r.get('stacked_opposing_fvg')),
        ('any_strong_opposing_trend',
         lambda r: r.get('strong_opposing_d1_trend') or r.get('strong_opposing_h4_trend')),
        ('forming_against + ipda_fade_extreme',
         lambda r: r.get('forming_against') and r.get('ipda_fade_extreme')),
    ]
    print(f"\n{'pattern':<55} {'win-fire':>10} {'lose-fire':>10} {'edge':>6}")
    print("-" * 90)
    for label, pred in combos:
        try:
            w = sum(1 for r in winners if pred(r))
            l = sum(1 for r in losers if pred(r))
        except Exception as e:
            print(f"  ERR: {label}: {e}")
            continue
        w_pct = w / len(winners) * 100 if winners else 0
        l_pct = l / len(losers) * 100 if losers else 0
        edge = l_pct - w_pct
        marker = '  ★' if edge >= 20 and l >= 3 else ''
        print(f"{label:<55} {w}/{len(winners)} ({w_pct:>4.0f}%) "
              f"{l}/{len(losers)} ({l_pct:>4.0f}%) {edge:>+5.0f}%{marker}")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
