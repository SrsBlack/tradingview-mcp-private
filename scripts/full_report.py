"""Full bridge trade report — live + paper."""
import MetaTrader5 as mt5
from datetime import datetime, timedelta
import json, glob
from pathlib import Path

mt5.initialize()

from_date = datetime.now() - timedelta(days=7)
to_date = datetime.now() + timedelta(days=1)
all_deals = mt5.history_deals_get(from_date, to_date)

# Find all bridge magic numbers (not EA 700xxx, not trading-ai-v2 11xxx)
bridge_magics = set()
for d in all_deals:
    if d.magic == 0:
        continue
    if 700000 <= d.magic <= 799999:
        continue
    if 11000 <= d.magic <= 11999:
        continue
    bridge_magics.add(d.magic)

print(f"Bridge magic numbers detected: {sorted(bridge_magics)}")

# Build position history
bridge_deals = [d for d in all_deals if d.magic in bridge_magics]
positions = {}
for d in bridge_deals:
    positions.setdefault(d.position_id, []).append(d)

open_pos = mt5.positions_get()
open_tickets = {p.ticket for p in open_pos}

print()
print("=" * 80)
print("  BRIDGE SYSTEM — COMPLETE LIVE TRADE HISTORY (MT5 VERIFIED)")
print("=" * 80)
print()

total_pnl = 0
wins = 0
losses = 0
open_count = 0
open_floating = 0
trade_num = 0

for pos_id in sorted(positions.keys()):
    dd = positions[pos_id]
    entry = [d for d in dd if d.entry == 0]
    exits = [d for d in dd if d.entry == 1]

    if not entry:
        continue

    trade_num += 1
    e = entry[0]
    direction = "BUY" if e.type == 0 else "SELL"
    entry_time = datetime.fromtimestamp(e.time)

    if exits:
        x = exits[0]
        exit_time = datetime.fromtimestamp(x.time)
        pnl = x.profit
        total_pnl += pnl
        hours = (exit_time - entry_time).total_seconds() / 3600
        tag = "WIN" if pnl > 0 else "LOSS"
        if pnl > 0:
            wins += 1
        else:
            losses += 1

        print(f"  {trade_num}. {e.symbol} {direction} | {tag} | Magic: {e.magic}")
        print(f"     Entry: {e.price} -> Exit: {x.price} | {e.volume} lots")
        print(f"     P&L: ${pnl:,.2f} | Duration: {hours:.1f}h")
        print(f"     {entry_time.strftime('%b %d %H:%M')} -> {exit_time.strftime('%b %d %H:%M')}")
        print()
    elif pos_id in open_tickets:
        p = [pp for pp in open_pos if pp.ticket == pos_id][0]
        open_count += 1
        open_floating += p.profit
        print(f"  {trade_num}. {e.symbol} {direction} | OPEN | Magic: {e.magic}")
        print(f"     Entry: {e.price} | Current: {p.price_current} | {e.volume} lots")
        print(f"     Floating P&L: ${p.profit:,.2f}")
        print(f"     Opened: {entry_time.strftime('%b %d %H:%M')}")
        print()
    else:
        print(f"  {trade_num}. {e.symbol} {direction} | CLOSED (exit deal diff magic)")
        print(f"     Entry: {e.price} | {e.volume} lots | {entry_time.strftime('%b %d %H:%M')}")
        print()

closed_total = wins + losses
print("=" * 80)
print("  LIVE SUMMARY")
if closed_total:
    print(f"  Closed: {closed_total} trades | {wins}W / {losses}L | WR: {wins/closed_total*100:.0f}%")
    print(f"  Realized P&L: ${total_pnl:,.2f}")
    if wins:
        w_pnl = sum(d.profit for dd in positions.values() for d in dd if d.entry == 1 and d.profit > 0)
        print(f"  Gross Wins: ${w_pnl:,.2f} | Avg Win: ${w_pnl/wins:,.2f}")
    if losses:
        l_pnl = sum(d.profit for dd in positions.values() for d in dd if d.entry == 1 and d.profit <= 0)
        print(f"  Gross Losses: ${l_pnl:,.2f} | Avg Loss: ${l_pnl/losses:,.2f}")
    if wins and losses:
        w_pnl = sum(d.profit for dd in positions.values() for d in dd if d.entry == 1 and d.profit > 0)
        l_pnl = abs(sum(d.profit for dd in positions.values() for d in dd if d.entry == 1 and d.profit <= 0))
        if l_pnl > 0:
            print(f"  Profit Factor: {w_pnl/l_pnl:.2f}")
if open_count:
    print(f"  Open: {open_count} positions | Floating: ${open_floating:,.2f}")
print(f"  Net (realized + floating): ${total_pnl + open_floating:,.2f}")
print("=" * 80)

