"""Verify the in-source gate methods match backtest predictions."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))

from bridge.claude_decision import ClaudeDecisionMaker

session_dir = Path.home() / ".tradingview-mcp" / "sessions"
all_decisions: list[dict] = []
all_trades: list[dict] = []
for d in ["2026-04-19", "2026-04-20", "2026-04-21", "2026-04-22", "2026-04-23", "2026-04-24"]:
    p = session_dir / f"{d}.json"
    if p.exists():
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        all_decisions.extend(s.get("decisions", []))
        all_trades.extend(s.get("trades", []))


def parse_ts(v):
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        return None


live_events = [t for t in all_trades if t.get("event") in ("OPEN", "CLOSE") and t.get("mode") != "paper_shadow"]
opens = {t.get("ticket"): t for t in live_events if t.get("event") == "OPEN"}
closes = [t for t in live_events if t.get("event") == "CLOSE" and t.get("reason") != "TP (while offline)"]

trades = []
for c in closes:
    o = opens.get(c.get("ticket"), {})
    sym = c.get("symbol") or o.get("symbol", "")
    opened_at = o.get("opened_at") or o.get("timestamp")
    if not opened_at:
        continue
    t_open = parse_ts(opened_at)
    if not t_open:
        continue
    best, best_dt = None, timedelta(hours=4)
    for d in all_decisions:
        if d.get("symbol") != sym or d.get("action") in ("SKIP", "HOLD", None):
            continue
        dt = parse_ts(d.get("timestamp", ""))
        if not dt:
            continue
        diff = t_open - dt
        if timedelta(0) <= diff < best_dt:
            best_dt = diff
            best = d
    if not best:
        continue
    trades.append({
        "symbol": sym,
        "side": c.get("direction"),
        "pnl": c.get("pnl", 0),
        "r": c.get("r_multiple", 0),
        "reasoning": best.get("reasoning") or "",
        "grade": best.get("grade"),
    })

# Create a dummy instance just to call the methods (they don't use instance state)
# Bypass __init__ by using __new__
dm = ClaudeDecisionMaker.__new__(ClaudeDecisionMaker)

print(f"Loaded {len(trades)} trades with reasoning\n")
print("=" * 95)
print("  LIVE IN-SOURCE GATE RUN — each trade through the actual methods")
print("=" * 95)

total_saved = 0.0
total_cost = 0.0
for t in trades:
    # Build a minimal decision-like object
    decision = SimpleNamespace(action=t["side"], grade=t["grade"])
    reasoning_lower = t["reasoning"].lower()
    # Check reasoning gate first (highest priority)
    rg_hit = None
    for phrase in ClaudeDecisionMaker._REASONING_HARD_GATE_PHRASES:
        if phrase in reasoning_lower:
            rg_hit = phrase
            break
    sw_hit = dm._check_opposing_sweep(decision, reasoning_lower)
    ip_hit = dm._check_ipda_extreme_fade(decision, reasoning_lower)
    hits = []
    if rg_hit: hits.append(f"REASONING('{rg_hit}')")
    if sw_hit: hits.append(f"SWEEP({sw_hit})")
    if ip_hit: hits.append(f"IPDA({ip_hit})")
    if hits:
        tag = "WIN " if t["pnl"] > 0 else "LOSS"
        print(f"  [BLOCK] [{tag}] {t['symbol']:<18} {t['side']:<4} ${t['pnl']:+8.2f}  {'; '.join(hits)}")
        if t["pnl"] > 0:
            total_cost += t["pnl"]
        else:
            total_saved += t["pnl"]

print()
print(f"Total money saved (losers blocked): ${abs(total_saved):.2f}")
print(f"Total money cost (winners blocked): ${total_cost:.2f}")
print(f"Net deployment value: ${-total_saved - total_cost:+.2f}")
