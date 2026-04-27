"""How often does Claude say 'C-equivalent' / 'downgraded to C' in trade reasoning?

Scans logs/trading.log for all Claude SELL/BUY decisions and counts how
many had the phrase. Goal: confirm adding it to _REASONING_HARD_GATE_PHRASES
catches the UKOIL loss without false positives on winners.
"""
from __future__ import annotations
import re
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "logs" / "trading.log"

# Phrases under consideration
CANDIDATE_PHRASES = [
    "c-equivalent",
    "c-equivalent conviction",
    "downgraded to c",
    "downgrade to c",
    "to c-equivalent",
    "grade a signal downgraded",
    "grade b signal downgraded",
    # 2026-04-27 extension after GBPJPY -$179 trap (same UKOIL pattern,
    # different phrasing). Claude wrote "reduce conviction to Grade C
    # threshold" — slipped through the c-equivalent-only gate.
    "grade c threshold",
    "to grade c threshold",
    "grade c risk",
    "grade c execution",  # already in original list as "grade b/c execution" — redundant safety
    "to grade c",         # broadest variant; verify no winner false positives
    "downgrades from grade a to c",
    "auto-downgrade from grade a to c",
]

OPEN_RX = re.compile(r"\[([A-Z0-9_:.\!]+)\]\s+OPENED:\s+(BUY|SELL)\s+\S+\s+@\s+([\d.]+)")
DECISION_RX = re.compile(r"\[([A-Z0-9_:.\!]+)\]\s+Decision:\s+(BUY|SELL)\s+\(confidence=(\d+),")
REASON_RX = re.compile(r"\[([A-Z0-9_:.\!]+)\]\s+Reason:\s+(.*)")

# Walk the log, but only keep the Decision+Reason that immediately
# precedes an OPENED for the SAME symbol — those are the actual entries
# that produced live trades.
all_decisions = []
last_per_sym = {}  # sym -> {"decision": dict, "reason_line_no": int}
with LOG.open("r", encoding="utf-8", errors="replace") as f:
    for ln_no, line in enumerate(f, 1):
        m = DECISION_RX.search(line)
        if m:
            sym = m.group(1)
            last_per_sym[sym] = {
                "sym": sym, "side": m.group(2), "conf": int(m.group(3)),
                "reason": "", "_ln": ln_no, "_opened": False,
            }
            continue
        m = REASON_RX.search(line)
        if m and m.group(1) in last_per_sym:
            last_per_sym[m.group(1)]["reason"] = m.group(2)
            continue
        m = OPEN_RX.search(line)
        if m:
            sym = m.group(1)
            if sym in last_per_sym:
                d = dict(last_per_sym[sym])
                d["_opened"] = True
                d["entry_price"] = float(m.group(3))
                all_decisions.append(d)

decisions = all_decisions
print(f"Total Claude BUY/SELL ENTRIES (Decision+OPENED pair) in log: {len(decisions)}")
for phrase in CANDIDATE_PHRASES:
    matches = [d for d in decisions if phrase in d["reason"].lower()]
    print(f"\n'{phrase}' — {len(matches)} matches:")
    for d in matches[:10]:
        snip = d["reason"][:140]
        print(f"  {d['sym']:<22} {d['side']:<4} conf={d['conf']:>3}  | {snip}...")
