# `bridge_integration` Backlog

> Cards in this directory whose `bridge_integration` field is currently a stub. Each entry needs real, accurate text describing how the concept fires (or should fire) in our bridge.
>
> **This is intentional.** Better to have a stub flagged in the lint than rushed text Claude treats as authoritative. See `project_kb_schema_upgrade_plan.md` in user memory for the multi-session plan.

---

## Working approach (per card)

For each card, ~10-15 minutes of careful work:

1. **Re-read the concept definition.** What is it actually claiming about market behavior?
2. **Walk the bridge code** (`bridge/ict_pipeline.py`, `bridge/synergy_scorer.py`, `bridge/claude_decision.py`, `bridge/live_executor_adapter.py`) — does anything currently use this concept?
3. **Decide:**
   - **Already integrated, just not documented:** write the prose describing what the bridge already does. Cite specific gate names / file paths / line numbers.
   - **Should be integrated, not yet:** decide the design first, ship the code change, then document the integration.
   - **Informational only:** write explicit text saying "This is methodological context; no specific gate maps to it because [reason]."
4. **Verify the description against running code.** No invented file paths, no aspirational claims. If you say "fires in `claude_decision.py:_pre_gate` line 600," that line had better do what you claim.
5. Replace the stub. Run `python scripts/lint_memory.py` to confirm.

When this backlog is empty, tighten the lint to reject `[NOT YET DEFINED` markers entirely.

---

## Stubs to fill in (18 cards as of 2026-04-26)

Suggested priority order: high-impact concepts first, since these get injected into Claude prompts most often.

### High priority — concepts that already drive scoring

| Card | Layer | Why high priority |
|------|-------|-------------------|
| `fair_value_gaps` | 4 | Core entry zone concept; used in nearly every Grade A signal |
| `order_blocks` | 4 | Same — second pillar of OB+FVG entry stacking |
| `liquidity` | 1 | Foundational; sweeps drive every ICT setup |
| `market_structure` (already filled? confirm) | 1 | Pre-trade bias depends on this. **Verify it's not in the stub list — if it is, prioritize.** |
| `power_of_three_and_AMD` | 2 | Accumulation/manipulation/distribution timing — `claude_decision.py` references this |
| `sessions_and_kill_zones` | 2 | Hard gate in pre-gate logic |
| `session_levels` | 1 | PDH/PDL/PWH/PWL — used as DOL targets |
| `judas_swing` | 3 | Manipulation phase detection |

### Medium priority — meta-cards

| Card | Layer | Why medium |
|------|-------|------------|
| `common_mistakes` | section | Lists what to avoid; useful but not directly executed |
| `conflict_resolution` | section | Priority rules; informational reference |
| `market_maker_model` | composite | High-level framework; informational |
| `stop_raid_displacement_retracement` | composite | Atomic ICT pattern; useful for prompt-shaping |

### Lower priority — narrower concepts

| Card | Layer | Why lower |
|------|-------|-----------|
| `CISD` | 3 | Pre-CHoCH detection; may not have a current bridge gate |
| `CRT_candle_range_theory` | 3 | Micro structure theory; supports other concepts |
| `dealing_range` | 1 | Used implicitly in P/D zone logic |
| `fibonacci_extensions` | 5 | Used in TP target setting |
| `liquidity_void` | 4 | Niche zone type; verify if bridge detects it |
| `market_philosophy` | 0 | Foundational; mostly informational |
| `volume_profile` | 5 | May be aspirational; verify against current code |

---

## How this list will shrink

Each work session that touches the KB:
1. Run `python scripts/lint_memory.py` — see current stub count
2. Pick 5-10 cards from the highest-priority section
3. Do the per-card workflow above
4. Update this file (delete entries as they're filled in)
5. Commit the batch

Do NOT batch all 18 in one session. The whole point of having a visible backlog is that we resist the rush to "finish" — the system improves as the integration text becomes accurate, not as the stubs disappear.

---

## When a card is "done"

The replacement `bridge_integration` text:
- References specific bridge artifacts (file:line, gate name, synergy ID, scoring weight)
- States real conditions, not aspirations ("fires when X" not "should fire when X")
- Survives a code-truth check (whatever it claims is verifiable in the running bridge)
- Doesn't make Claude reason from a false premise on any reasonable trade

If you're not sure a description meets the bar, leave the stub and add a note here about what's blocking it.
