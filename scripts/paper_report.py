"""Full paper shadow trade report with analysis."""
import json
import glob
from pathlib import Path
from collections import defaultdict
from datetime import datetime


def main():
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

    trade_pairs.sort(key=lambda x: x[0].get("timestamp", ""))

    paired_ids = set(id(p[0]) for p in trade_pairs)
    still_open = [o for o in opens if id(o) not in paired_ids and o.get("ict_score", 0) > 0]

    print("=" * 90)
    print("  PAPER SHADOW - EVERY TRADE WITH FULL DETAILS")
    print("=" * 90)

    num = 0
    total_pnl = 0
    w = 0
    l = 0
    by_grade = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    by_symbol = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    by_reason = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    r_multiples = []

    for o, c in trade_pairs:
        num += 1
        pnl = c.get("pnl", 0)
        total_pnl += pnl
        r = c.get("r_multiple", 0)
        r_multiples.append(r)
        tag = "WIN" if pnl > 0 else "LOSS"
        if pnl > 0:
            w += 1
        else:
            l += 1

        grade = o.get("ict_grade", "?")
        score = o.get("ict_score", 0)
        reason = c.get("reason", "?")
        sym = o.get("symbol", "?")
        direction = o.get("direction", "?")

        by_grade[grade]["pnl"] += pnl
        if pnl > 0:
            by_grade[grade]["w"] += 1
        else:
            by_grade[grade]["l"] += 1

        by_symbol[sym]["pnl"] += pnl
        if pnl > 0:
            by_symbol[sym]["w"] += 1
        else:
            by_symbol[sym]["l"] += 1

        by_reason[reason]["count"] += 1
        by_reason[reason]["pnl"] += pnl

        o_time = o.get("timestamp", "")[:16]
        c_time = c.get("timestamp", "")[:16]

        try:
            ot = datetime.fromisoformat(o.get("timestamp", "").replace("Z", "+00:00"))
            ct = datetime.fromisoformat(c.get("timestamp", "").replace("Z", "+00:00"))
            dur_h = (ct - ot).total_seconds() / 3600
            dur_str = f"{dur_h:.1f}h"
        except Exception:
            dur_str = "?"

        print(f"  {num}. {sym} {direction} | {tag} | Grade {grade} ({score:.0f}/100) | {dur_str}")
        print(f"     Entry: {o.get('entry_price')}  SL: {o.get('sl_price')}  TP: {o.get('tp_price')}  TP2: {o.get('tp2_price', 'N/A')}")
        exit_p = c.get("exit", c.get("exit_price", "?"))
        print(f"     Exit: {exit_p} | P&L: ${pnl:,.2f} ({r:+.1f}R) | {reason}")
        print(f"     Risk: {o.get('risk_pct', 0)*100:.2f}% | Lots: {o.get('lot_size', '?')} | Type: {o.get('trade_type', '?')}")
        print(f"     {o_time} -> {c_time}")
        if o.get("reasoning"):
            thesis = o["reasoning"][:200]
            if len(o["reasoning"]) > 200:
                thesis += "..."
            print(f"     Thesis: {thesis}")
        print()

    # Still open
    if still_open:
        print("-" * 90)
        print(f"  OPEN POSITIONS ({len(still_open)})")
        print("-" * 90)
        for o in still_open:
            score = o.get("ict_score", 0)
            print(f"  * {o.get('symbol')} {o.get('direction')} | Grade {o.get('ict_grade')} ({score:.0f}/100)")
            print(f"    Entry: {o.get('entry_price')}  SL: {o.get('sl_price')}  TP: {o.get('tp_price')}  TP2: {o.get('tp2_price', 'N/A')}")
            print(f"    Risk: {o.get('risk_pct', 0)*100:.2f}% | Lots: {o.get('lot_size', '?')} | Type: {o.get('trade_type', '?')}")
            print(f"    Opened: {o.get('timestamp', '')[:16]}")
            if o.get("reasoning"):
                thesis = o["reasoning"][:200]
                if len(o["reasoning"]) > 200:
                    thesis += "..."
                print(f"    Thesis: {thesis}")
            print()

    # ================================================================
    # ANALYSIS
    # ================================================================
    print("=" * 90)
    print("  ANALYSIS")
    print("=" * 90)

    total = w + l
    print()
    print(f"  Overall: {total} closed | {w}W / {l}L | WR: {w/total*100:.0f}%")
    print(f"  Net P&L: ${total_pnl:,.2f}")
    win_pnl = sum(c.get("pnl", 0) for _, c in trade_pairs if c.get("pnl", 0) > 0)
    loss_pnl = abs(sum(c.get("pnl", 0) for _, c in trade_pairs if c.get("pnl", 0) <= 0))
    if win_pnl and loss_pnl:
        print(f"  Profit Factor: {win_pnl/loss_pnl:.2f}")
    if w:
        print(f"  Avg Win: ${win_pnl/w:,.2f}")
    if l:
        print(f"  Avg Loss: ${-loss_pnl/l:,.2f}")
    if r_multiples:
        print(f"  Avg R: {sum(r_multiples)/len(r_multiples):+.2f}R")
        print(f"  Best R: {max(r_multiples):+.1f}R | Worst R: {min(r_multiples):+.1f}R")
        print(f"  Expectancy: ${total_pnl/total:,.2f} per trade")

    # By Grade
    print()
    print("  --- By Grade ---")
    for g in ["A", "B", "C", "D", "?"]:
        if g in by_grade:
            d = by_grade[g]
            gt = d["w"] + d["l"]
            wr = d["w"] / gt * 100 if gt else 0
            print(f"    Grade {g}: {gt} trades | {d['w']}W/{d['l']}L | WR: {wr:.0f}% | P&L: ${d['pnl']:,.2f}")

    # By Symbol
    print()
    print("  --- By Symbol ---")
    for sym, d in sorted(by_symbol.items(), key=lambda x: x[1]["pnl"], reverse=True):
        st = d["w"] + d["l"]
        wr = d["w"] / st * 100 if st else 0
        print(f"    {sym}: {st} trades | {d['w']}W/{d['l']}L | WR: {wr:.0f}% | P&L: ${d['pnl']:,.2f}")

    # By Exit Reason
    print()
    print("  --- By Exit Reason ---")
    for reason, d in sorted(by_reason.items(), key=lambda x: x[1]["pnl"], reverse=True):
        print(f"    {reason}: {d['count']} trades | P&L: ${d['pnl']:,.2f}")

    # Streaks
    print()
    print("  --- Streaks ---")
    max_win_streak = 0
    max_loss_streak = 0
    cur_win = 0
    cur_loss = 0
    for _, c in trade_pairs:
        if c.get("pnl", 0) > 0:
            cur_win += 1
            cur_loss = 0
            max_win_streak = max(max_win_streak, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            max_loss_streak = max(max_loss_streak, cur_loss)
    print(f"    Max Win Streak: {max_win_streak}")
    print(f"    Max Loss Streak: {max_loss_streak}")

    # Equity curve
    print()
    print("  --- Equity Curve ---")
    balance = 10000
    for i, (o, c) in enumerate(trade_pairs):
        pnl = c.get("pnl", 0)
        balance += pnl
        tag = "+" if pnl > 0 else "-"
        bar = "#" * max(1, int(abs(pnl) / 20))
        arrow = ">>>" if pnl > 0 else "<<<"
        print(f"    {i+1:2d}. {tag}${abs(pnl):>8.2f}  Balance: ${balance:>10,.2f}  {arrow} {bar}")

    # Key insights
    print()
    print("  --- Key Insights ---")
    ga = by_grade.get("A", {"w": 0, "l": 0, "pnl": 0})
    gb = by_grade.get("B", {"w": 0, "l": 0, "pnl": 0})
    gc = by_grade.get("C", {"w": 0, "l": 0, "pnl": 0})
    print(f"    1. Grade A: {ga['w']}W/{ga['l']}L | P&L: ${ga['pnl']:,.2f}")
    print(f"    2. Grade B: {gb['w']}W/{gb['l']}L | P&L: ${gb['pnl']:,.2f}")
    print(f"    3. Grade C: {gc['w']}W/{gc['l']}L | P&L: ${gc['pnl']:,.2f}")

    tp_pnl = by_reason.get("TP", {"pnl": 0})["pnl"] + by_reason.get("TP2", {"pnl": 0})["pnl"]
    trail_pnl = by_reason.get("TRAILING_SL", {"pnl": 0})["pnl"]
    sl_pnl = by_reason.get("SL", {"pnl": 0})["pnl"]
    sl_count = by_reason.get("SL", {"count": 0})["count"]

    print(f"    4. TP/TP2 exits: ${tp_pnl:,.2f} (clean target hits)")
    print(f"    5. Trailing SL exits: ${trail_pnl:,.2f} (managed profit locks)")
    print(f"    6. SL exits: ${sl_pnl:,.2f} ({sl_count}/{total} = {sl_count/total*100:.0f}% stopped out)")

    # R distribution
    if r_multiples:
        pos_r = [r for r in r_multiples if r > 0]
        neg_r = [r for r in r_multiples if r <= 0]
        print(f"    7. Positive R trades: {len(pos_r)} | Avg: {sum(pos_r)/len(pos_r):+.1f}R" if pos_r else "")
        print(f"    8. Negative R trades: {len(neg_r)} | Avg: {sum(neg_r)/len(neg_r):+.1f}R" if neg_r else "")

    # Biggest winner/loser
    if trade_pairs:
        best = max(trade_pairs, key=lambda x: x[1].get("pnl", 0))
        worst = min(trade_pairs, key=lambda x: x[1].get("pnl", 0))
        print(f"    9. Best trade: {best[0].get('symbol')} ${best[1].get('pnl',0):,.2f} (Grade {best[0].get('ict_grade')})")
        print(f"   10. Worst trade: {worst[0].get('symbol')} ${worst[1].get('pnl',0):,.2f} (Grade {worst[0].get('ict_grade')})")

    print()
    print("=" * 90)


if __name__ == "__main__":
    main()
