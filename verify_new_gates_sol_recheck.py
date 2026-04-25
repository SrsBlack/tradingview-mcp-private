"""Re-run gate backtest with SOL ticket 434190150 reclassified.

Three scenarios:
  (a) SOL as the manual-close loss the broker actually recorded: -$120.30
  (b) SOL as breakeven $0 (price recovered to entry within 4h)
  (c) SOL excluded from the trade pool entirely

Print per-scenario block list and deployment value, plus a phrase-attribution
table showing which phrases fired only on SOL (i.e., would lose justification
if SOL is reclassified).
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))

from bridge.claude_decision import ClaudeDecisionMaker

SOL_TICKET = 434190150
SESSION_DIR = Path.home() / ".tradingview-mcp" / "sessions"
DATES = ["2026-04-19", "2026-04-20", "2026-04-21", "2026-04-22", "2026-04-23", "2026-04-24"]

NEW_PHRASES = {"no kill zone", "critically impaired", "displacement-no-structure", "displacement no structure"}


def load_raw() -> tuple[list[dict], list[dict]]:
    decisions: list[dict] = []
    trades: list[dict] = []
    for d in DATES:
        p = SESSION_DIR / f"{d}.json"
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        decisions.extend(s.get("decisions", []))
        trades.extend(s.get("trades", []))
    return decisions, trades


def parse_ts(v: str) -> datetime | None:
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        return None


def build_trades(all_decisions: list[dict], all_trades: list[dict]) -> list[dict]:
    live_events = [t for t in all_trades if t.get("event") in ("OPEN", "CLOSE") and t.get("mode") != "paper_shadow"]
    opens = {t.get("ticket"): t for t in live_events if t.get("event") == "OPEN"}
    closes = [t for t in live_events if t.get("event") == "CLOSE" and t.get("reason") != "TP (while offline)"]

    out: list[dict] = []
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
        out.append({
            "ticket": c.get("ticket"),
            "symbol": sym,
            "side": c.get("direction"),
            "pnl": c.get("pnl", 0),
            "r": c.get("r_multiple", 0),
            "reasoning": best.get("reasoning") or "",
            "grade": best.get("grade"),
        })
    return out


def run_gates(trades: list[dict], label: str) -> tuple[float, float, list[dict]]:
    dm = ClaudeDecisionMaker.__new__(ClaudeDecisionMaker)
    print("=" * 100)
    print(f"  SCENARIO: {label}")
    print("=" * 100)

    saved = 0.0
    cost = 0.0
    blocks: list[dict] = []
    for t in trades:
        decision = SimpleNamespace(action=t["side"], grade=t["grade"])
        reasoning_lower = t["reasoning"].lower()
        rg_hit = None
        for phrase in ClaudeDecisionMaker._REASONING_HARD_GATE_PHRASES:
            if phrase in reasoning_lower:
                rg_hit = phrase
                break
        sw_hit = dm._check_opposing_sweep(decision, reasoning_lower)
        ip_hit = dm._check_ipda_extreme_fade(decision, reasoning_lower)
        hits: list[str] = []
        if rg_hit:
            hits.append(f"REASONING('{rg_hit}')")
        if sw_hit:
            hits.append(f"SWEEP({sw_hit})")
        if ip_hit:
            hits.append(f"IPDA({ip_hit})")
        if hits:
            tag = "WIN " if t["pnl"] > 0 else ("ZERO" if t["pnl"] == 0 else "LOSS")
            print(f"  [BLOCK] [{tag}] {t['symbol']:<18} {t['side']:<4} ${t['pnl']:+9.2f}  {'; '.join(hits)}")
            if t["pnl"] > 0:
                cost += t["pnl"]
            else:
                saved += t["pnl"]
            blocks.append({**t, "rg_hit": rg_hit, "sw_hit": sw_hit, "ip_hit": ip_hit})
    print(f"\n  Money saved (losers/zeros blocked): ${abs(saved):.2f}")
    print(f"  Money cost (winners blocked):       ${cost:.2f}")
    print(f"  Net deployment value: ${-saved - cost:+.2f}\n")
    return saved, cost, blocks


def attribute_new_phrases(scenarios: dict[str, list[dict]]) -> None:
    print("=" * 100)
    print("  PHRASE ATTRIBUTION — does each new phrase still earn its keep?")
    print("=" * 100)
    print(f"  {'phrase':<35} {'baseline blocks':>16} {'recovered blocks':>18} {'BE blocks':>12} {'excl blocks':>14}")
    for phrase in sorted(NEW_PHRASES):
        cells = []
        for label in ["baseline", "recovered (-$120)", "breakeven ($0)", "excluded"]:
            blocks = scenarios[label]
            n = sum(1 for b in blocks if b.get("rg_hit") == phrase)
            cells.append(n)
        print(f"  {phrase:<35} {cells[0]:>16} {cells[1]:>18} {cells[2]:>12} {cells[3]:>14}")
    print()


def main() -> None:
    all_decisions, all_trades = load_raw()
    base_trades = build_trades(all_decisions, all_trades)
    print(f"Loaded {len(base_trades)} trades total\n")

    sol_idx = next((i for i, t in enumerate(base_trades) if t["ticket"] == SOL_TICKET), None)
    if sol_idx is None:
        print(f"ERROR: SOL ticket {SOL_TICKET} not found in trade set")
        return
    print(f"SOL row in baseline: pnl=${base_trades[sol_idx]['pnl']:+.2f}, reasoning_phrases_match={[p for p in NEW_PHRASES if p in base_trades[sol_idx]['reasoning'].lower()]}\n")

    scenarios: dict[str, list[dict]] = {}

    # (baseline) — current state
    _, _, b = run_gates(base_trades, "baseline (SOL classified as -$613.53 SL loss, the wrong premise)")
    scenarios["baseline"] = b

    # (a) recovered manual-close: -$120.30
    t_a = deepcopy(base_trades)
    t_a[sol_idx]["pnl"] = -120.30
    t_a[sol_idx]["r"] = -0.34
    _, _, b = run_gates(t_a, "(a) SOL = -$120.30 manual close (broker truth)")
    scenarios["recovered (-$120)"] = b

    # (b) breakeven — price recovered to entry within 4h
    t_b = deepcopy(base_trades)
    t_b[sol_idx]["pnl"] = 0.0
    t_b[sol_idx]["r"] = 0.0
    _, _, b = run_gates(t_b, "(b) SOL = $0.00 (would have closed at entry given recovery)")
    scenarios["breakeven ($0)"] = b

    # (c) SOL excluded
    t_c = [t for i, t in enumerate(base_trades) if i != sol_idx]
    _, _, b = run_gates(t_c, "(c) SOL excluded from pool (treat as inconclusive)")
    scenarios["excluded"] = b

    attribute_new_phrases(scenarios)

    # Special check: did any *non-SOL* trade get blocked solely by a new phrase?
    print("=" * 100)
    print("  NON-SOL TRADES blocked SOLELY by a new phrase (no other gate would catch them)")
    print("=" * 100)
    found_any = False
    for b in scenarios["baseline"]:
        if b["ticket"] == SOL_TICKET:
            continue
        if b.get("rg_hit") in NEW_PHRASES and not b.get("sw_hit") and not b.get("ip_hit"):
            # Would another older phrase still catch this?
            r = b["reasoning"].lower()
            other_hit = None
            for p in ClaudeDecisionMaker._REASONING_HARD_GATE_PHRASES:
                if p in NEW_PHRASES:
                    continue
                if p in r:
                    other_hit = p
                    break
            note = f"  (older phrase '{other_hit}' would still catch)" if other_hit else "  (NO older phrase catches this — load-bearing)"
            print(f"  {b['symbol']:<18} {b['side']:<4} pnl=${b['pnl']:+8.2f} blocked by '{b['rg_hit']}'{note}")
            found_any = True
    if not found_any:
        print("  none — every non-SOL block has redundant or non-new-phrase coverage")
    print()


if __name__ == "__main__":
    main()
