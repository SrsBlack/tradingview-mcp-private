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
]

DECISION_RX = re.compile(r"\[([A-Z0-9_:.\!]+)\]\s+Decision:\s+(BUY|SELL)\s+\(confidence=(\d+),")
REASON_RX = re.compile(r"\[([A-Z0-9_:.\!]+)\]\s+Reason:\s+(.*)")

decisions = []
current_decision = None
with LOG.open("r", encoding="utf-8", errors="replace") as f:
    for line in f:
        m = DECISION_RX.search(line)
        if m:
            current_decision = {"sym": m.group(1), "side": m.group(2), "conf": int(m.group(3)), "reason": ""}
            continue
        m = REASON_RX.search(line)
        if m and current_decision and m.group(1) == current_decision["sym"]:
            current_decision["reason"] = m.group(2)
            decisions.append(current_decision)
            current_decision = None

print(f"Total Claude BUY/SELL decisions in log: {len(decisions)}")
for phrase in CANDIDATE_PHRASES:
    matches = [d for d in decisions if phrase in d["reason"].lower()]
    print(f"\n'{phrase}' — {len(matches)} matches:")
    for d in matches[:10]:
        snip = d["reason"][:140]
        print(f"  {d['sym']:<22} {d['side']:<4} conf={d['conf']:>3}  | {snip}...")
