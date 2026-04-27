"""Split-validation: does the 'forming H4 bar against trade' pattern
hold up out-of-sample?

Method:
  1. Take all 53 closed ICT_Bridge trades.
  2. Sort chronologically and split in half (oldest 26 vs newest 27).
  3. For each pattern, compute fire rate + dollar impact INDEPENDENTLY
     on each half. Critical: don't re-discover patterns on the test
     half — only measure the patterns we identified in-sample.
  4. Compare. A robust pattern shows similar edge on both halves.
     An overfit pattern looks great on the discovery half and weak
     or reversed on the test half.

Usage:
  PYTHONUTF8=1 python scripts/bench_split_validate.py
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

# Reuse the helpers from bench_loss_pattern_map (importable from same dir)
from scripts.bench_loss_pattern_map import (  # noqa: E402
    fetch_at, bias, trend_pct, ipda_pct,
    opposing_fvg_metrics, forming_bar_kind,
    fetch_trades_with_pnl,
    MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH,
)


def build_rows(trades):
    rows = []
    for t in trades:
        sym = t['symbol']
        entry = t['entry_price']
        side = t['side']
        ets = t['entry_time']
        df_d1 = fetch_at(sym, 'D1', ets, 100)
        df_h4 = fetch_at(sym, 'H4', ets, 200)
        df_h1 = fetch_at(sym, 'H1', ets, 200)
        if df_h4 is None or df_d1 is None:
            continue
        bias_d1 = bias(df_d1, 3)
        bias_h1 = bias(df_h1, 5)
        trade_dir = 'BULLISH' if side == 'BUY' else 'BEARISH'
        td1 = trend_pct(df_d1, 20)
        th4 = trend_pct(df_h4, 20)
        ipda = ipda_pct(df_d1, entry, 20)
        nearest, count_close = opposing_fvg_metrics(df_h4, side, entry)
        forming = forming_bar_kind(df_h4, side, ets, entry)

        row = {
            'sym': sym, 'side': side, 'entry_ts': ets, 'pnl': t['pnl_usd'],
            'is_winner': t['pnl_usd'] > 0,
            'bias_d1': bias_d1, 'bias_h1': bias_h1,
            'trend_d1': td1, 'trend_h4': th4, 'ipda': ipda,
            'htf_opposing_count_05': count_close,
            'forming_bar': forming,
            'forming_against': forming == 'against',
            'vs_d1_disagree': bias_d1 != trade_dir and bias_d1 in ('BULLISH', 'BEARISH'),
            'vs_h1_disagree': bias_h1 != trade_dir and bias_h1 in ('BULLISH', 'BEARISH'),
            'stacked_opposing_fvg': count_close >= 2,
            'ipda_fade_extreme': (
                (side == 'BUY' and ipda is not None and ipda > 80)
                or (side == 'SELL' and ipda is not None and ipda < 20)
            ),
        }
        if td1 is not None:
            row['strong_opposing_d1_trend'] = (
                (side == 'BUY' and td1 < -1.5) or (side == 'SELL' and td1 > 1.5)
            )
        else:
            row['strong_opposing_d1_trend'] = False
        if th4 is not None:
            row['strong_opposing_h4_trend'] = (
                (side == 'BUY' and th4 < -1.5) or (side == 'SELL' and th4 > 1.5)
            )
        else:
            row['strong_opposing_h4_trend'] = False
        rows.append(row)
    return rows


def report_half(label, rows, patterns):
    winners = [r for r in rows if r['is_winner']]
    losers = [r for r in rows if not r['is_winner']]
    print(f"\n--- {label} ({len(rows)} trades: {len(winners)}W / {len(losers)}L) ---")
    if rows:
        first_ts = min(r['entry_ts'] for r in rows)
        last_ts = max(r['entry_ts'] for r in rows)
        print(f"   span: {first_ts.strftime('%m-%d %H:%M')} -> "
              f"{last_ts.strftime('%m-%d %H:%M')}")
    print(f"   {'pattern':<48} {'win-fire':>10} {'lose-fire':>10} "
          f"{'edge%':>6} {'win$':>8} {'lose$':>8} {'NET$':>8}")
    print("   " + "-" * 100)
    out_rows = []
    for key, label_p in patterns:
        w_fire = sum(1 for r in winners if r.get(key, False))
        l_fire = sum(1 for r in losers if r.get(key, False))
        w_pct = w_fire / len(winners) * 100 if winners else 0
        l_pct = l_fire / len(losers) * 100 if losers else 0
        edge = l_pct - w_pct
        w_blocked = sum(r['pnl'] for r in winners if r.get(key, False))
        l_saved = -sum(r['pnl'] for r in losers if r.get(key, False))
        net = -w_blocked + l_saved
        marker = ' ★' if edge >= 20 and l_fire >= 3 else ''
        print(f"   {label_p:<48} {w_fire:>2}/{len(winners):<2} ({w_pct:>3.0f}%) "
              f"{l_fire:>2}/{len(losers):<2} ({l_pct:>3.0f}%) "
              f"{edge:>+5.0f}% {w_blocked:>+8.0f} {l_saved:>+8.0f} {net:>+8.0f}{marker}")
        out_rows.append({'key': key, 'edge': edge, 'net': net,
                         'w_fire': w_fire, 'l_fire': l_fire})
    return out_rows


def main():
    print("=" * 100)
    print("SPLIT VALIDATION — does each pattern hold up out-of-sample?")
    print("=" * 100)

    print("\nLoading broker history...")
    if not mt5.initialize(path=MT5_PATH, login=MT5_LOGIN,
                          password=MT5_PASSWORD, server=MT5_SERVER):
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    trades = fetch_trades_with_pnl()
    print(f"Total closed: {len(trades)}")

    print("\nBuilding feature rows (this fetches OHLCV per trade)...")
    rows = build_rows(trades)
    rows.sort(key=lambda r: r['entry_ts'])
    mt5.shutdown()
    print(f"Rows built: {len(rows)}")

    # Split chronologically in half
    half = len(rows) // 2
    train = rows[:half]
    test = rows[half:]

    patterns = [
        ('forming_against',           'Entry H4 bar forming AGAINST trade'),
        ('stacked_opposing_fvg',      'Stacked opposing HTF FVG <0.5%'),
        ('ipda_fade_extreme',         'Trade fades IPDA 20d extreme'),
        ('vs_d1_disagree',            'Trade disagrees with D1 bias'),
        ('strong_opposing_d1_trend',  'D1 trend strongly opposes trade'),
        ('strong_opposing_h4_trend',  'H4 trend strongly opposes trade'),
        ('vs_h1_disagree',            'Trade disagrees with H1 bias (anti-pattern)'),
    ]

    train_results = report_half("TRAIN HALF (older 50%)", train, patterns)
    test_results = report_half("TEST HALF  (newer 50%)", test, patterns)

    # Stability assessment
    print("\n" + "=" * 100)
    print("STABILITY — does the edge survive on the test half?")
    print("=" * 100)
    print(f"\n{'pattern':<48} {'train_edge':>11} {'test_edge':>11} "
          f"{'train_NET':>10} {'test_NET':>10} {'verdict':<14}")
    print("-" * 110)
    train_by_key = {r['key']: r for r in train_results}
    for r_test in test_results:
        key = r_test['key']
        r_train = train_by_key.get(key, {})
        edge_t = r_train.get('edge', 0)
        edge_te = r_test['edge']
        net_t = r_train.get('net', 0)
        net_te = r_test['net']

        # Verdict logic
        if edge_t >= 20 and edge_te >= 20 and net_t > 0 and net_te > 0:
            verdict = '✓ ROBUST'
        elif edge_t >= 20 and edge_te < 10:
            verdict = '✗ OVERFIT'
        elif edge_t < 0 and edge_te < 0:
            verdict = '✗ ANTI-EDGE'
        elif edge_te > 0 and net_te > 0:
            verdict = '~ WEAK+'
        else:
            verdict = '~ NOISE'

        label_p = next((lab for k, lab in patterns if k == key), key)
        print(f"{label_p:<48} {edge_t:>+10.0f}% {edge_te:>+10.0f}% "
              f"{net_t:>+10.0f} {net_te:>+10.0f}  {verdict}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
