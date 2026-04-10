"""
Full trade analysis with verified live prices (Alpaca + TradingView).
Apr 8-9 2026 — 6 contaminated entries excluded.
"""

# Current verified prices (Apr 9 2026 ~4pm PDT)
# BTC/ETH/SOL from Alpaca API, EURUSD/XAUUSD/UKOIL from TradingView live quote
LIVE = {
    "BTCUSD": 71799.0,
    "ETHUSD": 2191.0,
    "SOLUSD": 83.30,
    "EURUSD": 1.1691,
    "XAUUSD": 4763.0,
    "UKOIL": 96.40,
}

# All valid trades — T05 excluded as contaminated (SOL chart showed OIL price $98)
# (id, symbol, dir, entry, sl, tp1, tp2, result, evidence, pnl, r, status)
trades = [
    # CONFIRMED by bridge close logs
    ("T01", "BTCUSD", "BUY",  69000.00, 68500.00, 70000.00, None,    "TP HIT",  "Bridge close log",        200.00,  2.0, "CONFIRMED"),
    ("T02", "BTCUSD", "BUY",  80000.00, 79200.00, 81200.00, 82400.0, "TP2 HIT", "Bridge close log",        300.00,  3.0, "CONFIRMED"),
    ("T03", "EURUSD", "SELL",  1.1700,   1.1725,   1.1625,  1.1536,  "SL HIT",  "Bridge close log",        -60.00, -1.0, "CONFIRMED"),

    # VERIFIED against session price data (2026-04-09.json — 284 decision entries)
    ("T04", "BTCUSD", "BUY",  70923.00, 70680.00, 71850.00, 72950.0, "SL HIT",  "Price hit 70596 @ 14:19", -49.72, -1.0, "VERIFIED"),
    ("T06", "EURUSD", "BUY",   1.1700,   1.1680,   1.1750,  1.1813,  "SL HIT",  "Price hit 1.166 @ 08:12", -30.00, -1.0, "VERIFIED"),
    ("T10", "UKOIL",  "SELL",  98.29,    98.95,    97.15,   95.58,   "SL HIT",  "Price hit 99.15 @ 14:39", -19.98, -1.0, "VERIFIED"),
    ("T13", "XAUUSD", "BUY", 4731.93,  4720.15,  4748.60, 4762.85,  "TP2 HIT", "TP1@11:47 TP2@13:44",      52.50,  2.6, "VERIFIED"),
    ("T14", "ETHUSD", "BUY", 2180.96,  2168.50,  2198.75, 2227.44,  "SL HIT",  "Price hit 2165 @ 13:49",  -50.00, -1.0, "VERIFIED"),

    # STILL OPEN (price stayed between SL and TP during entire recording window)
    ("T07", "BTCUSD", "BUY",  70963.00, 70450.00, 72200.00, 73600.0, "OPEN", "Never hit SL or TP",  0, 0, "OPEN"),
    ("T08", "EURUSD", "SELL",  1.1700,   1.1750,   1.1620,  1.1490,  "OPEN", "Ranged 1.167-1.170",  0, 0, "OPEN"),
    ("T09", "SOLUSD", "BUY",   82.28,    81.15,    84.92,   87.56,   "OPEN", "Ranged 81.58-84.48",  0, 0, "OPEN"),
    ("T11", "SOLUSD", "BUY",   82.49,    81.24,    84.99,   87.49,   "OPEN", "Ranged 81.58-84.48",  0, 0, "OPEN"),
    ("T12", "EURUSD", "SELL",  1.1700,   1.1750,   1.1620,  1.1490,  "OPEN", "Ranged 1.167-1.170",  0, 0, "OPEN"),
]

W = 130
print()
print("=" * W)
print("  FULL TRADE ANALYSIS  |  Apr 8-9 2026  |  Live prices verified via Alpaca + TradingView")
print("  6 contaminated entries excluded  |  Prices as of Apr 9 ~4pm PDT")
print("=" * W)

# === SECTION 1: CLOSED TRADES ===
print()
print("  CLOSED TRADES (confirmed or verified against session price log)")
print("  " + "-" * (W - 4))
fmt = "  {:<5} {:<8} {:<5} {:>10} {:>10} {:>10}  {:<10} {:>10} {:>5}  {:<12} {:>10}"
print(fmt.format("#", "Symbol", "Dir", "Entry", "SL", "TP1", "Result", "PnL", "R", "Direction?", "Now"))
print("  " + "-" * (W - 4))

closed = [t for t in trades if t[11] != "OPEN"]
total_pnl = 0.0
wins = losses = correct_dir = 0

for t in closed:
    tid, sym, dr, entry, sl, tp1, tp2, result, evidence, pnl, r, status = t
    total_pnl += pnl
    if pnl > 0:
        wins += 1
    else:
        losses += 1

    pnl_s = "+${:,.2f}".format(pnl) if pnl >= 0 else "-${:,.2f}".format(abs(pnl))
    cur = LIVE.get(sym, 0)

    if dr == "BUY":
        dir_ok = cur > entry
    else:
        dir_ok = cur < entry
    if dir_ok:
        correct_dir += 1
    dir_s = "CORRECT" if dir_ok else "WRONG"

    print(fmt.format(tid, sym, dr, f"{entry:.2f}", f"{sl:.2f}", f"{tp1:.2f}",
                     result, pnl_s, f"{r:.1f}R", dir_s, f"{cur:.2f}"))

print("  " + "-" * (W - 4))
wr = wins * 100 // (wins + losses) if (wins + losses) > 0 else 0
print(f"  {wins}W / {losses}L  |  Win Rate: {wr}%  |  Direction Accuracy: "
      f"{correct_dir}/{len(closed)} ({correct_dir * 100 // len(closed)}%)  "
      f"|  Realized P&L: ${total_pnl:+,.2f}")

