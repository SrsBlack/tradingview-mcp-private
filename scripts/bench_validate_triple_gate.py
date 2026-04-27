"""Validate the top compound gate candidates with split-half + per-trade detail.

Tests three candidates:
  G1: forming_h4_against AND is_sell                          (single-feature pair)
  G2: forming_h4_against AND is_sell AND stacked_opposing_fvg (clean triple)
  G3: is_jpy_pair AND is_sell                                  (broadest sell veto)

For each:
  - List the exact trades it would have blocked (winners + losers)
  - Split-validation: edge on train half + test half
  - Winners-bench compatibility check

Usage:
  PYTHONUTF8=1 python scripts/bench_validate_triple_gate.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import pandas as pd
import MetaTrader5 as mt5

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(1, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from scripts.bench_compound_gate_explorer import (
    fetch_at, compute_features, fetch_trades_with_pnl,
    MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH,
)

GATES = [
    ("G1_forming_against_AND_sell",
     lambda r: r.get('forming_h4_against') and r.get('is_sell')),
    ("G2_forming_against_AND_sell_AND_stacked_fvg",
     lambda r: r.get('forming_h4_against') and r.get('is_sell') and r.get('stacked_opposing_fvg')),
    ("G3_jpy_AND_sell",
     lambda r: r.get('is_jpy_pair') and r.get('is_sell')),
    ("G4_jpy_AND_sell_AND_stacked_fvg",
     lambda r: r.get('is_jpy_pair') and r.get('is_sell') and r.get('stacked_opposing_fvg')),
]


def main():
    print("=" * 110)
    print("Compound-gate split validation + per-trade detail")
    print("=" * 110)

    if not mt5.initialize(path=MT5_PATH, login=MT5_LOGIN,
                          password=MT5_PASSWORD, server=MT5_SERVER):
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    trades = fetch_trades_with_pnl()

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
            'is_winner': t['pnl_usd'] > 0, **feats,
        })
    mt5.shutdown()

    rows.sort(key=lambda r: r['entry_ts'])
    half = len(rows) // 2
    train = rows[:half]
    test = rows[half:]

    for gate_name, gate_pred in GATES:
        print(f"\n{'=' * 110}")
        print(f"  {gate_name}")
        print('=' * 110)

        # Detail: every trade the gate would block
        print(f"\n  Trades blocked by gate (full dataset):")
        blocked = [r for r in rows if gate_pred(r)]
        if not blocked:
            print("    (no trades blocked)")
        for r in blocked:
            tag = '✓ WIN' if r['is_winner'] else '✗ LOSS'
            print(f"    {tag} {r['entry_ts'].strftime('%m-%d %H:%M')}  "
                  f"{r['sym']:<14} {r['side']:<5} pnl=${r['pnl']:+8.2f}")

        # Split validation
        for label, half_rows in (("TRAIN", train), ("TEST", test)):
            wnn = [r for r in half_rows if r['is_winner']]
            lnn = [r for r in half_rows if not r['is_winner']]
            wn = sum(1 for r in wnn if gate_pred(r))
            ln = sum(1 for r in lnn if gate_pred(r))
            wd = sum(r['pnl'] for r in wnn if gate_pred(r))
            ld = -sum(r['pnl'] for r in lnn if gate_pred(r))
            net = -wd + ld
            n_w = len(wnn) or 1
            n_l = len(lnn) or 1
            wpct = wn / n_w * 100
            lpct = ln / n_l * 100
            edge = lpct - wpct
            print(f"  {label:<5}:  winners {wn}/{len(wnn)} ({wpct:.0f}%) ${wd:+.0f}  "
                  f"losers {ln}/{len(lnn)} ({lpct:.0f}%) ${ld:+.0f}  "
                  f"edge {edge:+.0f}%  net ${net:+.0f}")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
