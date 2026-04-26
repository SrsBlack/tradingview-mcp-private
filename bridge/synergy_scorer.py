"""Synergy & Gate scorer — turns cross_correlations.json into runtime bonuses/penalties.

The ICT knowledge base defines explicit **synergies** (concept combinations
that are super-additive) and **gates** (prerequisites that zero-out or
downgrade scores when violated). This module reads those rules and
evaluates them against the live SymbolAnalysis.

Usage:
    from bridge.synergy_scorer import evaluate_synergies

    result = evaluate_synergies(analysis)
    analysis.total_score += result.bonus_points - result.penalty_points
    analysis.confluence_factors.extend(result.named_factors)

Design:
    - Input: a SymbolAnalysis (score components + booleans + session info)
    - Output: a ScoreAdjustment with bonus points, penalty points, named
      factors (for logging), and a list of human-readable explanations
    - Reads cross_correlations.json once at module load (cached)
    - Zero impact on trading if file is missing — returns empty adjustment

Why as a separate module:
    The JSON knowledge base previously sat idle — Claude saw numeric scores
    only, not the SYNERGIES between concepts. This module makes the
    cross-connection data actually affect grading.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_KNOWLEDGE_DIR = Path(__file__).parent / "strategy_knowledge"


@dataclass
class ScoreAdjustment:
    """Result of synergy/gate evaluation.

    bonus_points:  Additive — applied to total_score, capped at 100 externally.
    penalty_points: Additive — subtracted from total_score.
    named_factors:  Machine-readable tags ("OB+FVG_stack") for logging.
    explanations:   Human-readable ("OB + FVG overlap — highest probability entry zone").
    gate_violations: Gate rules that failed — informs Claude the trade is risky.
    """
    bonus_points: float = 0.0
    penalty_points: float = 0.0
    named_factors: list[str] = field(default_factory=list)
    explanations: list[str] = field(default_factory=list)
    gate_violations: list[str] = field(default_factory=list)

    @property
    def net_delta(self) -> float:
        return self.bonus_points - self.penalty_points


@lru_cache(maxsize=1)
def _load_cross_correlations() -> dict[str, Any]:
    """Load cross_correlations.json once — cached for the process lifetime."""
    path = _KNOWLEDGE_DIR / "ict_concepts" / "cross_correlations.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Synergy predicates — each maps a synergy definition to a runtime check
# ---------------------------------------------------------------------------

def _has_ob(a: Any) -> bool:
    """OB is 'present' when its confluence score is meaningful."""
    return getattr(a, "ob_score", 0) >= 10


def _has_fvg(a: Any) -> bool:
    return getattr(a, "fvg_score", 0) >= 9


def _has_sweep(a: Any) -> bool:
    return bool(getattr(a, "sweep_detected", False)) or any(
        "sweep" in (f or "").lower()
        for f in (getattr(a, "confluence_factors", []) or [])
    )


def _has_smt(a: Any) -> bool:
    return getattr(a, "smt_score", 0) > 0 or bool(getattr(a, "has_smt", False))


def _has_ote(a: Any) -> bool:
    return getattr(a, "ote_score", 0) >= 6


def _has_kill_zone(a: Any) -> bool:
    return bool(getattr(a, "is_kill_zone", False))


def _has_displacement(a: Any) -> bool:
    return bool(getattr(a, "displacement_confirmed", False)) or _has_fvg(a)


def _has_cisd(a: Any) -> bool:
    return bool(getattr(a, "has_cisd", False)) or any(
        "cisd" in (f or "").lower()
        for f in (getattr(a, "confluence_factors", []) or [])
    )


def _in_distribution_phase(a: Any) -> bool:
    """PO3 distribution phase = NY session typically."""
    session = (getattr(a, "session_type", "") or "").lower()
    return "ny" in session or "overlap" in session


def _has_equal_levels(a: Any) -> bool:
    return any(
        (f or "").startswith("EQ_")
        for f in (getattr(a, "advanced_factors", []) or [])
    )


def _has_fib_extensions(a: Any) -> bool:
    return bool(getattr(a, "fib_tp_levels", None))


def _pd_aligned(a: Any) -> bool:
    return bool(getattr(a, "pd_aligned", False))


def _has_judas_swing(a: Any) -> bool:
    return bool(getattr(a, "has_judas_swing", False))


def _has_macro_time(a: Any) -> bool:
    return bool(getattr(a, "is_macro_time", False))


def _has_silver_bullet(a: Any) -> bool:
    return bool(getattr(a, "is_silver_bullet", False))


def _has_nwog(a: Any) -> bool:
    return getattr(a, "nwog_count", 0) > 0


def _has_ndog(a: Any) -> bool:
    return getattr(a, "ndog_count", 0) > 0


def _at_ipda_extreme(a: Any) -> bool:
    """Price at IPDA 20/40/60-day range extreme (>90% or <10%)."""
    ipda = getattr(a, "ipda_ranges", None)
    if not ipda:
        return False
    return any(r.get("pct", 50) > 90 or r.get("pct", 50) < 10 for r in ipda.values())


def _has_quarterly_shift(a: Any) -> bool:
    q = getattr(a, "quarterly_shift", None)
    return bool(q) and q.get("strength") == "confirmed"


def _quarterly_aligned(a: Any) -> bool:
    """Trade direction aligns with confirmed quarterly shift."""
    q = getattr(a, "quarterly_shift", None)
    if not q or q.get("strength") != "confirmed":
        return False
    return getattr(a, "direction", "") == q.get("direction", "")


def _quarterly_opposed(a: Any) -> bool:
    """Trade direction opposes confirmed quarterly shift."""
    q = getattr(a, "quarterly_shift", None)
    if not q or q.get("strength") != "confirmed":
        return False
    direction = getattr(a, "direction", "")
    shift_dir = q.get("direction", "")
    return direction and shift_dir and direction != shift_dir


def _mtf_aligned(a: Any) -> bool:
    return bool(getattr(a, "mtf_aligned", False))


# ICT institutional algorithm execution times (ET), stored as (hour, minute) tuples
_ALGO_TIMES: list[tuple[int, int]] = [
    (3, 0), (3, 15),          # London macro
    (9, 50), (10, 10),        # NY AM macro
    (10, 50),                 # NY mid-morning
    (13, 50), (14, 10),       # NY PM macro
    (14, 50),                 # NY close
]
_ALGO_WINDOW_MINUTES = 10


def _at_algo_time(a: Any) -> bool:
    """True when current ET time is within 10 minutes of an ICT algo execution time."""
    try:
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return False
    now_total = now_et.hour * 60 + now_et.minute
    return any(
        abs(now_total - (h * 60 + m)) <= _ALGO_WINDOW_MINUTES
        for h, m in _ALGO_TIMES
    )


def _has_cbdr(a: Any) -> bool:
    """CBDR data exists when cbdr_range is populated."""
    return getattr(a, "cbdr_range", None) is not None


def _cbdr_opposes(a: Any) -> bool:
    """True when the CBDR expansion direction opposes the trade direction.

    CBDR direction is determined by:
    1. An explicit 'direction' key on the cbdr_range dict ("BULLISH"/"BEARISH"), or
    2. Comparing the close (or open) price relative to the range midpoint — if the
       session close is above the midpoint the range expanded bullishly, below = bearish.
    """
    cbdr = getattr(a, "cbdr_range", None)
    if not cbdr:
        return False
    direction = (getattr(a, "direction", "") or "").upper()
    if not direction:
        return False
    try:
        # Prefer an explicit direction tag on the CBDR object
        cbdr_dir = (cbdr.get("direction") or "").upper()
        if cbdr_dir in ("BULLISH", "BEARISH"):
            cbdr_bullish = cbdr_dir == "BULLISH"
        else:
            # Fall back: compare close (or open) to midpoint of the range
            high = float(cbdr.get("high", 0))
            low = float(cbdr.get("low", 0))
            midpoint = (high + low) / 2.0
            ref = cbdr.get("close") or cbdr.get("open")
            if ref is None:
                return False
            cbdr_bullish = float(ref) > midpoint
        if cbdr_bullish and direction == "BEARISH":
            return True
        if not cbdr_bullish and direction == "BULLISH":
            return True
    except (TypeError, ZeroDivisionError, ValueError):
        return False
    return False


def _has_near_sweep(a: Any) -> bool:
    """True when a near-sweep (90%+ of liquidity level) is present."""
    return any(
        "minor_sweep_only" in (f or "").lower()
        for f in (getattr(a, "advanced_factors", []) or [])
    )


def _ob_at_high_volume(a: Any) -> bool:
    """Check if any active OB price range overlaps an HVN bucket.

    Uses real volume-profile data wired in 2026-04-26 (ict_pipeline.py step 8e7):
    `ob_zones` carries active OB (bottom, top) ranges from get_active_obs, and
    `vp_hvn_zones` carries High Volume Node (bottom, top) ranges derived from
    bucket midpoints +/- bucket_width/2.

    Two ranges overlap when bottom_a <= top_b AND top_a >= bottom_b. Any single
    OB-HVN overlap fires the +3 'OB+HVN' synergy — institutional defense
    confirmed by real bucketed volume, not the legacy FVG_stack/HiddenOB proxy.

    Falls back to False if either list is empty (e.g. profile not built yet on
    short df, or no active OBs).
    """
    if getattr(a, "ob_score", 0) < 10:
        return False
    ob_zones = getattr(a, "ob_zones", []) or []
    hvn_zones = getattr(a, "vp_hvn_zones", []) or []
    if not ob_zones or not hvn_zones:
        return False
    for ob_bot, ob_top in ob_zones:
        for hvn_bot, hvn_top in hvn_zones:
            if ob_bot <= hvn_top and ob_top >= hvn_bot:
                return True
    return False


def _multi_tf_crt(a: Any) -> bool:
    """
    Multi-TF CRT alignment: D1 AND H4 CRT both present in this cycle.

    The CRT card frames methodology as fractal — when daily and 4-hour
    candles BOTH show sweep+reversal in the same direction, the signal
    crosses the noise threshold of single-TF CRT. Backtest (1220 cycles
    across 5 symbols) showed this fires in ~7% of cycles — rare enough
    to be a genuine conviction premium, common enough to actually trigger.

    Implementation: substring match on lowercased advanced_factors so
    the predicate is independent of the CRTSetup dataclass shape.
    """
    factors = " ".join(getattr(a, "advanced_factors", []) or []).lower()
    return "crt_d1" in factors and "crt_h4" in factors


def _has_session_crt(a: Any) -> bool:
    """SessionCRT (Asian/London/NY fractal) factor present in this cycle."""
    factors = " ".join(getattr(a, "advanced_factors", []) or []).lower()
    return "crt_sessioncrt" in factors


def _in_ny_kill_zone(a: Any) -> bool:
    """
    True when we're inside an NY kill zone (NY AM 7-10 or London-Close
    10-12). Distinguishes from the broad _has_kill_zone helper which
    also fires for London KZ (2-5 NY) and Asian KZ.

    SessionCRT pairs specifically with NY kill zones because NY is the
    distribution leg of the fractal — the entry window after London's
    manipulation. London-KZ triggers would be premature.
    """
    if not bool(getattr(a, "is_kill_zone", False)):
        return False
    sess = (getattr(a, "session_type", "") or "").upper()
    return any(tag in sess for tag in ("NY_OPEN", "NY_AM", "NY_PM", "OVERLAP"))


def _has_micro_smt(a: Any) -> bool:
    """Check for micro SMT divergence (same-candle divergence on M15).

    Regular SMT checks for divergence over multiple bars.
    Micro SMT is when the divergence happens on a single candle —
    even more powerful confirmation.
    """
    # If regular SMT is confirmed AND we have displacement,
    # the divergence is likely tight/micro
    if not _has_smt(a):
        return False
    # Check if sweep happened recently (within last few bars) —
    # micro SMT occurs at the sweep point
    if not _has_sweep(a):
        return False
    # Both SMT + sweep at same time = micro-level confirmation
    return True


def _near_correlation_cap(a: Any) -> bool:
    """Detect when we're approaching the portfolio correlation cap.

    This is informational — tells Claude to reduce size rather than
    the hard block in risk_bridge. Checks confluence factors for
    correlation-related blocks from previous cycles.
    """
    factors = getattr(a, "confluence_factors", []) or []
    return any("Portfolio cap" in f or "correlation" in f.lower() for f in factors)


def _wyckoff_po3_aligned(a: Any) -> bool:
    """Wyckoff and PO3 phases agree on the current market phase.

    Wyckoff Spring = PO3 Manipulation (both = false move before real move)
    Wyckoff Markup/Markdown = PO3 Distribution (both = trending phase)
    """
    factors = getattr(a, "advanced_factors", []) or []
    factors_str = " ".join(factors).lower()

    # PO3 distribution + Market Maker Model = Wyckoff markup confirmed
    has_po3_dist = "po3_distribution" in factors_str
    has_mm_model = any("mm_" in f.lower() for f in factors)

    if has_po3_dist and has_mm_model:
        return True

    # PO3 manipulation + Judas swing = Wyckoff spring confirmed
    has_po3_manip = "po3_manipulation" in factors_str
    has_judas = any("judasswing" in f.lower() for f in factors)

    if has_po3_manip and has_judas:
        return True

    return False


# ---------------------------------------------------------------------------
# Synergy evaluation
# ---------------------------------------------------------------------------

# Map combination-name → (predicate fn, points, short-tag)
# Points come from cross_correlations.json's 'effect' field (parsed once).
_SYNERGY_CHECKS: list[tuple[str, Any, float, str]] = [
    (
        "OB + FVG overlap",
        lambda a: _has_ob(a) and _has_fvg(a),
        10.0,
        "OB+FVG_stack",
    ),
    (
        "Liquidity sweep + SMT divergence",
        lambda a: _has_sweep(a) and _has_smt(a),
        8.0,
        "Sweep+SMT",
    ),
    (
        "OTE zone inside OB/FVG stack (PD array stacking)",
        lambda a: _has_ote(a) and _has_ob(a) and _has_fvg(a),
        5.0,
        "OTE_in_PDstack",
    ),
    (
        "Kill zone + PO3 distribution phase",
        lambda a: _has_kill_zone(a) and _in_distribution_phase(a),
        6.0,
        "KZ+PO3dist",
    ),
    (
        "CISD + PO3 phase transition",
        lambda a: _has_cisd(a) and _in_distribution_phase(a),
        5.0,
        "CISD+PO3",
    ),
    (
        "Session level sweep + order pairing target",
        lambda a: _has_sweep(a) and _has_equal_levels(a),
        4.0,
        "Sweep+OrderPair",
    ),
    (
        "Fibonacci extension target at session level",
        lambda a: _has_fib_extensions(a) and _has_equal_levels(a),
        3.0,
        "Fib+EqLevel",
    ),
    # --- New synergies from conflict_resolution + ICT methodology ---
    (
        "Sweep + displacement + FVG (complete ICT chain)",
        lambda a: _has_sweep(a) and _has_displacement(a) and _has_fvg(a),
        7.0,
        "Sweep+Disp+FVG",
    ),
    (
        "Judas swing + kill zone (session manipulation confirmed)",
        lambda a: _has_judas_swing(a) and _has_kill_zone(a),
        6.0,
        "Judas+KZ",
    ),
    (
        "Silver bullet + FVG (time-specific entry with imbalance)",
        lambda a: _has_silver_bullet(a) and _has_fvg(a),
        5.0,
        "SB+FVG",
    ),
    (
        "NWOG + sweep (weekly gap fill with manipulation)",
        lambda a: _has_nwog(a) and _has_sweep(a),
        4.0,
        "NWOG+Sweep",
    ),
    (
        "IPDA extreme + sweep (macro reversal zone with trigger)",
        lambda a: _at_ipda_extreme(a) and _has_sweep(a),
        6.0,
        "IPDA_extreme+Sweep",
    ),
    (
        "Quarterly shift alignment (trade WITH confirmed macro direction)",
        lambda a: _quarterly_aligned(a),
        5.0,
        "QShift_aligned",
    ),
    (
        "Macro time + displacement (algorithmic spike with commitment)",
        lambda a: _has_macro_time(a) and _has_displacement(a),
        3.0,
        "Macro+Disp",
    ),
    (
        "Multi-timeframe alignment (W1+D1+H4 agree)",
        lambda a: _mtf_aligned(a),
        5.0,
        "MTF_aligned",
    ),
    (
        "Algo timing + displacement (institutional algorithm execution window)",
        lambda a: _at_algo_time(a) and _has_displacement(a),
        4.0,
        "AlgoTime+Disp",
    ),
    (
        "Near-sweep + displacement (partial liquidity grab with commitment)",
        lambda a: _has_near_sweep(a) and _has_displacement(a),
        2.0,
        "NearSweep+Disp",
    ),
    (
        "OB at high-volume zone (institutional footprint confirmed)",
        lambda a: _ob_at_high_volume(a),
        3.0,
        "OB+HVN",
    ),
    (
        "Micro SMT (SMT + sweep at same level — strongest divergence confirmation)",
        lambda a: _has_micro_smt(a),
        3.0,
        "MicroSMT",
    ),
    (
        "Wyckoff/PO3 alignment (institutional cycle phases confirmed)",
        lambda a: _wyckoff_po3_aligned(a),
        4.0,
        "Wyckoff+PO3",
    ),
    (
        "Multi-TF CRT (D1 + H4 sweep+reversal both fired — fractal alignment)",
        lambda a: _multi_tf_crt(a),
        5.0,
        "MultiTF_CRT",
    ),
    (
        "SessionCRT + NY kill zone (London-Asian sweep + NY distribution window)",
        lambda a: _has_session_crt(a) and _in_ny_kill_zone(a),
        4.0,
        "SessionCRT+KillZone",
    ),
]


# ---------------------------------------------------------------------------
# Gate evaluation — violations = penalty points or disqualification hints
# ---------------------------------------------------------------------------

_GATE_CHECKS: list[tuple[str, Any, float, str]] = [
    (
        "Premium/Discount gates OTE",
        lambda a: _has_ote(a) and not _pd_aligned(a),
        5.0,
        "OTE_in_wrong_zone",
    ),
    (
        "Displacement gates OB validity",
        lambda a: _has_ob(a) and not _has_displacement(a),
        4.0,
        "OB_without_displacement",
    ),
    (
        "Kill zone gates session score during accumulation",
        # Kill zone triggered during Asian session = wrong phase
        lambda a: _has_kill_zone(a) and "asian" in (getattr(a, "session_type", "") or "").lower(),
        3.0,
        "KZ_in_accumulation",
    ),
    (
        "Daily bias gates trade direction",
        # Trade opposing the HTF/daily bias has reduced conviction
        lambda a: (
            bool(getattr(a, "htf_analysis", None))
            and bool(getattr(a, "direction", ""))
            and getattr(getattr(a, "htf_analysis", None), "bias", "NEUTRAL") != "NEUTRAL"
            and (
                (getattr(getattr(a, "htf_analysis", None), "bias", "") == "BULLISH" and getattr(a, "direction", "") == "BEARISH")
                or (getattr(getattr(a, "htf_analysis", None), "bias", "") == "BEARISH" and getattr(a, "direction", "") == "BULLISH")
            )
        ),
        5.0,
        "direction_vs_htf_bias",
    ),
    # --- New gates from conflict_resolution + ICT methodology ---
    (
        "Quarterly shift opposes trade direction",
        # Confirmed quarterly shift against trade direction = major macro headwind
        lambda a: _quarterly_opposed(a),
        6.0,
        "counter_quarterly_shift",
    ),
    (
        "Sweep without structure change (no CHoCH/BOS)",
        # Sweep happened but structure didn't break = might be continuation, not reversal
        lambda a: _has_sweep(a) and not _has_displacement(a) and getattr(a, "structure_score", 0) < 10,
        3.0,
        "sweep_no_structure",
    ),
    (
        "FVG in wrong premium/discount zone",
        # FVG present but in wrong zone = lower probability fill
        lambda a: _has_fvg(a) and not _pd_aligned(a),
        3.0,
        "FVG_wrong_zone",
    ),
    (
        "HTF FVG obstacle (trading into opposing H4 imbalance)",
        lambda a: bool(getattr(a, "htf_fvg_obstacle", False)),
        5.0,
        "HTF_FVG_obstacle",
    ),
    (
        "Multi-timeframe conflict (W1/D1/H4 disagree)",
        lambda a: getattr(a, "mtf_alignment", "") and not getattr(a, "mtf_aligned", True) and "MTF_conflict" in (getattr(a, "advanced_factors", []) or []),
        4.0,
        "MTF_conflict",
    ),
    (
        "CBDR range opposes trade direction",
        lambda a: _cbdr_opposes(a),
        3.0,
        "CBDR_conflict",
    ),
    (
        "Intermarket conflict (DXY/VIX opposes trade direction)",
        lambda a: bool(getattr(a, "intermarket_conflict", False)),
        5.0,
        "Intermarket_conflict",
    ),
    (
        "HTF pullback active (H4 making consecutive closes against bias — retracement not complete)",
        lambda a: bool(getattr(a, "htf_pullback_active", False)),
        6.0,
        "HTF_pullback_active",
    ),
]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def evaluate_synergies(analysis: Any) -> ScoreAdjustment:
    """Evaluate all synergies and gates against the analysis.

    Returns a ScoreAdjustment whose delta can be applied to total_score.
    Also populates named_factors for logging and gate_violations that
    can be surfaced in the Claude prompt as explicit warnings.
    """
    cc_data = _load_cross_correlations()
    adj = ScoreAdjustment()

    # --- Synergies (bonuses) ---
    synergy_defs = {s["combination"]: s for s in cc_data.get("synergies", [])}

    for combo_name, predicate, points, tag in _SYNERGY_CHECKS:
        try:
            fires = predicate(analysis)
        except Exception:
            fires = False
        if fires:
            json_entry = synergy_defs.get(combo_name, {})
            json_bonus = json_entry.get("bonus_points")
            effective_points = float(json_bonus) if isinstance(json_bonus, (int, float)) else points
            adj.bonus_points += effective_points
            adj.named_factors.append(tag)
            effect = json_entry.get("effect", "")
            if effect:
                adj.explanations.append(f"SYNERGY: {combo_name} — {effect}")
            else:
                adj.explanations.append(f"SYNERGY: {combo_name} (+{effective_points:.0f})")

    # --- Gates (penalties + violations) ---
    gate_defs = {g["gate"]: g for g in cc_data.get("gates", [])}

    for gate_name, predicate, penalty, tag in _GATE_CHECKS:
        try:
            violated = predicate(analysis)
        except Exception:
            violated = False
        if violated:
            json_gate = gate_defs.get(gate_name, {})
            json_penalty = json_gate.get("bonus_points")
            effective_penalty = float(json_penalty) if isinstance(json_penalty, (int, float)) else penalty
            adj.penalty_points += effective_penalty
            adj.named_factors.append(f"!{tag}")
            rule = json_gate.get("rule", "")
            msg = f"GATE VIOLATED: {gate_name}" + (f" — {rule}" if rule else "")
            adj.gate_violations.append(msg)
            adj.explanations.append(msg)

    return adj


def format_adjustment_for_log(adj: ScoreAdjustment) -> str:
    """One-line summary for orchestrator logs."""
    if not adj.named_factors:
        return ""
    parts = []
    if adj.bonus_points:
        parts.append(f"+{adj.bonus_points:.1f}")
    if adj.penalty_points:
        parts.append(f"-{adj.penalty_points:.1f}")
    factors_str = " ".join(adj.named_factors)
    return f"Synergies: {factors_str} (net {' '.join(parts)})"
