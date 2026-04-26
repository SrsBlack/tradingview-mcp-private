"""Concept-card prompt injector — teaches Claude how ICT builds a trade.

When specific confluence patterns fire, this module loads the relevant
ICT concept cards from strategy_knowledge/ict_concepts/ and formats them
into a compact context block for the Claude prompt.

The goal: instead of dumping all 52 concepts into every prompt (context
bloat), select only the ~3-7 concepts that actually matter for THIS
signal and show Claude the reasoning chain.

Input: a SymbolAnalysis object.
Output: a formatted string block appended to the user message.

Design:
    - Pick concepts based on what's TRUE in the analysis (sweep present,
      OB+FVG stacked, PO3 phase active, etc.)
    - Uses _index.json dependency graph to include prerequisite concepts
      via BFS traversal (depends_on edges), keeping context complete
    - Each concept contributes 1-3 lines — definition + the specific rule
      that applies to this setup
    - Hard cap on total length (~1500 chars) to prevent prompt bloat
    - Graceful fallback: if concept files missing, return empty string

Typical output size: 800-1200 chars (~200-300 tokens added per call).
This is a trade-off: higher Claude cost per call in exchange for better
reasoning grounded in ICT methodology.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONCEPTS_DIR = Path(__file__).parent / "strategy_knowledge" / "ict_concepts"
_MAX_CHARS = 1800
_MAX_CONCEPTS = 8  # hard cap — never load more than this many cards


def _load_index_json() -> dict[str, Any]:
    """Load _index.json from ict_concepts/, cached in memory."""
    path = _CONCEPTS_DIR / "_index.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@lru_cache(maxsize=64)
def _load_concept(name: str) -> dict[str, Any]:
    """Load a concept card JSON, cached per-process."""
    path = _CONCEPTS_DIR / f"{name}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _summarize_concept(name: str, concept: dict, hint: str = "") -> str:
    """Produce a 1-2 line teaching excerpt for a concept card.

    Pulls the definition (truncated to 120 chars) + the runtime hint.
    Aggressive truncation keeps prompt bloat in check.
    """
    if not concept:
        return ""

    definition = concept.get("definition", "").strip()
    if len(definition) > 120:
        first_period = definition.find(". ")
        if 40 < first_period < 120:
            definition = definition[:first_period + 1]
        else:
            definition = definition[:117] + "..."

    lines = []
    if definition:
        lines.append(f"  {definition}")
    if hint:
        lines.append(f"  -> {hint}")

    return "\n".join(lines) if lines else ""


# ---------------------------------------------------------------------------
# Dependency traversal — which concepts are transitively relevant
# ---------------------------------------------------------------------------

def _get_relevant_concepts(a: Any) -> list[str]:
    """Get only concepts relevant to this signal via dependency traversal.

    Starts from triggered concepts (based on non-zero scores / detected
    signals), then performs BFS over the depends_on graph in _index.json
    to include any prerequisite concepts. Foundation concepts are always
    included. Returns a flat list of concept names.
    """
    triggered: set[str] = set()

    # Map scores / flags to concept names
    if getattr(a, "structure_score", 0) > 0:
        triggered.add("market_structure")
    if getattr(a, "ob_score", 0) > 0:
        triggered.add("order_blocks")
    if getattr(a, "fvg_score", 0) > 0:
        triggered.add("fair_value_gaps")
    if getattr(a, "smt_score", 0) > 0:
        triggered.add("SMT_divergence")
    if getattr(a, "ote_score", 0) > 0:
        triggered.add("optimal_trade_entry")
    if getattr(a, "has_cisd", False):
        triggered.add("CISD")
    if getattr(a, "po3_phase", ""):
        triggered.add("power_of_three_and_AMD")
    if getattr(a, "has_judas_swing", False):
        triggered.add("judas_swing")
    if getattr(a, "sweep_detected", False):
        triggered.add("liquidity_sweep")
    if getattr(a, "displacement_confirmed", False):
        triggered.add("displacement")

    # Always include foundation concepts
    triggered.add("premium_discount")
    triggered.add("sessions_and_kill_zones")

    # Load dependency graph from index
    index = _load_index_json()
    if not index:
        return list(triggered)

    # Build concept-to-layer and layer dependency maps from the graph
    dep_graph = index.get("concept_dependency_graph", {})
    concept_to_layer: dict[str, str] = {}
    layer_feeds_into: dict[str, list[str]] = {}  # layer -> list of concept names it feeds

    for layer_name, layer_data in dep_graph.items():
        if layer_name.startswith("_") or not isinstance(layer_data, dict):
            continue
        for concept in layer_data.get("concepts", []):
            concept_to_layer[concept] = layer_name
        # feeds_into at layer level contains concept names from downstream layers
        layer_feeds_into[layer_name] = layer_data.get("feeds_into", [])

    relevant: set[str] = set(triggered)

    # For each triggered concept, add concepts from its layer's feeds_into
    # (these are the downstream concepts that depend on it)
    for concept in list(triggered):
        layer = concept_to_layer.get(concept, "")
        if layer:
            # Add all concepts from the same layer (they share context)
            layer_data = dep_graph.get(layer, {})
            for peer in layer_data.get("concepts", []):
                relevant.add(peer)

    return list(relevant)


# ---------------------------------------------------------------------------
# Concept selectors — which cards to load based on analysis state
# ---------------------------------------------------------------------------

def _select_relevant_concepts(a: Any) -> list[tuple[str, str]]:
    """Return (concept_name, hint) tuples for concepts relevant to this signal.

    Ordered by priority: most load-bearing concepts first. Hints are
    short reminders of HOW the concept applies to this specific setup.
    """
    picks: list[tuple[str, str]] = []

    direction = getattr(a, "direction", "") or ""
    confluence = " ".join(getattr(a, "confluence_factors", []) or []).lower()
    adv_factors = " ".join(getattr(a, "advanced_factors", []) or []).lower()

    # Always include: premium/discount (zone rule is the #1 filter)
    if getattr(a, "pd_zone", ""):
        zone = a.pd_zone
        aligned = "aligned" if getattr(a, "pd_aligned", False) else "MISALIGNED"
        hint = f"Current: {direction} in {zone} zone ({aligned})"
        picks.append(("premium_discount", hint))

    # OB + FVG stack → show both cards so Claude understands the combo
    ob_score = getattr(a, "ob_score", 0)
    fvg_score = getattr(a, "fvg_score", 0)
    if ob_score >= 10 and fvg_score >= 9:
        picks.append(("order_blocks", "OB + FVG stacked — highest-probability entry zone"))
        picks.append(("fair_value_gaps", "Gap filled → displacement proven"))
    elif ob_score >= 10:
        picks.append(("order_blocks", "OB present (no FVG confluence)"))
    elif fvg_score >= 9:
        picks.append(("fair_value_gaps", "FVG present (no OB confluence — widen SL)"))

    # Sweep + SMT → show how the combo confirms manipulation
    if getattr(a, "sweep_detected", False) or "sweep" in confluence:
        hint = "Sweep confirmed"
        if getattr(a, "smt_score", 0) > 0:
            hint += " + SMT divergence = engineered manipulation (not breakout)"
        picks.append(("liquidity_sweep", hint))

    # Displacement — the prerequisite gate for OB validity
    if getattr(a, "displacement_confirmed", False):
        picks.append(("displacement", "Proves institutional commitment — OB valid"))

    # Market Structure — foundational, always teach when structure is analyzed
    structure_score = getattr(a, 'structure_score', 0)
    if structure_score > 0:
        strength = "strong" if structure_score >= 20 else "partial" if structure_score >= 10 else "weak"
        picks.append(("market_structure", f"Structure ({strength}): score {structure_score:.0f}/30"))

    # SMT Divergence — confirms sweep was manipulation, not breakout
    if getattr(a, 'smt_score', 0) > 0 or getattr(a, 'has_smt', False):
        picks.append(("SMT_divergence", "Divergence confirmed — correlated asset didn't make new extreme"))

    # OTE — only meaningful with PD alignment
    if getattr(a, "ote_score", 0) >= 6:
        ote_hint = "At 0.618-0.786 retracement — optimal entry"
        if not getattr(a, "pd_aligned", False):
            ote_hint += " BUT in wrong PD zone (penalty applied)"
        picks.append(("optimal_trade_entry", ote_hint))

    # Fibonacci extensions — TP targeting framework
    fib_levels = getattr(a, 'fib_tp_levels', [])
    if fib_levels:
        picks.append(("fibonacci_extensions", f"TP targets: 1.272={fib_levels[0]:,.2f}, 1.618={fib_levels[1]:,.2f}"))

    # PO3 / AMD — ALWAYS inject (universal framework, not optional)
    session = (getattr(a, "session_type", "") or "").lower()
    po3_phase = getattr(a, 'po3_phase', '')
    if po3_phase:
        picks.append(("power_of_three_and_AMD", f"Current phase: {po3_phase}"))
    elif "asian" in session:
        picks.append(("power_of_three_and_AMD", "ACCUMULATION phase — range building, low conviction"))
    elif "london" in session and "ny" not in session:
        picks.append(("power_of_three_and_AMD", "MANIPULATION phase — expect false moves"))
    elif "ny" in session or "overlap" in session:
        picks.append(("power_of_three_and_AMD", "DISTRIBUTION phase — real directional move"))
    else:
        picks.append(("power_of_three_and_AMD", "Identify current phase before entry"))

    # CISD — earliest reversal signal
    if getattr(a, 'has_cisd', False) or "cisd" in confluence or "cisd" in adv_factors:
        picks.append(("CISD", "Candle-level phase change confirmed — earliest reversal signal"))

    # Breaker blocks — failed OB flips polarity
    if "breaker" in adv_factors:
        picks.append(("breaker_blocks", "Failed OB flipped — now acts as support/resistance"))

    # Judas swing
    if getattr(a, "has_judas_swing", False):
        picks.append(("judas_swing", "Manipulation-phase false move — distribution expected"))

    # Silver Bullet time window (10-11 AM NY)
    if getattr(a, "is_silver_bullet", False):
        picks.append(("silver_bullet", "Within 10-11 AM NY window — time-specific FVG entry"))

    # Turtle Soup — counter-liquidity reversal
    if "turtle" in confluence or "turtle" in adv_factors:
        picks.append(("turtle_soup", "Sweep+reversal = stops harvested at swing failure"))

    # CRT — multi-TF sweep+reversal (Candle Range Theory). Hint is differentiated
    # by highest TF firing: D1 = major reversal, H4 = swing-tradable, M15 = intrabar.
    if "crt_d1" in adv_factors:
        picks.append((
            "CRT_candle_range_theory",
            "Daily CRT — sweep of prior-day extreme + close back inside daily range. "
            "Major reversal signal; target = opposite daily extreme.",
        ))
    elif "crt_h4" in adv_factors:
        picks.append((
            "CRT_candle_range_theory",
            "H4 CRT — sweep of prior-H4 extreme + close back inside. "
            "Swing-tradable reversal; target = opposite H4 extreme.",
        ))
    elif "crt_m15" in adv_factors or "crt(" in adv_factors:
        picks.append((
            "CRT_candle_range_theory",
            "M15 CRT — single-bar sweep of prior-bar extreme + close back inside. "
            "Intrabar confluence; target = opposite extreme.",
        ))

    # Liquidity voids — unfilled LVN zones above/below current price act as draws
    voids = getattr(a, "liquidity_voids", None) or []
    cp = getattr(a, "current_price", 0.0)
    if voids and cp > 0:
        above = sum(1 for (lo, hi) in voids if lo > cp)
        below = sum(1 for (lo, hi) in voids if hi < cp)
        if above and below:
            hint = f"{above} void(s) above + {below} below — magnets for retracement"
        elif above:
            hint = f"{above} unfilled void(s) above — bullish draw target(s)"
        else:
            hint = f"{below} unfilled void(s) below — bearish draw target(s)"
        picks.append(("liquidity_void", hint))

    # Unicorn / Venom — advanced reversal models
    if "unicorn" in confluence or "unicorn" in adv_factors:
        picks.append(("unicorn_model", "Breaker + FVG overlap = high-conviction reversal"))
    if "venom" in confluence or "venom" in adv_factors:
        picks.append(("venom_model", "Liquidity grab + FVG rejection = reversal setup"))

    # Market maker model
    if "mm_" in adv_factors or "mmbm" in adv_factors or "mmsm" in adv_factors:
        picks.append(("market_maker_model", "Full institutional cycle detected"))

    # IPDA ranges — institutional delivery framework
    ipda = getattr(a, 'ipda_ranges', None)
    if ipda:
        for label, rng in ipda.items():
            if rng.get('pct', 50) > 90 or rng.get('pct', 50) < 10:
                picks.append(("IPDA", f"Price at {label} extreme ({rng['pct']:.0f}%) — high-probability reversal zone"))
                break
        else:
            # Not at extreme but IPDA data is available — still useful for context
            if any(r.get('pct', 50) > 75 or r.get('pct', 50) < 25 for r in ipda.values()):
                picks.append(("IPDA", "Price approaching IPDA range boundary — watch for reversal"))

    # Quarterly shift
    q_shift = getattr(a, 'quarterly_shift', None)
    if q_shift:
        strength = q_shift.get('strength', 'developing')
        direction = q_shift.get('direction', '?')
        picks.append(("quarterly_shifts", f"Quarterly shift {direction} ({strength}) — macro bias override"))

    # Opening gaps (NWOG/NDOG)
    nwog = getattr(a, 'nwog_count', 0)
    ndog = getattr(a, 'ndog_count', 0)
    if nwog > 0 or ndog > 0:
        gap_parts = []
        if nwog > 0:
            gap_parts.append(f"NWOG({nwog})")
        if ndog > 0:
            gap_parts.append(f"NDOG({ndog})")
        picks.append(("opening_gaps", f"Active gaps: {', '.join(gap_parts)} — price seeks to fill these"))

    # HTF FVG obstacle — trading into opposing imbalance
    if getattr(a, 'htf_fvg_obstacle', False):
        zone = getattr(a, 'htf_fvg_obstacle_zone', '')
        picks.append(("inverse_fvg", f"WARNING: {zone} — opposing H4 FVG = institutional resistance"))

    # Key opens — when price is near an open level
    key_opens = getattr(a, 'key_opens', {})
    if key_opens and getattr(a, 'current_price', 0) > 0:
        price = a.current_price
        for label, open_price in key_opens.items():
            dist_pct = abs(price - open_price) / price * 100
            if dist_pct < 0.3:  # Within 0.3% of an open level
                picks.append(("session_levels", f"Price at {label} ({open_price:,.2f}) — equilibrium reference, watch for reaction"))
                break

    return picks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_concept_teaching_block(a: Any) -> str:
    """Return a formatted teaching block for the Claude prompt.

    Uses dependency traversal via _index.json to inject only concepts
    that are transitively relevant to the detected signals. Empty string
    if no relevant concepts — zero prompt bloat on signals that don't
    benefit from the extra context.
    """
    # Get the set of transitively-relevant concept names via BFS over
    # the dependency graph. This filters out concepts that fired but are
    # not connected to the current signal chain.
    relevant_names = set(_get_relevant_concepts(a))

    all_picks = _select_relevant_concepts(a)
    # Filter to only picks whose concept name is in the relevant set
    picks = [(name, hint) for name, hint in all_picks if name in relevant_names]
    if not picks:
        return ""

    lines = [
        "",
        "ICT CONCEPT CHAIN (how this trade is built, per ICT methodology):",
    ]

    total_chars = 0
    seen: set[str] = set()
    added = 0
    for name, hint in picks:
        if name in seen:
            continue
        seen.add(name)
        if added >= _MAX_CONCEPTS:
            break

        concept = _load_concept(name)
        if not concept:
            continue
        summary = _summarize_concept(name, concept, hint=hint)
        if not summary:
            continue
        block = f"- {name.upper()}:\n{summary}"

        # Enforce size cap — graceful truncation
        if total_chars + len(block) > _MAX_CHARS:
            lines.append(f"  [truncated — {len(picks) - added} more concepts available]")
            break
        lines.append(block)
        total_chars += len(block)
        added += 1

    # Always append synergy / gate findings if present
    synergy_explanations = getattr(a, "synergy_explanations", []) or []
    if synergy_explanations:
        lines.append("")
        lines.append("ACTIVE SYNERGIES/GATES (cross-correlations.json):")
        for e in synergy_explanations[:5]:  # cap at 5
            lines.append(f"- {e}")

    return "\n".join(lines)


def build_gate_violation_warning(a: Any) -> str:
    """If any gates are violated, produce an explicit warning block.

    Gate violations are high-signal — they should be prominent in the
    prompt so Claude can weigh them heavily (often → SKIP).
    """
    violations = getattr(a, "gate_violations", []) or []
    if not violations:
        return ""

    lines = ["", "*** GATE VIOLATIONS (ICT rules breached) ***"]
    for v in violations:
        lines.append(f"- {v}")
    lines.append("Trade quality is reduced. Consider SKIP unless very strong overriding confluence.")
    return "\n".join(lines)