# ================================================================
# PAPER SHADOW
# ================================================================
print()
print("=" * 80)
print("  PAPER SHADOW — COMPLETE TRADE HISTORY")
print("=" * 80)
print()

all_paper = []
for f in sorted(glob.glob("logs/paper_trades_*.jsonl")):
    with open(f) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    all_paper.append(json.loads(line))
                except Exception:
                    pass

all_paper.sort(key=lambda x: x.get("timestamp", ""))
opens = [t for t in all_paper if t.get("event") == "OPEN"]
closes = [t for t in all_paper if t.get("event") == "CLOSE"]

# Match opens to closes
trade_pairs = []
used_open_ids = set()
for c in closes:
    for o in reversed(opens):
        if (
            o.get("ticket") == c.get("ticket")
            and o.get("symbol") == c.get("symbol")
            and o.get("timestamp", "") < c.get("timestamp", "")
            and id(o) not in used_open_ids
        ):
            trade_pairs.append((o, c))
            used_open_ids.add(id(o))
            break

paper_num = 0
paper_pnl = 0
paper_wins = 0
paper_losses = 0

for o, c in sorted(trade_pairs, key=lambda x: x[0].get("timestamp", "")):
    paper_num += 1
    pnl = c.get("pnl", 0)
    paper_pnl += pnl
    r = c.get("r_multiple", 0)
    tag = "WIN" if pnl > 0 else "LOSS"
    if pnl > 0:
        paper_wins += 1
    else:
        paper_losses += 1

    grade = o.get("ict_grade", "?")
    score = o.get("ict_score", 0)
    reason = c.get("reason", "?")

    print(f"  {paper_num}. {o.get('symbol')} {o.get('direction')} | {tag} | Grade {grade} ({score:.0f})")
    print(f"     Entry: {o.get('entry_price')} -> Exit: {c.get('exit', c.get('exit_price', '?'))}")
    print(f"     P&L: ${pnl:,.2f} ({r:+.1f}R) | {reason}")
    print(f"     {o.get('timestamp', '')[:16]} -> {c.get('timestamp', '')[:16]}")
    print()

# Still open paper
paired_ids = set(id(p[0]) for p in trade_pairs)
unpaired = [o for o in opens if id(o) not in paired_ids and o.get("ict_score", 0) > 0]

paper_total = paper_wins + paper_losses
print("=" * 80)
print("  PAPER SUMMARY")
if paper_total:
    print(f"  Closed: {paper_total} trades | {paper_wins}W / {paper_losses}L | WR: {paper_wins/paper_total*100:.0f}%")
    print(f"  Net P&L: ${paper_pnl:,.2f}")
    if paper_wins:
        w = sum(c.get("pnl", 0) for _, c in trade_pairs if c.get("pnl", 0) > 0)
        print(f"  Gross Wins: ${w:,.2f} | Avg Win: ${w/paper_wins:,.2f}")
    if paper_losses:
        l = sum(c.get("pnl", 0) for _, c in trade_pairs if c.get("pnl", 0) <= 0)
        print(f"  Gross Losses: ${l:,.2f} | Avg Loss: ${l/paper_losses:,.2f}")
    if paper_wins and paper_losses:
        w = sum(c.get("pnl", 0) for _, c in trade_pairs if c.get("pnl", 0) > 0)
        l = abs(sum(c.get("pnl", 0) for _, c in trade_pairs if c.get("pnl", 0) <= 0))
        if l > 0:
            print(f"  Profit Factor: {w/l:.2f}")

if unpaired:
    print(f"  Open: {len(unpaired)} positions")
    for o in unpaired:
        print(f"    {o.get('symbol')} {o.get('direction')} @ {o.get('entry_price')} | Grade {o.get('ict_grade')} ({o.get('ict_score', 0):.0f})")

ps = json.loads(
    (Path.home() / ".tradingview-mcp" / "paper_shadow_state.json").read_text(encoding="utf-8")
)
print(f"  State Balance: ${ps['balance']:,.2f}")
print("=" * 80)

# ================================================================
# ACCOUNT OVERVIEW
# ================================================================
print()
info = mt5.account_info()
print("=" * 80)
print("  ACCOUNT OVERVIEW")
print(f"  Balance: ${info.balance:,.2f} | Equity: ${info.equity:,.2f}")
print(f"  Open Positions: {len(open_pos)} (all engines)")
print(f"  Unrealized P&L (all): ${info.equity - info.balance:,.2f}")
print(f"  Bridge Live: ${total_pnl:,.2f} realized + ${open_floating:,.2f} floating = ${total_pnl + open_floating:,.2f} net")
print(f"  Bridge Paper: ${paper_pnl:,.2f} realized")
print("=" * 80)

mt5.shutdown()
