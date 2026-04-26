"""Backtest variant: D1 ALONE as invalidator. Same harness as MTF version."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

sys.path.insert(0, r"C:\Users\User\tradingview-mcp-jackson")
sys.stdout.reconfigure(encoding="utf-8")

from bridge.config import ensure_trading_ai_path, tv_to_ftmo_symbol
ensure_trading_ai_path()
from analysis.structure import detect_swings, classify_structure, get_current_bias

CHECK_INTERVAL_MIN = 15
HTF_AGE_SKIP_HOURS = 2

mt5.initialize()


def get_bias_at(sym, end_dt, tf_name):
    cfg = {"H4":(mt5.TIMEFRAME_H4,100,5),"D1":(mt5.TIMEFRAME_D1,60,3),"W1":(mt5.TIMEFRAME_W1,30,2)}
    tf, n, lb = cfg[tf_name]
    rates = mt5.copy_rates_from(sym, tf, end_dt, n)
    if rates is None or len(rates) < 15: return "NEUTRAL"
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True); df = df.set_index("time")
    df = df.rename(columns={"tick_volume":"volume"})
    if len(df) > 15: df = df.iloc[:-1]
    sw = detect_swings(df, lookback=lb)
    _, ev = classify_structure(sw, df=df)
    return get_current_bias(ev).name


def get_price_at(sym, dt):
    rates = mt5.copy_rates_from(sym, mt5.TIMEFRAME_M5, dt, 1)
    if rates is None or len(rates) == 0: return None
    return float(rates[0]["close"])


def parse_iso(s):
    if not s: return None
    s = s.replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
    return d


session_dir = Path.home() / ".tradingview-mcp" / "sessions"
all_trades = []
for d in ["2026-04-19","2026-04-20","2026-04-21","2026-04-22","2026-04-23","2026-04-24","2026-04-25"]:
    p = session_dir / f"{d}.json"
    if p.exists():
        with open(p, encoding="utf-8") as f: all_trades.extend(json.load(f).get("trades", []))

opens = {t["ticket"]: t for t in all_trades if t.get("event")=="OPEN" and t.get("mode")!="paper_shadow"}
closes = [t for t in all_trades if t.get("event")=="CLOSE" and t.get("reason")!="TP (while offline)"]

pairs = [(opens[c["ticket"]], c) for c in closes if c["ticket"] in opens]
print(f"Loaded {len(pairs)} closed trades — testing D1-ALONE invalidation\n")

total_saved = 0.0
total_cost = 0.0
rows = []

for o, c in pairs:
    sym_base = o["symbol"].split(":")[-1]
    ftmo_sym = tv_to_ftmo_symbol(sym_base)
    direction = o["direction"]
    if direction not in ("BUY", "SELL"): continue

    open_t = parse_iso(o.get("opened_at") or o["timestamp"])
    close_t = parse_iso(c["timestamp"])
    if not open_t or not close_t: continue
    actual_pnl = c.get("pnl", 0)
    actual_exit = c.get("exit_price", 0)
    entry = o.get("entry_price", 0)
    sl = o.get("sl_price", 0)
    sl_dist = abs(entry - sl)
    actual_outcome = "WIN" if actual_pnl > 0 else ("LOSS" if actual_pnl < 0 else "BE")

    check_start = open_t + timedelta(hours=HTF_AGE_SKIP_HOURS)
    if check_start >= close_t:
        rows.append({"sym":sym_base,"dir":direction,"out":actual_outcome,"pnl":actual_pnl,"fired":False,"verdict":f"NO_INTERFERENCE ({(close_t-open_t).total_seconds()/3600:.1f}h)"})
        continue

    cur = check_start
    fired = None
    while cur < close_t:
        cp = get_price_at(ftmo_sym, cur)
        if cp is None:
            cur += timedelta(minutes=CHECK_INTERVAL_MIN); continue
        unreal = (cp - entry) if direction == "BUY" else (entry - cp)
        r = unreal / sl_dist if sl_dist > 0 else 0
        if r >= 0:
            cur += timedelta(minutes=CHECK_INTERVAL_MIN); continue
        d1 = get_bias_at(ftmo_sym, cur, "D1")
        opp = (direction == "BUY" and d1 == "BEARISH") or (direction == "SELL" and d1 == "BULLISH")
        if opp:
            fired = {"t": cur, "price": cp, "d1": d1, "r": r}; break
        cur += timedelta(minutes=CHECK_INTERVAL_MIN)

    if not fired:
        rows.append({"sym":sym_base,"dir":direction,"out":actual_outcome,"pnl":actual_pnl,"fired":False,"verdict":"NO_FIRE"})
        continue

    actual_unrealized = (actual_exit - entry) if direction == "BUY" else (entry - actual_exit)
    mtf_unrealized = (fired["price"] - entry) if direction == "BUY" else (entry - fired["price"])
    hyp = actual_pnl * (mtf_unrealized / actual_unrealized) if actual_unrealized != 0 else 0

    if actual_outcome == "LOSS":
        saved = abs(actual_pnl) - abs(hyp)
        if saved > 0:
            verdict = f"SAVED ${saved:+.2f} (hyp=${hyp:+.2f})"; total_saved += saved
        elif saved < 0:
            verdict = f"WORSE ${saved:+.2f} (hyp=${hyp:+.2f})"; total_cost += -saved
        else:
            verdict = f"NO_HELP (hyp=${hyp:+.2f})"
    elif actual_outcome == "WIN":
        cost = actual_pnl - hyp
        verdict = f"KILLED_WINNER cost=${cost:+.2f} (hyp=${hyp:+.2f})"; total_cost += cost
    else:
        verdict = f"BE hyp=${hyp:+.2f}"
    rows.append({"sym":sym_base,"dir":direction,"out":actual_outcome,"pnl":actual_pnl,"fired":True,"d1":fired["d1"],"verdict":verdict})

print(f"{'Sym':<10} {'Dir':<5} {'Out':<5} {'Actual':>10} {'Fired':<6} {'Verdict'}")
print("-" * 100)
for r in rows:
    print(f"{r['sym']:<10} {r['dir']:<5} {r['out']:<5} ${r['pnl']:>+8.2f} {'YES' if r['fired'] else 'no':<6} {r['verdict']}")

fired_n = sum(1 for r in rows if r["fired"])
saved_losers = sum(1 for r in rows if r["fired"] and r["out"] == "LOSS")
killed_winners = sum(1 for r in rows if r["fired"] and r["out"] == "WIN")
print(f"\n=== D1-ONLY SUMMARY ===")
print(f"MTF fired: {fired_n}/{len(rows)}")
print(f"Losers caught: {saved_losers}/{sum(1 for r in rows if r['out']=='LOSS')}")
print(f"Winners hit: {killed_winners}/{sum(1 for r in rows if r['out']=='WIN')}")
print(f"Money saved: ${total_saved:+.2f}")
print(f"Money cost: ${total_cost:+.2f}")
print(f"Net deployment value: ${total_saved - total_cost:+.2f}")
mt5.shutdown()
