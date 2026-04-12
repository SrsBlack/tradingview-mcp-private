"""
Trace each trade from entry to outcome using actual session price data.
Shows what the market did AFTER each trade was placed.
"""

import json
import os
from datetime import datetime, timezone
from bridge.config import price_in_range

# Load session price data
path = os.path.expanduser("~/.tradingview-mcp/sessions/2026-04-09.json")
with open(path, encoding="utf-8") as f:
    session = json.load(f)

# Build clean price timeline per symbol (filter contaminated readings)
price_timeline: dict[str, list[tuple[str, float]]] = {}
for d in session["decisions"]:
    sym = d.get("symbol", "")
    ts = d.get("timestamp", "")
    price = d.get("entry_price", 0)
    if not (sym and price > 0 and ts):
        continue
    base = sym.split(":")[-1]
    if not price_in_range(sym, price):
        continue  # skip contaminated readings
    if base not in price_timeline:
        price_timeline[base] = []
    price_timeline[base].append((ts, price))

# Sort each timeline
for sym in price_timeline:
    price_timeline[sym].sort(key=lambda x: x[0])


# All valid trades with their actual entry timestamps from the raw logs
# (id, symbol_base, direction, entry_price, sl, tp1, tp2, entry_timestamp, session_file)
trades = [
    ("T01", "BTCUSD", "BUY",  69000.00, 68500.00, 70000.00, None,     "2026-04-08T05:02:46", "20260408"),
    ("T02", "BTCUSD", "BUY",  80000.00, 79200.00, 81200.00, 82400.0,  "2026-04-09T06:21:26", "20260409"),
    ("T03", "EURUSD", "SELL",  1.1700,   1.1725,   1.1625,  1.1536,   "2026-04-09T08:05:47", "20260409"),
    ("T04", "BTCUSD", "BUY",  70923.00, 70680.00, 71850.00, 72950.0,  "2026-04-09T08:12:32", "20260409"),
    ("T06", "EURUSD", "BUY",   1.1700,   1.1680,   1.1750,  1.1813,   "2026-04-09T08:06:31", "20260409"),
    ("T07", "BTCUSD", "BUY",  70963.00, 70450.00, 72200.00, 73600.0,  "2026-04-09T08:20:29", "20260409"),
    ("T08", "EURUSD", "SELL",  1.1700,   1.1750,   1.1620,  1.1490,   "2026-04-09T08:25:49", "20260409"),
    ("T09", "SOLUSD", "BUY",   82.28,    81.15,    84.92,   87.56,    "2026-04-09T08:23:40", "20260409"),
    ("T10", "UKOIL",  "SELL",  98.29,    98.95,    97.15,   95.58,    "2026-04-09T08:30:28", "20260409"),
    ("T11", "SOLUSD", "BUY",   82.49,    81.24,    84.99,   87.49,    "2026-04-09T08:42:36", "20260409"),
    ("T12", "EURUSD", "SELL",  1.1700,   1.1750,   1.1620,  1.1490,   "2026-04-09T08:44:25", "20260409"),
    ("T13", "XAUUSD", "BUY", 4731.93,  4720.15,  4748.60, 4762.85,   "2026-04-09T08:47:43", "20260409"),
    ("T14", "ETHUSD", "BUY", 2180.96,  2168.50,  2198.75, 2227.44,   "2026-04-09T09:10:03", "20260409"),
]

# Contaminated trades (excluded)
contaminated_ids = {"T05"}  # SOL @ $98 was OIL price

# Current live prices (verified via Alpaca + TV)
LIVE = {
    "BTCUSD": 71799.0,
    "ETHUSD": 2191.0,
    "SOLUSD": 83.30,
    "EURUSD": 1.1691,
    "XAUUSD": 4763.0,
    "UKOIL": 96.40,
}

W = 120
print()
print("=" * W)
print("  TRADE-BY-TRADE TIMELINE ANALYSIS")
print("  Tracing actual price path after each entry using session price log")
print("=" * W)

