"""
Claude Decision Layer — evaluates ICT scores and makes trade decisions.

Pre-gate filters save API cost by auto-skipping low-grade signals.
Post-gate validates R:R and position limits.
Fallback mode handles API unavailability.

Includes strategy knowledge context from:
- ChartFanatics (33 strategies organized into 4 archetypes)
- MT5 backtests (375K passes, per-symbol profiles)
- Session routing and mean reversion filters

Usage:
    from bridge.claude_decision import ClaudeDecisionMaker
    maker = ClaudeDecisionMaker()
    decision = maker.evaluate(symbol_analysis)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bridge.decision_types import TradeDecision
from bridge.ict_pipeline import SymbolAnalysis


# ---------------------------------------------------------------------------
# Strategy knowledge loader
# ---------------------------------------------------------------------------

_KNOWLEDGE_DIR = Path(__file__).parent / "strategy_knowledge"


def _load_json(name: str) -> dict:
    """Load a JSON file from strategy_knowledge/, returning {} on failure."""
    path = _KNOWLEDGE_DIR / name
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _load_rules_json() -> dict:
    """Load rules.json from project root."""
    path = Path(__file__).parent.parent / "rules.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _get_current_et_hour() -> tuple[int, int]:
    """Return (hour, minute) in US Eastern Time (handles DST automatically)."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    return now.hour, now.minute


def _get_session_name(et_hour: int) -> str:
    """Classify current ET hour into a session name."""
    if 3 <= et_hour < 7:
        return "london_open"
    elif 7 <= et_hour < 12:
        return "london_ny_overlap"
    elif 12 <= et_hour < 16:
        return "ny_afternoon"
    elif 16 <= et_hour < 19:
        return "ny_close"
    else:
        return "asian"


def _build_strategy_context(symbol: str, rules: dict) -> str:
    """Build strategy context string for Claude prompt from rules.json symbol_profiles."""
    profiles = rules.get("symbol_profiles", {})
    profile = profiles.get(symbol, {})
    if not profile:
        return ""

    lines = []
    lines.append(f"\nSTRATEGY CONTEXT ({symbol}):")
    lines.append(f"- Asset class: {profile.get('asset_class', 'unknown')}")
    lines.append(f"- Best sessions: {', '.join(profile.get('best_sessions', []))}")
    if profile.get("mt5_pf"):
        lines.append(f"- MT5 Backtest Profit Factor: {profile['mt5_pf']}")
    if profile.get("mt5_sharpe"):
        lines.append(f"- MT5 Backtest Sharpe: {profile['mt5_sharpe']}")
    lines.append(f"- Backtest confidence: {profile.get('backtest_confidence', 1.0)}x")
    strats = profile.get("primary_strategies", [])
    if strats:
        lines.append(f"- Recommended strategies: {', '.join(strats)}")
    if profile.get("smt_pair"):
        lines.append(f"- SMT pair: {profile['smt_pair']}")
    if profile.get("notes"):
        lines.append(f"- Notes: {profile['notes']}")

    # Risk overrides
    overrides = profile.get("risk_overrides", {})
    if overrides:
        lines.append(f"- Risk overrides: Grade A={overrides.get('grade_a', 0.01):.1%}, "
                      f"B={overrides.get('grade_b', 0.005):.1%}, "
                      f"C={overrides.get('grade_c', 0.0025):.1%}")

    return "\n".join(lines)


def _build_session_context(rules: dict) -> str:
    """Build current session context for Claude prompt."""
    et_hour, et_min = _get_current_et_hour()
    session = _get_session_name(et_hour)
    et_str = f"{et_hour:02d}:{et_min:02d}"

    lines = [f"\nSESSION CONTEXT:"]
    lines.append(f"- Current time: {et_str} ET | Session: {session}")

    # Check active kill zones
    time_filters = rules.get("time_filters", {})
    active_kz = []
    for kz in time_filters.get("kill_zone_windows", []):
        start_h, start_m = map(int, kz["et_start"].split(":"))
        end_h, end_m = map(int, kz["et_end"].split(":"))
        start_min = start_h * 60 + start_m
        end_min = end_h * 60 + end_m
        current_min = et_hour * 60 + et_min
        if start_min <= current_min <= end_min:
            active_kz.append(f"{kz['name']} ({kz.get('description', '')})")

    if active_kz:
        lines.append(f"- ACTIVE KILL ZONES: {'; '.join(active_kz)}")
    else:
        lines.append("- No active kill zones")

    # Check no-trade windows
    for ntw in time_filters.get("no_trade_windows", []):
        if "et_start" in ntw:
            start_h, start_m = map(int, ntw["et_start"].split(":"))
            end_h, end_m = map(int, ntw["et_end"].split(":"))
            start_min = start_h * 60 + start_m
            end_min = end_h * 60 + end_m
            current_min = et_hour * 60 + et_min
            if start_min <= current_min <= end_min:
                lines.append(f"- WARNING: In no-trade window '{ntw['name']}': {ntw['reason']}")

    # Session confidence multiplier
    multipliers = time_filters.get("session_confidence_multipliers", {})
    mult = multipliers.get(session, 1.0)
    if mult != 1.0:
        lines.append(f"- Session confidence multiplier: {mult}x")

    return "\n".join(lines)


