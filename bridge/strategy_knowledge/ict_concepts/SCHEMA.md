# ICT Concept Card Schema

> Canonical structure for files in `bridge/strategy_knowledge/ict_concepts/*.json`.
>
> Enforced by `scripts/lint_memory.py::check_kb_schema()`. New or migrated cards must conform.

---

## Required fields (every card)

| Field | Type | Purpose |
|-------|------|---------|
| `id` | string | Unique stable identifier (snake_case, matches filename without `.json`) |
| `layer` | string | One of: `layer_0_macro`, `layer_1_foundation`, `layer_2_context`, `layer_3_manipulation`, `layer_4_confirmation`, `layer_5_entry`, `layer_6_management`. Matches `_index.json` ontology. |
| `definition` | string | One- or two-paragraph plain-English description. What the concept *is*, not how to trade it. |
| `depends_on` | array of strings | Concept IDs (or external triggers) this concept requires as input. e.g. `["market_structure", "liquidity"]`. Must reference real card IDs. |
| `feeds_into` | array of strings | Concept IDs that consume this concept's output. Counterpart to `depends_on`. **Terminal cards** (where nothing further consumes the output, e.g. `entry_models`, `risk_management`) should have `feeds_into: []` (empty array, not absent). |
| `bridge_integration` | string | **How this concept fires in the bridge.** Specific: which gate, which file:line, what trigger condition. Stub `[NOT YET DEFINED — see INTEGRATION_BACKLOG.md]` is acceptable temporarily but counted in lint. |

## Recommended fields (most cards should have these)

| Field | Type | When to include |
|-------|------|-----------------|
| `related_to` | array of strings | Concepts that share theme but aren't strict dependencies. Good for cross-referencing. |
| `trading_rules` | array or object | Concrete rules a trader (or the bridge) follows when this concept applies. |
| `common_mistakes` | array or object | Failure patterns. **Use plural form `common_mistakes`** — singular `common_mistake` is deprecated. |
| `scoring` | object | If this concept contributes to confluence scoring, document the weight here. Match `synergy_scorer.py`. |

## Optional specialized sections

Concept-specific structured data goes here. Each card may have unique keys; that's fine as long as the required fields above are present.

Examples:
- `CBDR.json` has `CBR_calculation`, `SD_projections`, `asian_range_relationship`
- `IPDA.json` has `IPDA_framework`, `lookback_ranges`
- `risk_management.json` has `position_sizing`, `stop_loss`, `take_profit`, `trailing_stop_rules`

These don't need normalization across cards — they're concept-specific. The lint only enforces the required fields.

## Forbidden / deprecated

| Pattern | Reason | Migration target |
|---------|--------|------------------|
| `bridge_usage` | Inconsistent with majority | Rename to `bridge_integration` |
| `common_mistake` (singular) | Inconsistent with majority | Rename to `common_mistakes` (and ensure value is array/object, not single item) |
| `mistake_1_*`, `mistake_2_*` etc. (numbered top-level keys) | Should be array entries inside `common_mistakes` | Restructure into `common_mistakes` object/array |

---

## Lifecycle

### Creating a new card

1. Pick the `id` (snake_case, must equal filename)
2. Decide the `layer` based on `_index.json` ontology
3. Write `definition`, `depends_on`, `feeds_into`
4. Write `bridge_integration` — if you don't know yet, use the stub and add card to `INTEGRATION_BACKLOG.md`
5. Add card to `_index.json` under the appropriate layer's `concepts` array
6. Run `python scripts/lint_memory.py` — should pass

### Migrating an existing card

1. Run `python scripts/migrate_kb_schema.py` on the file
2. Manually review the diff
3. Fill in any gaps the migration couldn't auto-resolve
4. If `bridge_integration` ended up as a stub, add card to `INTEGRATION_BACKLOG.md`

### Retiring a card

1. Remove from `_index.json`
2. Move file to `archive/ict_concepts/` (don't delete — historical reference)
3. Update any cards that reference it in `depends_on` / `feeds_into` / `related_to`

---

## Why this schema and not something more rigid

The cards are heterogeneous on purpose — `CBDR` and `risk_management` and `judas_swing` describe genuinely different things. Forcing every card into the same flat shape would lose information.

The required fields exist because **without them, no programmatic process can use the KB consistently**:
- `id`/`layer`/`definition`/`depends_on`/`feeds_into` — needed for `_index.json` dependency traversal in `concept_injector.py`
- `bridge_integration` — needed so Claude knows how each concept actually fires in our system, not just what it means in theory

Specialized sections are free-form because that's where the interesting domain knowledge lives. Forcing those into a schema would either be too loose to enforce or would cripple the cards.