for t in trades:
    tid, sym, dr, entry, sl, tp1, tp2, entry_ts, sess = t
    if tid in contaminated_ids:
        continue

    print()
    print(f"  --- {tid}: {sym} {dr} @ {entry:.4f} ---")
    print(f"  Entered: {entry_ts[:19]} UTC")
    print(f"  SL: {sl:.4f}  |  TP1: {tp1:.4f}  |  TP2: {tp2:.4f}" if tp2 else
          f"  SL: {sl:.4f}  |  TP1: {tp1:.4f}")
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp1 - entry)
    rr = tp_dist / sl_dist if sl_dist > 0 else 0
    print(f"  SL dist: {sl_dist:.4f}  |  TP dist: {tp_dist:.4f}  |  R:R = {rr:.1f}")
    print()

    # Get price path after entry
    timeline = price_timeline.get(sym, [])
    after_entry = [(ts, p) for ts, p in timeline if ts >= entry_ts[:19]]

    if not after_entry:
        print(f"  No price data found after entry time")
        print()
        continue

    # Trace the path
    sl_hit_at = None
    tp1_hit_at = None
    tp2_hit_at = None
    min_price = entry
    max_price = entry
    min_ts = entry_ts
    max_ts = entry_ts

    print(f"  {'Time':19}  {'Price':>12}  {'vs Entry':>10}  {'SL dist':>10}  {'TP1 dist':>10}  Event")
    print(f"  {'-'*19}  {'-'*12}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*20}")

    for ts, price in after_entry:
        vs_entry = price - entry if dr == "BUY" else entry - price
        to_sl = abs(price - sl)
        to_tp = abs(price - tp1)

        if price < min_price:
            min_price = price
            min_ts = ts
        if price > max_price:
            max_price = price
            max_ts = ts

        event = ""
        if dr == "BUY":
            if price <= sl and not sl_hit_at:
                sl_hit_at = ts
                event = "<<< SL HIT"
            elif price >= tp1 and not tp1_hit_at:
                tp1_hit_at = ts
                event = ">>> TP1 HIT"
            elif tp2 and price >= tp2 and not tp2_hit_at:
                tp2_hit_at = ts
                event = ">>> TP2 HIT"
        else:
            if price >= sl and not sl_hit_at:
                sl_hit_at = ts
                event = "<<< SL HIT"
            elif price <= tp1 and not tp1_hit_at:
                tp1_hit_at = ts
                event = ">>> TP1 HIT"
            elif tp2 and price <= tp2 and not tp2_hit_at:
                tp2_hit_at = ts
                event = ">>> TP2 HIT"

        vs_s = f"+{vs_entry:.4f}" if vs_entry >= 0 else f"{vs_entry:.4f}"
        print(f"  {ts[:19]}  {price:>12.4f}  {vs_s:>10}  {to_sl:>10.4f}  {to_tp:>10.4f}  {event}")

    # Summary for this trade
    print()
    cur = LIVE.get(sym, 0)
    cur_vs = cur - entry if dr == "BUY" else entry - cur

    if sl_hit_at and (not tp1_hit_at or sl_hit_at < tp1_hit_at):
        outcome = "SL HIT FIRST"
        outcome_ts = sl_hit_at
    elif tp1_hit_at and (not sl_hit_at or tp1_hit_at < sl_hit_at):
        if tp2_hit_at:
            outcome = "TP1 then TP2 HIT"
        else:
            outcome = "TP1 HIT"
        outcome_ts = tp1_hit_at
    else:
        outcome = "STILL OPEN (neither SL nor TP hit in data)"
        outcome_ts = None

    print(f"  OUTCOME: {outcome}" + (f" at {outcome_ts[:19]}" if outcome_ts else ""))
    print(f"  Price range after entry: {min_price:.4f} (low) to {max_price:.4f} (high)")
    print(f"  Current price: {cur:.4f}  (vs entry: {'+' if cur_vs >= 0 else ''}{cur_vs:.4f})")

    # Was direction correct?
    dir_correct = cur_vs > 0
    print(f"  Direction call: {'CORRECT' if dir_correct else 'WRONG'} (price {'above' if cur > entry else 'below'} entry now)")

    if sl_hit_at and tp1_hit_at and sl_hit_at < tp1_hit_at:
        print(f"  NOTE: SL was hit BEFORE TP1. Price later reached TP1 — SL was too tight!")

print()
print("=" * W)