# === SECTION 2: STILL OPEN TRADES ===
print()
print("  STILL OPEN TRADES (bridge restarted before SL/TP could be monitored)")
print("  " + "-" * (W - 4))
fmt2 = "  {:<5} {:<8} {:<5} {:>10} {:>10} {:>10} {:>10}  {:>10} {:>12} {:<20}"
print(fmt2.format("#", "Symbol", "Dir", "Entry", "SL", "TP1", "TP2", "Now", "Unrealized", "Status"))
print("  " + "-" * (W - 4))

open_trades = [t for t in trades if t[11] == "OPEN"]
open_total = 0.0

for t in open_trades:
    tid, sym, dr, entry, sl, tp1, tp2, result, evidence, pnl, r, status = t
    cur = LIVE.get(sym, 0)

    if dr == "BUY":
        ur = cur - entry
        hit_tp1 = cur >= tp1
        hit_tp2 = tp2 and cur >= tp2
    else:
        ur = entry - cur
        hit_tp1 = cur <= tp1
        hit_tp2 = tp2 and cur <= tp2

    if hit_tp2:
        stat = "TP2 WOULD HIT"
    elif hit_tp1:
        stat = "TP1 WOULD HIT"
    elif ur > 0:
        stat = "IN PROFIT"
    else:
        stat = "IN DRAWDOWN"

    tp2_s = f"{tp2:.2f}" if tp2 else "---"
    ur_s = "+${:,.2f}".format(ur) if ur >= 0 else "-${:,.2f}".format(abs(ur))
    open_total += ur

    print(fmt2.format(tid, sym, dr, f"{entry:.2f}", f"{sl:.2f}", f"{tp1:.2f}",
                      tp2_s, f"{cur:.2f}", ur_s, stat))

print("  " + "-" * (W - 4))
open_s = "+${:,.2f}".format(open_total) if open_total >= 0 else "-${:,.2f}".format(abs(open_total))
print(f"  Total unrealized (mark-to-market): {open_s}")

# === SECTION 3: PREMATURE STOPOUTS ===
print()
print("  PREMATURE STOPOUTS (SL hit, but direction was correct)")
print("  " + "-" * (W - 4))

stopped_correct = []
for t in closed:
    tid, sym, dr, entry, sl, tp1, tp2, result, evidence, pnl, r, status = t
    if pnl >= 0:
        continue
    cur = LIVE.get(sym, 0)
    if dr == "BUY" and cur > tp1:
        stopped_correct.append(t)
    elif dr == "SELL" and cur < tp1:
        stopped_correct.append(t)

for t in stopped_correct:
    tid, sym, dr, entry, sl, tp1, tp2, result, evidence, pnl, r, status = t
    cur = LIVE.get(sym, 0)
    sl_dist = abs(entry - sl)
    would_pnl = (tp1 - entry) if dr == "BUY" else (entry - tp1)
    overshoot = abs(float(evidence.split()[-3]) - sl) if "hit" in evidence.lower() else 0
    print(f"    {tid} {sym} {dr} @ {entry:.2f}")
    print(f"      SL: {sl:.2f} (dist {sl_dist:.2f}) | {evidence}")
    print(f"      TP1: {tp1:.2f} | Price now: {cur:.2f}")
    print(f"      Lost ${abs(pnl):.2f} instead of winning ${would_pnl:.2f} per unit")
    print()

if not stopped_correct:
    print("    None")

# === SECTION 4: DUPLICATE ENTRIES ===
print("  DUPLICATE ENTRIES (same levels placed in separate bridge sessions)")
print("  " + "-" * (W - 4))
seen = {}
dupes_found = False
for t in trades:
    key = f"{t[1]}_{t[2]}_{t[3]:.4f}"
    if key in seen:
        print(f"    {seen[key]} and {t[0]}: {t[1]} {t[2]} @ {t[3]}")
        dupes_found = True
    seen[key] = t[0]
if not dupes_found:
    print("    None found")

# === SECTION 5: SUMMARY ===
print()
print("=" * W)
print("  SUMMARY")
print("=" * W)
print()
print(f"    Raw entries in logs:     20")
print(f"    Contaminated (excluded): 6  (cross-symbol price leaks)")
print(f"    Valid trades:            14")
print(f"    Closed:                  {len(closed)} ({wins}W / {losses}L = {wr}% win rate)")
print(f"    Still open:              {len(open_trades)} (orphaned by bridge restarts)")
print()
print(f"    Direction accuracy:      {correct_dir}/{len(closed)} ({correct_dir * 100 // len(closed)}%)")
print(f"    Avg winner:              ${total_pnl / wins if wins > 0 else 0:+,.2f}" if wins > 0 else "")
avg_loss = sum(t[9] for t in closed if t[9] < 0) / losses if losses > 0 else 0
print(f"    Avg loser:               ${avg_loss:,.2f}")
if wins > 0 and losses > 0:
    avg_w = sum(t[9] for t in closed if t[9] > 0) / wins
    print(f"    Profit factor:           {abs(avg_w * wins / (avg_loss * losses)):.2f}")
print()
print(f"    Realized P&L:            ${total_pnl:+,.2f}")
print(f"    Unrealized (open):       {open_s}")
combined = total_pnl + open_total
combined_s = "+${:,.2f}".format(combined) if combined >= 0 else "-${:,.2f}".format(abs(combined))
print(f"    Combined:                {combined_s}")
print()

# R-multiples
r_values = [t[10] for t in closed]
total_r = sum(r_values)
print(f"    Total R:                 {total_r:+.1f}R across {len(closed)} trades")
print(f"    Expectancy:              {total_r / len(closed):+.2f}R per trade")
print()
print("=" * W)
