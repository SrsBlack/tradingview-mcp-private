"""One-off backtest of two proposed reasoning gate filters against Apr 19-24 live trades."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

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


def parse_ts(val: str) -> datetime | None:
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except Exception:
        return None


live_events = [t for t in all_trades if t.get("event") in ("OPEN", "CLOSE") and t.get("mode") != "paper_shadow"]
opens = {t.get("ticket"): t for t in live_events if t.get("event") == "OPEN"}
closes = [t for t in live_events if t.get("event") == "CLOSE"]

trades: list[dict] = []
for c in closes:
    if c.get("reason") == "TP (while offline)":
        continue  # pollution from the Apr 21 blowout
    ticket = c.get("ticket")
    o = opens.get(ticket, {})
    sym = c.get("symbol") or o.get("symbol", "")
    opened_at = o.get("opened_at") or o.get("timestamp")
    if not opened_at:
        continue
    t_open = parse_ts(opened_at)
    if not t_open:
        continue
    best: dict | None = None
    best_delta = timedelta(hours=4)
    for d in all_decisions:
        if d.get("symbol") != sym or d.get("action") in ("SKIP", "HOLD", None):
            continue
        dt = parse_ts(d.get("timestamp", ""))
        if not dt:
            continue
        diff = t_open - dt
        if timedelta(0) <= diff < best_delta:
            best_delta = diff
            best = d
    if not best:
        continue
    trades.append({
        "symbol": sym,
        "side": c.get("direction"),
        "opened_at": t_open,
        "reason": c.get("reason"),
        "pnl": c.get("pnl", 0),
        "r": c.get("r_multiple", 0),
        "grade": best.get("grade"),
        "reasoning": (best.get("reasoning") or "").lower(),
    })


def summarize(label: str, trades: list[dict]) -> None:
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    print(f"{label}: {len(trades)} trades | wins {len(wins)} losses {len(losses)} | net ${sum(t['pnl'] for t in trades):+.2f}")


print(f"Loaded {len(trades)} live trades with matched reasoning\n")
summarize("All", trades)
print()


def filter_a_opposing_sweep(t: dict) -> str | None:
    r = t["reasoning"]
    side = t["side"]
    swept_low_phrases = (
        "sweep of low", "sweep of asian low", "sweep of pdl", "sweep of pwl",
        "sweep of pml", "sweep of lo.l", "sweep of d_low", "sweep of session low",
        "swept low", "swept the low", "swept pdl", "swept pwl", "swept asian low",
        "liquidity sweep of low", "liq sweep of low", "sweep of equal lows",
        "sweep of lol", "sweep of london low",
        "sweep of d_open+pdl", "sweep of pdl+",
    )
    swept_high_phrases = (
        "sweep of high", "sweep of asian high", "sweep of pdh", "sweep of pwh",
        "sweep of pmh", "sweep of lo.h", "sweep of d_high", "sweep of session high",
        "swept high", "swept the high", "swept pdh", "swept pwh", "swept asian high",
        "liquidity sweep of high", "liq sweep of high", "sweep of equal highs",
        "sweep of loh", "sweep of london high",
        "sweep of d_open+pdh", "sweep of pdh+",
    )
    swept_low = any(p in r for p in swept_low_phrases)
    swept_high = any(p in r for p in swept_high_phrases)
    if side == "BUY" and swept_high and not swept_low:
        return "BUY after sweep of high (fading reversal)"
    if side == "SELL" and swept_low and not swept_high:
        return "SELL after sweep of low (fading reversal)"
    return None


def filter_b_ipda_extreme(t: dict) -> str | None:
    r = t["reasoning"]
    side = t["side"]
    at_high = any(p in r for p in (
        "ipda 20/40/60d high extreme", "ipda 20/40/60 high extreme",
        "ipda 20d high extreme", "ipda 40d high extreme", "ipda 60d high extreme",
        "at 20d high", "at 40d high", "at 60d high",
        "multi-day high extreme", "ipda high extreme", "at ipda high",
    ))
    at_low = any(p in r for p in (
        "ipda 20/40/60d low extreme", "ipda 20/40/60 low extreme",
        "ipda 20d low extreme", "ipda 40d low extreme", "ipda 60d low extreme",
        "at 20d low", "at 40d low", "at 60d low",
        "multi-day low extreme", "ipda low extreme", "at ipda low",
    ))
    if side == "SELL" and at_high:
        return "SELL at IPDA high extreme"
    if side == "BUY" and at_low:
        return "BUY at IPDA low extreme"
    return None


def run_filter(name: str, fn) -> None:
    print("=" * 95)
    print(f"  {name}")
    print("=" * 95)
    blocked = [(t, fn(t)) for t in trades if fn(t)]
    wins_blocked = [t for t, _ in blocked if t["pnl"] > 0]
    losses_blocked = [t for t, _ in blocked if t["pnl"] <= 0]
    saved = sum(t["pnl"] for t in losses_blocked)  # negative = money saved
    cost = sum(t["pnl"] for t in wins_blocked)      # positive = money we would lose
    net = -saved - cost
    print(f"Blocked {len(blocked)} trades: {len(losses_blocked)} losers, {len(wins_blocked)} winners")
    print(f"  Money saved (losers avoided): ${abs(saved):.2f}")
    print(f"  Money cost (winners killed):  ${cost:.2f}")
    print(f"  Net deployment impact: ${net:+.2f}")
    print()
    for t, reason in blocked:
        tag = "WIN " if t["pnl"] > 0 else "LOSS"
        print(f"  [{tag}] {t['symbol']:<18} {t['side']:<4} ${t['pnl']:+8.2f} R={t['r']:+.2f} — {reason}")
    print()


run_filter("FILTER A: opposing-sweep", filter_a_opposing_sweep)
run_filter("FILTER B: IPDA-extreme fade", filter_b_ipda_extreme)


print("=" * 95)
print("  COMBINED A + B")
print("=" * 95)
blocked_combo = [(t, filter_a_opposing_sweep(t) or filter_b_ipda_extreme(t)) for t in trades
                 if filter_a_opposing_sweep(t) or filter_b_ipda_extreme(t)]
wins_c = [t for t, _ in blocked_combo if t["pnl"] > 0]
loss_c = [t for t, _ in blocked_combo if t["pnl"] <= 0]
saved_c = sum(t["pnl"] for t in loss_c)
cost_c = sum(t["pnl"] for t in wins_c)
print(f"Blocked {len(blocked_combo)}: {len(loss_c)} losers saved ${abs(saved_c):.2f}, {len(wins_c)} winners killed ${cost_c:.2f}")
print(f"Net: ${-saved_c - cost_c:+.2f}")