def _load_ict_concept(name: str) -> dict:
    """Load a single ICT concept from ict_concepts/<name>.json."""
    return _load_json(f"ict_concepts/{name}.json")


def _build_ict_context(a: "SymbolAnalysis") -> str:
    """Build ICT teachings context relevant to the current signal.

    Loads individual concept files from ict_concepts/ directory and
    uses the dependency graph and cross-connections to provide
    layered, connected guidance.
    """
    # Load only the concepts and sections we need (not the full monolith)
    index = _load_json("ict_concepts/_index.json")
    if not index:
        # Fallback to monolith if individual files don't exist
        teachings = _load_json("ict_core_teachings.json")
        if not teachings:
            return ""
        concepts_data = teachings.get("core_concepts", {})
    else:
        concepts_data = None  # We'll load individual concepts on demand

    lines = ["\nICT FRAMEWORK GUIDANCE (concept chain):"]

    # Helper to get a concept either from monolith or individual file
    def _get_concept(name: str) -> dict:
        if concepts_data is not None:
            return concepts_data.get(name, {})
        return _load_ict_concept(name)

    # 1. Layer 1 — Foundation: Structure + Dealing Range + Liquidity
    if a.structure_score < 15:
        lines.append("- LAYER 1 FAIL: HTF structure unclear. Without structure, dealing_range and premium_discount cannot be determined. Reduce to Grade C max.")
    else:
        lines.append(f"- LAYER 1 OK: Structure {a.structure_score:.0f}/30. Dealing range and liquidity levels established.")

    # 2. Layer 2 — Context: Premium/Discount + Session/PO3 phase
    pd = _get_concept("premium_discount")
    if pd:
        rules = pd.get("rules", {})
        if a.direction == "BULLISH":
            lines.append(f"- LAYER 2 ZONE: {rules.get('buy_in_discount', 'Longs ONLY below 50% — buying below fair value.')}")
        elif a.direction == "BEARISH":
            lines.append(f"- LAYER 2 ZONE: {rules.get('sell_in_premium', 'Shorts ONLY above 50% — selling above fair value.')}")

    # PO3 phase from session (connected to kill zones)
    po3 = _get_concept("power_of_three_and_AMD")
    if po3:
        session = getattr(a, "session_type", "")
        phases = po3.get("phases", {})
        if "asian" in session.lower():
            acc = phases.get("accumulation", {})
            lines.append(f"- LAYER 2 PO3: ACCUMULATION. {acc.get('trading_rule', 'DO NOT TRADE.')} (feeds into: liquidity builds for later sweep)")
        elif "london" in session.lower() and "ny" not in session.lower():
            manip = phases.get("manipulation", {})
            lines.append(f"- LAYER 2 PO3: MANIPULATION. {manip.get('trading_rule', 'DO NOT CHASE.')} → wait for displacement to confirm distribution start")
        elif "overlap" in session.lower() or "ny" in session.lower():
            dist = phases.get("distribution", {})
            lines.append(f"- LAYER 2 PO3: DISTRIBUTION. {dist.get('trading_rule', 'Enter retracements after manipulation completes.')}")

    # 3. Layer 3 — Events: Sweep + Displacement + SMT
    confluence = a.confluence_factors or []
    confluence_str = " ".join(confluence).lower()

    layer3_issues = []
    has_sweep = "sweep" in confluence_str or "liquidity" in confluence_str
    has_displacement = a.fvg_score >= 9  # FVG is evidence of displacement
    has_smt = a.smt_score > 0

    if not has_sweep:
        sweep_info = _get_concept("liquidity_sweep")
        layer3_issues.append(f"NO SWEEP: {sweep_info.get('chain_after_sweep', 'Without sweep, manipulation may not be complete. Wait.')}")
    if not has_displacement:
        disp_info = _get_concept("displacement")
        layer3_issues.append(f"NO DISPLACEMENT: {disp_info.get('critical_chain', 'Without displacement, FVG/OB/OTE are all unproven.')}")
    if not has_smt:
        smt_info = _get_concept("SMT_divergence")
        layer3_issues.append(f"NO SMT: {smt_info.get('role_in_chain', 'Acceptable but reduces conviction.')}")

    if layer3_issues:
        lines.append("- LAYER 3 GAPS:")
        for issue in layer3_issues:
            lines.append(f"  * {issue}")
    else:
        lines.append("- LAYER 3 OK: Sweep + Displacement + SMT all confirmed.")

    # 4. Layer 4-5 — Entry precision: OB + FVG + OTE
    layer45_notes = []
    if a.ob_score >= 10 and a.fvg_score >= 9:
        layer45_notes.append("OB + FVG stacked — highest probability entry zone")
    elif a.ob_score >= 10:
        layer45_notes.append("OB present but weak FVG — displacement may not have left clear imbalance")
    elif a.fvg_score >= 9:
        layer45_notes.append("FVG present but weak OB — entry zone is less precise, widen SL")

    if a.ote_score >= 6:
        layer45_notes.append("OTE reached — entry at 0.618-0.786 Fibonacci within zone")
    elif a.ote_score > 0:
        layer45_notes.append("Partial OTE — entry near but not optimal. Still acceptable.")
    else:
        layer45_notes.append("NOT AT OTE — entry not at retracement level. R:R suboptimal.")

    if layer45_notes:
        lines.append(f"- LAYER 4-5 ENTRY: {' | '.join(layer45_notes)}")

    # 5. Conflict resolution — check for concept contradictions
    conflict_data = _load_json("ict_concepts/conflict_resolution.json")
    conflicts = conflict_data.get("rules", [])
    active_conflicts = []

    # Check: strong FVG but wrong PO3 phase (chasing)
    if has_displacement and "asian" in getattr(a, "session_type", "").lower():
        active_conflicts.append("Displacement in accumulation phase — distribution hasn't started. Wait for kill zone.")

    # Check: OB present but no displacement
    if a.ob_score >= 10 and not has_displacement:
        active_conflicts.append("OB without displacement = unproven zone. Wait for displacement to validate.")

    if active_conflicts:
        lines.append("- CONFLICTS DETECTED:")
        for c in active_conflicts:
            lines.append(f"  * {c}")

    # 6. SL placement from risk management
    risk_data = _load_json("ict_concepts/risk_management.json")
    sl_info = risk_data.get("stop_loss", {})
    if sl_info:
        lines.append(f"- SL: {sl_info.get('connection', 'Anchored to liquidity_sweep wick. If revisited, thesis is wrong.')}")

    return "\n".join(lines)


