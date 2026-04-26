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

## Stubs to fill in (8 cards as of 2026-04-26)

Suggested priority order: high-impact concepts first, since these get injected into Claude prompts most often.

> **Note (2026-04-26):** `market_structure` was incorrectly listed as a stub in earlier versions of this backlog. Its `bridge_integration` is a dict (see SCHEMA.md — string is preferred but dict was the legacy shape) with `detection`/`prompt_display`/`score_impact` keys; it was never counted as a stub by the lint. If a future cleanup wants to normalize it to a string-form like the other cards, that's a separate doc task and does not affect Track 2.

### Completed 2026-04-26 (Track 2 batch 1)

- `fair_value_gaps` — already integrated. M15 detection drives 15-pt scoring; H4 closed-bar detection feeds HTF FVG obstacle gate (-5) + Claude prompt warning; D1 FVG advanced_factors; FVG-CE entry pricing.
- `order_blocks` — already integrated. M15 detection with require_fvg=True (displacement enforcement); 15-pt scoring; OB+FVG synergy +10; HiddenOB → OB-at-HVN +3; breaker_blocks NOT detected in code (informational).
- `liquidity` — already integrated. build_liquidity_map + scan_sweeps with significance filter; 20-pt scoring; DOL pre-filter (4x ATR rule) hard SKIP; equal levels + opposing-sweep post-gate.
- `power_of_three_and_AMD` — already integrated. detect_po3_phase per cycle; advanced_factor 'PO3_<phase>'; always-injected concept; Wyckoff/PO3 alignment synergy +4. Daily/weekly PO3 patterns informational only.
- `sessions_and_kill_zones` — already integrated. SessionInfo at start of each cycle; 10-pt scoring; KILL ZONE GATE hard pre-gate with crypto/JPY-Tokyo/Grade-A-displacement exceptions; phrase gates removed (caught winner).

### Completed 2026-04-26 (Track 2 batch 2)

- `dealing_range` — already integrated. M15 + H4 ranges from detect_swings (last-3 highs/lows); drives ZONE GATE (hard SKIP), HTF Zone Check (Grade A→B), OTE zone, Fibonacci TP. Nested H1 range NOT computed (only M15/H4).
- `session_levels` — already integrated. PDH/PDL/PWH/PWL/PMH/PML/Asian range/key opens via build_liquidity_map; sweep significance filter; DOL pre-filter; reasoning post-gate phrase tuples for opposing-sweep enforcement.
- `judas_swing` — already integrated. detect_judas_swing per cycle (uses asian_range + daily_bias); has_judas_swing + judas_direction populated; Judas+KZ synergy +6; Wyckoff/PO3 alignment +4. NOT a hard gate — prompt-context only.
- `common_mistakes` — informational only. Meta-card NOT loaded by claude_decision or concept_injector. All 6 mistakes are enforced ELSEWHERE by dedicated code paths (ZONE GATE, HTF Alignment, displacement-required, ATR floor, DOL pre-filter, OB-without-displacement gate). Card serves as human-readable index.
- `conflict_resolution` — partially integrated. File IS loaded by claude_decision.py:291 but `rules` array is NOT iterated; only 2 hardcoded conflict checks fire (Asian-displacement, OB-no-displacement). Remaining 12 rules are surfaced indirectly via dedicated code paths or are real gaps (rules 5/11/13 + BPR aspect of 12 not enforced).

### High priority — composite frameworks

| Card | Layer | Why high priority |
|------|-------|-------------------|
| `market_maker_model` | composite | High-level framework; synergy_scorer Wyckoff/PO3 alignment uses MM_ factor — verify integration |
| `stop_raid_displacement_retracement` | composite | Atomic ICT pattern; useful for prompt-shaping |

### Lower priority — narrower concepts

| Card | Layer | Why lower |
|------|-------|-----------|
| `CISD` | 3 | Pre-CHoCH detection; verify has_cisd + 'CISD + PO3' synergy integration |
| `CRT_candle_range_theory` | 3 | Micro structure theory; supports other concepts |
| `fibonacci_extensions` | 5 | Used in TP target setting (fib_tp_levels) |
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