def _build_mean_reversion_warning(a: "SymbolAnalysis", rules: dict) -> str:
    """Build mean reversion warning if applicable."""
    mr = rules.get("mean_reversion_thresholds", {})
    if not mr:
        return ""

    # We don't have actual SD data in SymbolAnalysis yet, but we can flag
    # the concept for Claude to consider based on available signals
    lines = []
    lines.append("\nMEAN REVERSION CHECK:")
    lines.append(f"- If price is >{mr.get('warning_sd', 2.0)} SD from {mr.get('ema_period', 20)} EMA "
                  f"with {mr.get('consecutive_candles', 3)}+ same-direction candles:")
    lines.append(f"  -> REDUCE continuation confidence by {abs(mr.get('effect_on_continuation', -10))} points")
    lines.append(f"  -> BOOST reversal confidence by {mr.get('effect_on_reversal', 10)} points")
    lines.append(f"- If price is >{mr.get('extreme_sd', 3.0)} SD: flag as 'extreme -- reversal setups only'")
    lines.append("- Assess whether current price action shows signs of extended displacement from mean.")

    return "\n".join(lines)


def _classify_trade_type(a: "SymbolAnalysis") -> str:
    """
    Classify trade as 'swing' or 'intraday' based on HTF alignment.

    Swing: HTF H4 bias matches direction AND we're at a HTF structure level.
    Intraday: Everything else — tighter SL behind OB/FVG entry zone.
    """
    if a.htf_analysis is None:
        return "intraday"
    htf_bias = a.htf_analysis.bias  # e.g. "BULLISH", "BEARISH", "NEUTRAL"
    if htf_bias == "NEUTRAL":
        return "intraday"
    # If HTF bias matches signal direction → swing trade
    if (htf_bias == "BULLISH" and a.direction == "BULLISH") or \
       (htf_bias == "BEARISH" and a.direction == "BEARISH"):
        return "swing"
    return "intraday"


# ---------------------------------------------------------------------------
# Claude API wrapper (lazy import to avoid hard dependency)
# ---------------------------------------------------------------------------

def _get_anthropic_client():
    """Lazy-load Anthropic client."""
    try:
        from anthropic import Anthropic
        return Anthropic()
    except ImportError:
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Decision Maker
# ---------------------------------------------------------------------------

class ClaudeDecisionMaker:
    """
    Makes trade decisions from ICT analysis using Claude.

    Grade routing:
    - Grade A  -> Claude Sonnet (deeper analysis)
    - Grade B  -> Claude Haiku (fast, cost-efficient)
    - Grade C in kill zone -> Claude Haiku
    - Grade C outside kill zone -> auto-SKIP
    - Grade D / INVALID -> auto-SKIP (no API call)
    """

    SONNET = "claude-sonnet-4-6"   # Grade A trades — deeper analysis
    HAIKU = "claude-haiku-4-5-20251001"    # Grade B/C trades — fast, cost-efficient

    def __init__(self, min_rr: float = 1.5):
        self.min_rr = min_rr
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = _get_anthropic_client()
        return self._client

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def evaluate(self, analysis: SymbolAnalysis) -> TradeDecision:
        """
        Evaluate an ICT analysis and return a trade decision.

        Args:
            analysis: Output from ICTPipeline.analyze_symbol()

        Returns:
            TradeDecision with action, entry, SL, TP, confidence, reasoning.
        """
        # Pre-gate: skip low-quality signals
        skip_reason = self._pre_gate(analysis)
        if skip_reason:
            return TradeDecision(
                action="SKIP",
                symbol=analysis.symbol,
                entry_price=analysis.current_price,
                reasoning=skip_reason,
                grade=analysis.grade,
                ict_score=analysis.total_score,
                model_used="pre-gate",
            )

        # Route to appropriate model
        model = self.SONNET if analysis.grade == "A" else self.HAIKU

        # Try Claude API (with one retry on transient errors)
        if self.client:
            import time as _time
            for attempt in range(2):
                try:
                    decision = self._call_claude(analysis, model)
                    # Post-gate
                    rejection = self._post_gate(decision, analysis)
                    if rejection:
                        decision.action = "SKIP"
                        decision.reasoning = f"Post-gate: {rejection}. Original: {decision.reasoning}"
                    return decision
                except Exception as e:
                    print(f"  [CLAUDE] API error for {analysis.symbol} (attempt {attempt+1}): {type(e).__name__}: {e}", flush=True)
                    if attempt == 0:
                        _time.sleep(2)  # brief pause before retry
                # Fall through to rule-based after 2 attempts

        # Fallback: rule-based decision
        return self._rule_based_decision(analysis)

    # ------------------------------------------------------------------
    # Pre-gate
    # ------------------------------------------------------------------

    def _pre_gate(self, analysis: SymbolAnalysis) -> str | None:
        """
        Filter out signals that don't warrant a Claude API call.
        Returns skip reason or None if signal should proceed.
        """
        if analysis.error:
            return f"Analysis error: {analysis.error}"

        if analysis.grade in ("D", "INVALID"):
            return f"Grade {analysis.grade} ({analysis.total_score:.0f}/100) below minimum"

        if analysis.grade == "C" and not analysis.is_kill_zone:
            return f"Grade C outside kill zone (session: {analysis.session_type})"

        # Grade B outside kill zone during low-conviction sessions
        if analysis.grade == "B" and not analysis.is_kill_zone:
            session = getattr(analysis, "session_type", "").lower()
            if session in ("asian", "ny_close", "ny_afternoon"):
                return f"Grade B in low-conviction session ({session}) without kill zone"

        if analysis.current_price <= 0:
            return "Invalid price data"

        return None

    # ------------------------------------------------------------------
    # Claude API call
    # ------------------------------------------------------------------

    def _call_claude(self, analysis: SymbolAnalysis, model: str) -> TradeDecision:
        """Call Claude to evaluate the signal and return a trade decision."""
        prompt = self._build_prompt(analysis)

        response = self.client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        return self._parse_response(text, analysis, model)

    def _build_prompt(self, a: SymbolAnalysis) -> str:
        """Build a concise prompt for Claude trade evaluation with strategy knowledge context."""
        direction_action = "BUY" if a.direction == "BULLISH" else "SELL" if a.direction == "BEARISH" else "SKIP"

        # Check for EA ensemble signal in confluence factors
        ea_line = ""
        for factor in (a.confluence_factors or []):
            if factor.startswith("EA_ensemble"):
                ea_line = f"\n- EA Strategy Ensemble: CONFIRMS {a.direction} ({factor})"
                break

        # Load strategy knowledge context
        rules = _load_rules_json()
        strategy_ctx = _build_strategy_context(a.symbol, rules)
        session_ctx = _build_session_context(rules)
        mr_warning = _build_mean_reversion_warning(a, rules)
        ict_ctx = _build_ict_context(a)

        # Get per-symbol risk override
        profile = rules.get("symbol_profiles", {}).get(a.symbol, {})
        risk_overrides = profile.get("risk_overrides", {})
        grade_key = f"grade_{a.grade.lower()}" if a.grade in ("A", "B", "C") else "grade_c"
        max_risk = risk_overrides.get(grade_key, 0.01)

        trade_type = _classify_trade_type(a)

        atr_line = f"\n- ATR(14) on M15: {a.atr_m15:.5f}" if a.atr_m15 > 0 else ""
        min_sl_dist = a.atr_m15 * 2.0 if a.atr_m15 > 0 else 0

        return f"""You are an ICT trading decision engine trained in ICT methodology (market structure, PO3, AMD, liquidity engineering, premium/discount, OTE, displacement, FVGs, order blocks, SMT divergence). Enhanced with 33 ChartFanatics strategies and 375K MT5 backtest passes. Evaluate this signal using ICT principles and respond with ONLY a JSON object.

SIGNAL:
- Symbol: {a.symbol} @ ${a.current_price:,.2f}
- Direction: {a.direction} | Grade: {a.grade} ({a.total_score:.0f}/100)
- Confluence: {', '.join(a.confluence_factors) if a.confluence_factors else 'None'}
- Session: {a.session_type} | Kill Zone: {a.is_kill_zone} | Silver Bullet: {a.is_silver_bullet}
- P/D Zone: {a.pd_zone or 'unknown'} | Aligned: {a.pd_aligned}
- Displacement: {'CONFIRMED (sweep + FVG reversal)' if a.displacement_confirmed else 'NOT CONFIRMED'}{ea_line}{atr_line}

SCORE BREAKDOWN:
- Structure: {a.structure_score:.0f}/30 | Liquidity sweep: REQUIRED GATE (not scored)
- Order Block: {a.ob_score:.0f}/20 | FVG: {a.fvg_score:.0f}/18
- Session: {a.session_score:.0f}/12 | OTE: {a.ote_score:.0f}/12 | SMT: {a.smt_score:.0f}/8
{strategy_ctx}
{session_ctx}
{ict_ctx}
{mr_warning}

RULES:
- Minimum R:R 1.5:1 (hard gate — trades below this are auto-rejected). Prefer 2:1+ but 1.5:1 is acceptable with strong confluence.
- Grade A: full conviction | Grade B: strict risk | Grade C: pullback only
- SL behind nearest structure level/OB, but NEVER closer than 2x ATR(14) from entry{f' (minimum {min_sl_dist:.5f} distance)' if min_sl_dist > 0 else ''}
- CRITICAL: Tight stops get hunted — our data shows sweeps regularly exceed 2x ATR. Place SL BEYOND the liquidity sweep wick, not at the edge. For crypto, minimum 0.5% of price distance. The trade that got stopped at 9pt on ETH would have made +30pts with a 12pt SL.
- TP at next liquidity target
- TP1 (tp_price): nearest HTF FVG or Order Block in trade direction (partial close at 50%)
- TP2 (tp2_price): next liquidity pool / swing high/low (final target, trail remainder)
- If only one TP level is clear, set tp2_price = tp_price * 1.5 (for BUY) or * 0.667 (for SELL) as fallback
- Max risk for this symbol/grade: {max_risk:.1%}
- Trade type: {trade_type.upper()} → SL placement: {'beyond the swept high/low (give buffer beyond the wick)' if trade_type == 'swing' else 'behind the OB/FVG entry zone (at least 1.5x ATR from entry)'}
- CRITICAL ZONE CHECK: BUY in premium or SELL in discount = WRONG ZONE. Auto-downgrade by 1 grade or SKIP unless strong confluence overrides.
- EA ensemble confirmation adds conviction — treat as extra confluence factor
- If in a kill zone that matches this symbol's best session: boost confidence
- If mean reversion warning applies: reduce continuation confidence, prefer reversal setups
- If session confidence multiplier < 1.0: reduce position size proportionally

Respond ONLY with this JSON (no markdown, no explanation):
{{"action":"{direction_action}","entry_price":<float>,"sl_price":<float>,"tp_price":<float>,"tp2_price":<float>,"confidence":<0-100>,"risk_pct":<decimal e.g. 0.01 means 1%>,"reasoning":"<1 sentence>"}}

risk_pct MUST be a decimal fraction: 0.01 = 1%, 0.005 = 0.5%, 0.0025 = 0.25%. Max allowed: {max_risk:.4f}
If the setup is not convincing, use "SKIP" for action."""

    def _parse_response(self, text: str, analysis: SymbolAnalysis, model: str) -> TradeDecision:
        """Parse Claude's JSON response into a TradeDecision."""
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON in the response
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start:end])
                except json.JSONDecodeError:
                    return TradeDecision(
                        action="SKIP",
                        symbol=analysis.symbol,
                        entry_price=analysis.current_price,
                        reasoning=f"Failed to parse Claude response: {text[:100]}",
                        grade=analysis.grade,
                        ict_score=analysis.total_score,
                        model_used=model,
                    )
            else:
                return TradeDecision(
                    action="SKIP",
                    symbol=analysis.symbol,
                    entry_price=analysis.current_price,
                    reasoning=f"No JSON in Claude response: {text[:100]}",
                    grade=analysis.grade,
                    ict_score=analysis.total_score,
                    model_used=model,
                )

        # Guard against null values in Claude's JSON (e.g. "entry_price": null)
        def _f(key: str, default: float) -> float:
            v = data.get(key)
            return float(v) if v is not None else float(default)

        return TradeDecision(
            action=(data.get("action") or "SKIP").upper(),
            symbol=analysis.symbol,
            entry_price=_f("entry_price", analysis.current_price),
            sl_price=_f("sl_price", 0),
            tp_price=_f("tp_price", 0),
            tp2_price=_f("tp2_price", 0),
            confidence=int(data.get("confidence") or 0),
            risk_pct=_f("risk_pct", 0.005),
            reasoning=data.get("reasoning") or "No reasoning provided",
            grade=analysis.grade,
            ict_score=analysis.total_score,
            model_used=model,
            trade_type=_classify_trade_type(analysis),
        )

    # ------------------------------------------------------------------
    # Post-gate
    # ------------------------------------------------------------------

    def _post_gate(self, decision: TradeDecision, analysis: SymbolAnalysis | None = None) -> str | None:
        """
        Validate a trade decision after Claude responds.
        Returns rejection reason or None if valid.
        """
        if not decision.is_trade:
            return None

        # Check R:R
        rr = decision.risk_reward_ratio
        if rr > 0 and rr < self.min_rr:
            return f"R:R {rr:.1f} below minimum {self.min_rr}"

        # SL must be on correct side
        if decision.action == "BUY":
            if decision.sl_price >= decision.entry_price:
                return "SL above entry for BUY"
            if decision.tp_price <= decision.entry_price:
                return "TP below entry for BUY"
        elif decision.action == "SELL":
            if decision.sl_price <= decision.entry_price:
                return "SL below entry for SELL"
            if decision.tp_price >= decision.entry_price:
                return "TP above entry for SELL"

        # Risk % sanity
        if decision.risk_pct > 0.02:
            return f"Risk {decision.risk_pct:.1%} exceeds 2% max"

        # Minimum SL distance: must be at least 2x ATR(14) on M15
        # AND at least 0.5% of price for crypto (sweeps go deep), 0.2% for metals
        # Prevents premature stopouts from liquidity sweeps
        if analysis and analysis.atr_m15 > 0:
            sl_distance = abs(decision.entry_price - decision.sl_price)
            min_sl_atr = analysis.atr_m15 * 2.0  # 2x ATR minimum (sweeps exceed 1.5x)

            # Per-asset-class minimum as % of price
            base_sym = analysis.symbol.split(":")[-1]
            if base_sym in ("BTCUSD", "ETHUSD", "SOLUSD", "DOGEUSD"):
                min_sl_pct = decision.entry_price * 0.005  # 0.5% for crypto (sweeps go deep)
            elif base_sym in ("XAUUSD", "XAGUSD"):
                min_sl_pct = decision.entry_price * 0.003  # 0.3% for metals
            else:
                min_sl_pct = decision.entry_price * 0.001  # 0.1% for forex/indices

            min_sl = max(min_sl_atr, min_sl_pct)

            if sl_distance < min_sl:
                return (
                    f"SL too tight: {sl_distance:.5f} < min({min_sl:.5f}). "
                    f"ATR={analysis.atr_m15:.5f}, min_atr={min_sl_atr:.5f}, min_pct={min_sl_pct:.5f}"
                )

        return None

    # ------------------------------------------------------------------
    # Fallback rule-based
    # ------------------------------------------------------------------

    def _rule_based_decision(self, analysis: SymbolAnalysis) -> TradeDecision:
        """
        Fallback when Claude API is unavailable.
        Only enters on Grade A + kill zone + clear direction.
        """
        if analysis.grade != "A" or not analysis.is_kill_zone:
            return TradeDecision(
                action="SKIP",
                symbol=analysis.symbol,
                entry_price=analysis.current_price,
                reasoning="Fallback mode: only Grade A + kill zone (API unavailable)",
                grade=analysis.grade,
                ict_score=analysis.total_score,
                model_used="rule-based-fallback",
            )

        # SL/TP calculation: use real ATR if available, else 0.5% fallback
        price = analysis.current_price
        if analysis.atr_m15 > 0:
            risk_dist = analysis.atr_m15 * 1.5  # 1.5x ATR for breathing room
        else:
            risk_dist = price * 0.005

        if analysis.direction == "BULLISH":
            action = "BUY"
            sl = price - risk_dist
            tp = price + risk_dist * 2  # 2R target
        elif analysis.direction == "BEARISH":
            action = "SELL"
            sl = price + risk_dist
            tp = price - risk_dist * 2
        else:
            return TradeDecision(
                action="SKIP",
                symbol=analysis.symbol,
                entry_price=price,
                reasoning="Fallback: neutral direction, no trade",
                grade=analysis.grade,
                ict_score=analysis.total_score,
                model_used="rule-based-fallback",
            )

        return TradeDecision(
            action=action,
            symbol=analysis.symbol,
            entry_price=price,
            sl_price=round(sl, 5),
            tp_price=round(tp, 5),
            confidence=70,
            risk_pct=0.01,
            reasoning=f"Fallback: Grade A + {analysis.session_type} kill zone, {len(analysis.confluence_factors)} confluence factors",
            grade=analysis.grade,
            ict_score=analysis.total_score,
            model_used="rule-based-fallback",
        )


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from bridge.ict_pipeline import SymbolAnalysis

    # Simulate a Grade B analysis
    test = SymbolAnalysis(
        symbol="BTCUSD",
        current_price=69000.0,
        total_score=76.8,
        grade="B",
        direction="BEARISH",
        confidence=0.77,
        confluence_factors=["CHoCH", "BOS", "Liquidity Sweep", "DOL", "OB", "FVG"],
        structure_score=25.0,
        liquidity_score=20.0,
        ob_score=12.8,
        fvg_score=15.0,
        session_score=0.0,
        ote_score=4.0,
        smt_score=0.0,
        session_type="ASIAN",
        is_kill_zone=False,
        is_silver_bullet=False,
    )

    maker = ClaudeDecisionMaker()
    decision = maker.evaluate(test)
    print(json.dumps(decision.to_dict(), indent=2))
