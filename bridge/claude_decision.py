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

# In-memory cache for JSON files (loaded once, reused across calls)
_json_cache: dict[str, dict] = {}


def _load_json(name: str) -> dict:
    """Load a JSON file from strategy_knowledge/, cached in memory."""
    if name in _json_cache:
        return _json_cache[name]
    path = _KNOWLEDGE_DIR / name
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _json_cache[name] = data
            return data
        except Exception:
            return {}
    return {}


def _load_rules_json() -> dict:
    """Load rules.json from project root, cached in memory."""
    cache_key = "__rules_json__"
    if cache_key in _json_cache:
        return _json_cache[cache_key]
    path = Path(__file__).parent.parent / "rules.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _json_cache[cache_key] = data
            return data
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
        if start_min > end_min:  # crosses midnight
            in_window = current_min >= start_min or current_min <= end_min
        else:
            in_window = start_min <= current_min <= end_min
        if in_window:
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
            if start_min > end_min:  # crosses midnight
                in_window = current_min >= start_min or current_min <= end_min
            else:
                in_window = start_min <= current_min <= end_min
            if in_window:
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
        lines.append(f"- LAYER 1: Structure {a.structure_score:.0f}/30 (weak — use wider SL for uncertainty). Dealing range may be ambiguous.")
    else:
        lines.append(f"- LAYER 1: Structure {a.structure_score:.0f}/30 (confirmed). Dealing range and liquidity levels established.")

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

    has_sweep = "sweep" in confluence_str or "liquidity" in confluence_str
    has_displacement = a.fvg_score >= 9  # FVG is evidence of displacement
    has_smt = a.smt_score > 0

    layer3_present = []
    layer3_absent = []
    if has_sweep:
        layer3_present.append("Liquidity sweep confirmed")
    else:
        layer3_absent.append("Sweep not detected in window (reduces conviction but not disqualifying)")
    if has_displacement:
        layer3_present.append("Displacement confirmed via FVG")
    else:
        layer3_absent.append("Displacement unconfirmed (use structure + OB for entry instead)")
    if has_smt:
        layer3_present.append("SMT divergence confirmed")
    else:
        layer3_absent.append("SMT not detected (optional confluence — trade is still valid without it)")

    lines.append(f"- LAYER 3 EVENTS: {len(layer3_present)}/3 confirmed")
    for item in layer3_present:
        lines.append(f"  + {item}")
    for item in layer3_absent:
        lines.append(f"  ~ {item}")

    # 4. Layer 4-5 — Entry precision: OB + FVG + OTE
    layer45_notes = []
    if a.ob_score >= 10 and a.fvg_score >= 9:
        layer45_notes.append("OB + FVG stacked — highest probability entry zone")
    elif a.ob_score >= 10:
        layer45_notes.append("OB present but weak FVG — displacement may not have left clear imbalance")
    elif a.fvg_score >= 9:
        layer45_notes.append("FVG present but weak OB — entry zone is less precise, widen SL")

    if a.ote_score >= 6:
        layer45_notes.append("OTE reached — entry at 0.618-0.786 Fibonacci retracement (optimal)")
    elif a.ote_score > 0:
        layer45_notes.append("Partial OTE — near retracement level (acceptable entry)")
    else:
        layer45_notes.append("OTE not at retracement — use OB/FVG zone for entry instead (acceptable)")

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
    # TODO: Calculate actual SD from OHLCV data before enabling
    # Currently returns speculative warnings without data — disabled
    return ""


def _classify_trade_type(a: "SymbolAnalysis") -> str:
    """
    Classify trade as 'scalp', 'intraday', or 'swing' using multiple signals.

    Scalp  (8h timeout):  Silver Bullet or kill-zone-only entry, no HTF alignment
    Intraday (48h timeout): Standard ICT setup within a session
    Swing (168h/1wk timeout): HTF H4 bias aligned + strong structure + displacement

    The classification drives:
    - Position age timeout (stale losers only — winners/tp1_hit exempt)
    - SL placement guidance in the Claude prompt
    """
    htf_aligned = False
    if a.htf_analysis is not None:
        htf_bias = a.htf_analysis.bias
        if htf_bias != "NEUTRAL":
            htf_aligned = (
                (htf_bias == "BULLISH" and a.direction == "BULLISH") or
                (htf_bias == "BEARISH" and a.direction == "BEARISH")
            )

    # --- Swing: HTF aligned + strong confluence ---
    # Needs H4 bias match AND at least two of: displacement, OB, structure >=20
    if htf_aligned:
        swing_signals = sum([
            a.displacement_confirmed,
            a.ob_score >= 12,
            a.structure_score >= 20,
            a.sweep_detected,
            a.has_cisd,
        ])
        if swing_signals >= 2:
            return "swing"

    # --- Scalp: Silver Bullet or pure kill-zone play with no HTF backing ---
    if a.is_silver_bullet and not htf_aligned:
        return "scalp"

    # --- Default: intraday ---
    return "intraday"


# ---------------------------------------------------------------------------
# Claude API wrapper (lazy import to avoid hard dependency)
# ---------------------------------------------------------------------------

def _get_anthropic_client():
    """Lazy-load Anthropic client.

    Reads ANTHROPIC_API_KEY from os.environ at call time and passes it
    explicitly so the client never binds to a stale/empty value captured
    during module import.
    """
    import os as _os
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    key = _os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        return Anthropic(api_key=key)
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

    def __init__(self, min_rr: float = 1.25):
        self.min_rr = min_rr
        self._client = None
        # Decision cache: avoid redundant API calls when signal hasn't changed
        # Key: symbol, Value: (grade, direction, score_bucket, timestamp, decision)
        self._decision_cache: dict[str, tuple[str, str, int, float, TradeDecision]] = {}
        self._cache_ttl = 540  # 9 minutes — covers ~2 cycles at 300s interval

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

        # Route: Sonnet for Grade A (deeper analysis), Haiku for Grade B (cost-efficient)
        model = self.SONNET if analysis.grade == "A" else self.HAIKU

        # Decision cache: reuse last decision if signal hasn't materially changed
        # Includes price bucket so stale entries at different prices don't replay
        import time as _time
        score_bucket = int(analysis.total_score // 5) * 5  # bucket by 5-pt bands
        # Price bucket: 0.2% bands — cache invalidates when price moves significantly
        price_bucket = int(analysis.current_price / (analysis.current_price * 0.002)) if analysis.current_price > 0 else 0
        cache_key = analysis.symbol
        cached = self._decision_cache.get(cache_key)
        if cached:
            c_grade, c_dir, c_bucket, c_ts, c_decision, c_price_bucket = cached
            age = _time.time() - c_ts
            if (age < self._cache_ttl
                    and c_grade == analysis.grade
                    and c_dir == analysis.direction
                    and c_bucket == score_bucket
                    and c_price_bucket == price_bucket):
                print(f"  [{analysis.symbol}] Cache hit ({age:.0f}s old) — reusing last decision", flush=True)
                return c_decision

        # Try Claude API (with one retry on transient errors)
        if self.client:
            for attempt in range(2):
                try:
                    decision = self._call_claude(analysis, model)
                    # Post-gate
                    rejection = self._post_gate(decision, analysis)
                    if rejection:
                        decision.action = "SKIP"
                        decision.reasoning = f"Post-gate: {rejection}. Original: {decision.reasoning}"
                    # Cache the decision for reuse
                    self._decision_cache[cache_key] = (
                        analysis.grade, analysis.direction, score_bucket,
                        _time.time(), decision, price_bucket,
                    )
                    return decision
                except Exception as e:
                    err_name = type(e).__name__
                    err_msg = str(e)
                    print(f"  [CLAUDE] API error for {analysis.symbol} (attempt {attempt+1}): {err_name}: {err_msg}", flush=True)
                    # Auth errors (401) will never recover mid-process — abort
                    # immediately so the :loop wrapper restarts us with a fresh
                    # .env load rather than silently flooding with 401s.
                    is_auth = (
                        err_name in ("AuthenticationError", "PermissionDeniedError")
                        or "401" in err_msg
                        or "authentication" in err_msg.lower()
                    )
                    if is_auth:
                        import sys as _sys
                        print(
                            "  [CLAUDE] Authentication failed — ANTHROPIC_API_KEY rejected. "
                            "Exiting so the wrapper can restart with a fresh .env load.",
                            flush=True,
                        )
                        _sys.exit(3)
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

        if analysis.grade in ("C", "D", "INVALID"):
            return f"Grade {analysis.grade} ({analysis.total_score:.0f}/100) below minimum for API call"

        if analysis.current_price <= 0:
            return "Invalid price data"

        # Snapshot the original grade BEFORE the HTF-data and HTF-zone
        # downgrades mutate analysis.grade. The KILL_ZONE bypass below
        # uses original_grade so trades that score Grade-A on raw merit
        # but get downgraded to B for HTF-context reasons can still
        # bypass the kill-zone gate when displacement/sweep is confirmed.
        # Without this snapshot, the XAU +$835 (2026-04-24) winner was
        # blocked: HTF DATA GATE downgraded A->B, then KILL_ZONE saw
        # grade=B and rejected it. Confirmed via broker-truth bench
        # 2026-04-26 (commit be905bf was incomplete; this commit fixes).
        original_grade = analysis.grade

        # HTF DATA GATE: Grade A requires HTF context. Without W1/D1/H4 data,
        # the system can't confirm macro direction — a M15 CHoCH in a bearish
        # H4 trend looks like Grade A but is actually a counter-trend bounce.
        # GER40 -$956 loss (2026-04-22) was caused by this exact scenario.
        trade_type = _classify_trade_type(analysis)
        if trade_type != "scalp" and analysis.grade == "A":
            has_htf = (
                analysis.htf_analysis is not None
                and analysis.htf_analysis.bias != "NEUTRAL"
                and (analysis.w1_bias or analysis.d1_bias)
            )
            if not has_htf:
                # Downgrade to B — no Grade A without confirmed macro context
                analysis.grade = "B"
                analysis.total_score = min(analysis.total_score, 79)
                analysis.confluence_factors.append("HTF_data_missing_cap_B")
                print(
                    f"  [{analysis.symbol}] GRADE CAP A->B: HTF data missing "
                    f"(W1={analysis.w1_bias or '?'} D1={analysis.d1_bias or '?'} "
                    f"H4={analysis.htf_analysis.bias if analysis.htf_analysis else '?'})",
                    flush=True,
                )

        # HTF ALIGNMENT GATE: non-scalp trades must not oppose H4 bias.
        # Scalps are exempt (fast in/out, don't need HTF confirmation).
        if trade_type != "scalp" and analysis.htf_analysis:
            htf_bias = analysis.htf_analysis.bias
            if htf_bias != "NEUTRAL":
                opposes_htf = (
                    (htf_bias == "BULLISH" and analysis.direction == "BEARISH") or
                    (htf_bias == "BEARISH" and analysis.direction == "BULLISH")
                )
                if opposes_htf:
                    return (
                        f"HTF MISALIGNMENT: {analysis.direction} opposes H4 bias ({htf_bias}) — "
                        f"{trade_type} trades must align with HTF. Only scalps exempt."
                    )

        # ZONE GATE: BUY in premium or SELL in discount = wrong side of fair value.
        # This was the #1 cause of losses — system was buying into resistance.
        if analysis.pd_zone and not analysis.pd_aligned:
            zone = analysis.pd_zone
            direction = analysis.direction
            if (direction == "BULLISH" and zone == "premium") or \
               (direction == "BEARISH" and zone == "discount"):
                return (
                    f"ZONE VIOLATION: {direction} in {zone} — "
                    f"ICT rule: BUY in discount only, SELL in premium only"
                )

        # HTF ZONE CHECK: M15 vs H4 premium/discount conflict.
        # Changed from hard block to downgrade — let Claude evaluate.
        # Strong displacement can break through macro zones. The synergy
        # scorer already penalizes this (-5 for HTF zone conflict).
        # Only hard-block Grade C (not worth API call with zone conflict).
        htf_pd_zone = getattr(analysis, 'htf_pd_zone', '')
        htf_pd_aligned = getattr(analysis, 'htf_pd_aligned', True)
        if htf_pd_zone and not htf_pd_aligned:
            direction = analysis.direction
            if (direction == "BULLISH" and htf_pd_zone == "premium") or \
               (direction == "BEARISH" and htf_pd_zone == "discount"):
                if analysis.grade == "C":
                    return (
                        f"HTF ZONE VIOLATION: {direction} in H4 {htf_pd_zone} — "
                        f"Grade C with zone conflict not worth API call"
                    )
                # Downgrade A->B for zone conflict, let Claude decide
                if analysis.grade == "A":
                    analysis.grade = "B"
                    analysis.total_score = min(analysis.total_score, 79)
                    analysis.confluence_factors.append(f"HTF_zone_conflict(H4_{htf_pd_zone})")
                    print(
                        f"  [{analysis.symbol}] GRADE CAP A->B: {direction} in H4 {htf_pd_zone} "
                        f"— letting Claude evaluate with reduced conviction",
                        flush=True,
                    )

        # KILL ZONE GATE: ICT 2022 model says ONLY trade during kill zones.
        # Kill zones: London 2-5AM ET, NY AM 7-10AM ET, NY PM 1:30-3PM ET.
        # Silver Bullet windows also qualify (3-4AM, 10-11AM, 2-3PM ET).
        # Exception 1: Grade A setups with sweep + displacement can trade outside.
        # Exception 2: Crypto trades 24/7 — exempt from kill zone gate but
        #   still get reduced session score (already handled in scorer).
        # Exception 3: JPY pairs during Tokyo open (19:00-23:00 ET = 00:00-04:00 UTC).
        #   Tokyo is the legitimate ICT kill zone for JPY pairs; treat it as their
        #   London-equivalent. Non-JPY pairs stay blocked during Asian session
        #   because they're in the accumulation phase (no directional move expected).
        _CRYPTO_SYMBOLS = {"BTCUSD", "ETHUSD", "SOLUSD", "DOGEUSD"}
        base_sym = analysis.symbol.split(":")[-1]
        is_crypto = base_sym in _CRYPTO_SYMBOLS

        # Tokyo kill zone: 00:00-04:00 UTC (19:00-23:00 ET) for JPY pairs only.
        now_utc = datetime.now(timezone.utc)
        is_tokyo_window = 0 <= now_utc.hour < 4
        is_jpy_pair = "JPY" in base_sym  # USDJPY, EURJPY, GBPJPY, AUDJPY, etc.
        is_tokyo_kz_for_jpy = is_tokyo_window and is_jpy_pair

        if (
            not analysis.is_kill_zone
            and not analysis.is_silver_bullet
            and not is_crypto
            and not is_tokyo_kz_for_jpy
        ):
            # Allow Grade A trades outside the kill zone IF either:
            #   (a) displacement_confirmed (the strict ICT criterion: sweep
            #       + reversal FVG), OR
            #   (b) sweep_detected (upstream condition — some clean
            #       continuation Grade A trades have a sweep but the FVG
            #       didn't materialize in the lookback; still strong enough
            #       to let Claude evaluate)
            #
            # Loosened 2026-04-26 from displacement-only to (displacement OR
            # sweep). Empirical: the displacement-only bypass blocked
            # XAUUSD +$835 (real Grade A winner outside canonical KZ). See
            # memory/feedback_kill_zone_too_strict.md and the broker-truth
            # bench at scripts/bench_winners_not_blocked_2026-04-26.txt.
            # Use ORIGINAL grade snapshot (before HTF-data / HTF-zone
            # downgrades). A trade that scored raw Grade-A and has
            # sweep+displacement evidence shouldn't be excluded from the
            # KZ bypass just because we downgraded its conviction for
            # macro-context reasons.
            grade_a_high_conviction = original_grade == "A" and (
                analysis.displacement_confirmed or analysis.sweep_detected
            )
            # HTF rejection bypass: an HTF FVG/OB rejection setup (textbook
            # ICT 2022 short trigger when paired with M15 displacement) is
            # higher-conviction than ordinary M15 sweep. Allow it through
            # the KZ gate even outside canonical kill zones, on the same
            # original_grade=A footing as the existing bypass — but the
            # detector itself is feature-flagged in ict_pipeline.py so this
            # only fires when htf_rejection_enabled=True.
            _factors_lower = " ".join(
                getattr(analysis, "advanced_factors", []) or []
            ).lower()
            # Match both old (HTF_REJ_<TF>_<DIR>) and new
            # (HTF_REJ_<TRIG>_<ZONE>_<DIR>) factor formats. Substring is
            # sufficient — any HTF_REJ_* present means a rejection fired.
            htf_rej_present = "htf_rej_" in _factors_lower
            htf_rejection_high_conviction = (
                original_grade == "A"
                and htf_rej_present
                and analysis.displacement_confirmed
            )
            if not (grade_a_high_conviction or htf_rejection_high_conviction):
                return (
                    f"KILL ZONE GATE: Not in a kill zone or Silver Bullet window. "
                    f"ICT 2022 model: only trade during London (2-5AM ET), NY AM (7-10AM ET), "
                    f"or NY PM (1:30-3PM ET). JPY pairs also permitted during Tokyo "
                    f"(19:00-23:00 ET). Session: {analysis.session_type}"
                )

        if is_tokyo_kz_for_jpy and not analysis.is_kill_zone:
            print(
                f"  [{analysis.symbol}] TOKYO KZ: JPY pair during Tokyo window "
                f"(19:00-23:00 ET) — allowed as JPY-specific kill zone",
                flush=True,
            )

        # DOL PRE-FILTER: ICT 2022 model Step 2 — identify WHERE price needs to go.
        # If there's no clear Draw on Liquidity target, there's no clear trade.
        # This is checked roughly here using FVG entry and ATR.
        if analysis.atr_m15 > 0 and analysis.current_price > 0:
            # Estimate minimum target distance: 2x SL distance (for 2:1 R:R)
            min_target_dist = analysis.atr_m15 * 2.0 * 2.0  # 2x ATR SL × 2:1 R:R = 4x ATR
            # Check if any liquidity target exists at that distance
            has_target = False
            fib_levels = getattr(analysis, 'fib_tp_levels', [])
            if fib_levels:
                for fib in fib_levels[:2]:  # Check TP1 and TP2
                    dist = abs(fib - analysis.current_price)
                    if dist >= min_target_dist:
                        has_target = True
                        break
            # Also check key levels (DOL)
            key_opens = getattr(analysis, 'key_opens', {})
            for _, open_price in (key_opens or {}).items():
                dist = abs(open_price - analysis.current_price)
                if dist >= min_target_dist:
                    has_target = True
                    break
            if not has_target and analysis.grade not in ("A", "B"):
                return (
                    f"NO CLEAR DOL: No liquidity target found >= {min_target_dist:.2f} "
                    f"({4:.0f}x ATR) from current price. ICT requires clear Draw on Liquidity "
                    f"before entering. Grade A/B exempt."
                )

        # INTERMARKET CONFLICT GATE: DXY/US10Y/VIX opposing trade direction
        # ICT: "Never trade EUR without knowing where DXY is going"
        if getattr(analysis, 'intermarket_conflict', False) and analysis.grade != "A":
            return (
                f"INTERMARKET CONFLICT: {getattr(analysis, 'intermarket_explanation', 'DXY/VIX opposes trade')}. "
                f"Grade A exempt. Grade {analysis.grade} blocked."
            )

        # G2 COMPOUND GATE: SELL into stacked support during bullish forming H4.
        # The standalone forming-H4-against-trade gate failed bench_winners_not_blocked
        # (blocked BTCUSD +$456 + ETHUSD +$38). G2 adds two bypass conditions:
        #   - is_sell:                only block SELLs (BUY winners protected)
        #   - stacked_opposing_fvg:   require >=2 opposing HTF FVGs within 0.5%
        # Validation (scripts/bench_validate_triple_gate.py, 2026-04-27):
        #   train: 0/8 winners blocked, 3/18 losers caught, +$1,392
        #   test:  0/7 winners blocked, 3/20 losers caught, +$775
        #   full:  0/15 winners blocked, 6/38 losers caught, +$2,167
        forming_o = getattr(analysis, 'forming_h4_open', 0.0)
        forming_c = getattr(analysis, 'forming_h4_close', 0.0)
        forming_h = getattr(analysis, 'forming_h4_high', 0.0)
        forming_l = getattr(analysis, 'forming_h4_low', 0.0)
        atr_h4 = getattr(analysis, 'atr_h4', 0.0)
        opposing_fvg_count = getattr(analysis, 'htf_opposing_fvg_count_05pct', 0)

        if (forming_o > 0 and forming_c > 0 and atr_h4 > 0 and forming_h > 0):
            forming_range = forming_h - forming_l
            forming_bull = forming_c > forming_o
            # Direction (string-safe — analysis.direction may be enum or name)
            dir_str = str(getattr(analysis.direction, "name", analysis.direction))
            is_sell = (dir_str == "BEARISH")
            # G2: SELL + bullish forming H4 (>=0.5*ATR range) + stacked support (>=2 FVGs <0.5%)
            if (is_sell and forming_bull and forming_range >= 0.5 * atr_h4
                    and opposing_fvg_count >= 2):
                return (
                    f"G2 COMPOUND GATE: SELL into stacked support during bullish forming H4. "
                    f"Forming H4: O={forming_o:.4f} C={forming_c:.4f}, range={forming_range:.4f} "
                    f"= {forming_range/atr_h4:.1f}x ATR. {opposing_fvg_count} bullish HTF FVGs "
                    f"within 0.5% below price. (Validated: 0/15 winners, 6/38 losers, +$2,167.)"
                )

        return None

    # ------------------------------------------------------------------
    # Claude API call
    # ------------------------------------------------------------------

    SYSTEM_PROMPT = (
        "You are a disciplined ICT trader. Evaluate this setup objectively. "
        "Trade when the confluence of structure, displacement, and entry precision "
        "justifies the risk. A signal that reached you has passed pre-gate filters, "
        "but you should still exercise discretion on entry quality. "
        "A Grade B signal missing key confirmations (no displacement, no sweep, "
        "wrong premium/discount zone) should be SKIPPED. "
        "Focus on entry precision: exact entry price, tight SL behind structure, "
        "and realistic TP levels. Respond with JSON only."
    )

    def _call_claude(self, analysis: SymbolAnalysis, model: str) -> TradeDecision:
        """Call Claude to evaluate the signal and return a trade decision.

        Uses Anthropic prompt caching on both the system prompt and the rules
        section to reduce API cost by ~30-40%. The ephemeral cache persists for
        5 minutes, covering multiple symbols in a single analysis cycle.
        """
        signal_prompt = self._build_signal_section(analysis)
        rules_prompt = self._build_rules_section(analysis)

        response = self.client.messages.create(
            model=model,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": self.SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": rules_prompt,
                            "cache_control": {"type": "ephemeral"},
                        },
                        {
                            "type": "text",
                            "text": signal_prompt,
                        },
                    ],
                }
            ],
        )

        text = response.content[0].text.strip()
        return self._parse_response(text, analysis, model)

    def _build_prompt(self, a: SymbolAnalysis) -> str:
        """Build the full prompt by combining rules and signal sections (backward compat)."""
        return self._build_rules_section(a) + "\n\n" + self._build_signal_section(a)

    def _build_rules_section(self, a: SymbolAnalysis) -> str:
        """Build the static rules/context section (cached by Anthropic across calls).

        Contains: strategy context, session context, ICT teachings, concept block,
        and the RULES block. These change rarely within a 5-minute window and are
        good candidates for prompt caching.
        """
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

        # SL floor uses asset-class-specific ATR multiple.
        # Crypto needs 2.5x because volatility + wider sweep wicks; forex/indices 2.0x.
        base_sym = a.symbol.split(":")[-1] if ":" in a.symbol else a.symbol
        _crypto_syms = {"BTCUSD", "ETHUSD", "SOLUSD", "DOGEUSD", "ADAUSD", "AVAXUSD"}
        atr_multiplier = 2.5 if base_sym in _crypto_syms else 2.0
        min_sl_dist = a.atr_m15 * atr_multiplier if a.atr_m15 > 0 else 0

        # Percentage floor used ONLY when ATR is unavailable. The post-gate
        # enforces max(atr_multiplier * ATR, percent_floor * entry) on what
        # Claude returns, so the prompt must make the ATR rule the primary one.
        _pct_floors = {
            "BTCUSD": 0.005, "ETHUSD": 0.005, "SOLUSD": 0.005, "DOGEUSD": 0.005,
            "XAUUSD": 0.003, "XAGUSD": 0.003,
            "US30": 0.003, "US500": 0.003, "US100": 0.003, "GER40": 0.003,
        }
        pct_floor = _pct_floors.get(base_sym, 0.0015)  # forex default 0.15%

        # Concept teaching block — loads relevant ICT concept cards so Claude
        # reasons from methodology, not just numeric scores.
        try:
            from bridge.concept_injector import build_concept_teaching_block, build_gate_violation_warning
            concept_block = build_concept_teaching_block(a)
            gate_warning = build_gate_violation_warning(a)
        except Exception:
            concept_block = ""
            gate_warning = ""

        return f"""You are an ICT trading decision engine trained in ICT methodology (market structure, PO3, AMD, liquidity engineering, premium/discount, OTE, displacement, FVGs, order blocks, SMT divergence). Enhanced with 33 ChartFanatics strategies and 375K MT5 backtest passes. Evaluate this signal using ICT principles and respond with ONLY a JSON object.
{strategy_ctx}
{session_ctx}
{ict_ctx}
{concept_block}
{gate_warning}
{mr_warning}

RULES:
- Minimum R:R 1.25:1 (hard gate — trades below this are auto-rejected). Prefer 2:1+ but 1.25:1 is acceptable with strong confluence.
- Grade A: full conviction | Grade B: strict risk | Grade C: pullback only
- **SL PLACEMENT — PRIMARY RULE (hard enforced post-gate):**
  SL distance from entry MUST be at least {atr_multiplier}x ATR(14) on M15 = **{min_sl_dist:.5f}** (for {base_sym}){' [CRYPTO — 2.5x REQUIRED, not 2.0x]' if base_sym in ('BTCUSD','ETHUSD','SOLUSD','DOGEUSD') else ''}.
  Do NOT propose an SL that barely clears any % floor — the ATR rule is primary. Place SL BEYOND the liquidity sweep wick plus the ATR buffer, not at the edge.
  Only when ATR data is zero/missing, fall back to {pct_floor:.1%} of entry as the floor.
- CRITICAL: Tight stops get hunted. Our data shows ETH/SOL sweeps regularly exceed 2x ATR; 2.5x ATR is the minimum safe distance for crypto. ETH trade 2026-04-23 stopped at 1.31% SL while 2.5x ATR would have required ~1.8% — that's the exact gap that cost the -$361 loss.
- TP at next liquidity target
- TP1 (tp_price): nearest HTF FVG or Order Block in trade direction (partial close at 50%). Prefer the 1.272 Fibonacci extension if available.
- TP2 (tp2_price): next liquidity pool / swing high/low (final target, trail remainder). Prefer the 1.618 or 2.0 Fibonacci extension if available.
- If only one TP level is clear, use Fibonacci extensions as TP targets. Fallback: tp2_price = tp_price * 1.5 (BUY) or * 0.667 (SELL).
- Max risk for this symbol/grade: {max_risk:.1%}
- Trade type: {trade_type.upper()} → SL placement: {'beyond the swept high/low (give buffer beyond the wick — this is a multi-day swing)' if trade_type == 'swing' else 'behind the OB/FVG entry zone (at least 1.5x ATR from entry)' if trade_type == 'intraday' else 'tight behind the entry zone (scalp — quick in/out)'}
- CRITICAL ZONE CHECK: BUY in premium or SELL in discount = WRONG ZONE. Auto-downgrade by 1 grade or SKIP unless strong confluence overrides.
- KEY OPENS: Daily/Weekly/Monthly/Quarterly opens are equilibrium references. Price above weekly open = bullish week forming. A sweep of the weekly open followed by rejection = high-probability reversal. Monthly and quarterly opens are macro bias levels.
- IPDA RANGES: Price near IPDA 20-day extreme (>90% or <10%) = high-probability reversal zone. At 40/60-day extreme = macro reversal. IPDA midpoint = equilibrium (expect accumulation, not trending). Trade WITH the IPDA range when in the middle, look for REVERSALS at extremes.
- QUARTERLY SHIFT: If a quarterly shift is confirmed (3+ weeks), all trades MUST align with the shift direction. A developing shift (2 weeks) should reduce confidence in counter-shift trades by 1 grade.
- HTF FVG OBSTACLE: If price is inside or approaching an opposing H4 FVG, this is a HIGH-PROBABILITY rejection zone. BUY inside a bearish H4 FVG = buying into institutional resistance. SELL inside a bullish H4 FVG = selling into institutional support. Auto-downgrade by 1 grade or SKIP unless very strong displacement has already broken through the FVG.
- HTF PULLBACK ACTIVE: If H4 has 3+ consecutive closes moving AGAINST the bias direction, the pullback/retracement is still in progress. DO NOT enter with the bias until the pullback completes — you need a sweep of a significant low (BUY) or high (SELL) followed by displacement BACK in the bias direction. Entering during an active pullback = buying into selling pressure. SKIP or reduce confidence by 20+ points.
- MTF ALIGNMENT: When Weekly, Daily, and H4 biases all agree, this is the highest-conviction direction. When they conflict (e.g., W1 bearish but H4 bullish), trade with reduced size or SKIP — the H4 move may be a pullback within the larger trend.
- FVG ENTRY: When an OPTIMAL ENTRY zone is provided, use the FVG CE (consequent encroachment) as entry_price instead of current market price. This creates a limit-order-style entry at the imbalance zone. ICT entries should be at the FVG/OB zone on the pullback, NOT at market price after the move has started.
- EA ensemble confirmation adds conviction — treat as extra confluence factor
- If in a kill zone that matches this symbol's best session: boost confidence
- If mean reversion warning applies: reduce continuation confidence, prefer reversal setups
- If session confidence multiplier < 1.0: reduce position size proportionally

Respond ONLY with this JSON (no markdown, no explanation):
{{"action":"<BUY|SELL|SKIP>","entry_price":<float>,"sl_price":<float>,"tp_price":<float>,"tp2_price":<float>,"confidence":<0-100>,"risk_pct":<decimal e.g. 0.01 means 1%>,"reasoning":"<1 sentence>"}}

risk_pct MUST be a decimal fraction: 0.01 = 1%, 0.005 = 0.5%, 0.0025 = 0.25%. Max allowed: {max_risk:.4f}

Confidence calibration:
- 90-100: Grade A, all 3 ICT layers confirmed, kill zone active, OB+FVG stacked
- 75-89: Grade B, strong structure + good entry, 1-2 missing confluences
- 60-74: Grade C, partial setup, pullback entry only
- Below 60: SKIP

If the setup is not convincing, use "SKIP" for action."""

    def _build_signal_section(self, a: SymbolAnalysis) -> str:
        """Build the dynamic signal section (changes every call — not cached).

        Contains: current price, scores, confluence factors, and live market data.
        """
        # Check for EA ensemble signal in confluence factors
        ea_line = ""
        for factor in (a.confluence_factors or []):
            if factor.startswith("EA_ensemble"):
                ea_line = f"\n- EA Strategy Ensemble: CONFIRMS {a.direction} ({factor})"
                break

        if a.atr_m15 > 0:
            _base = a.symbol.split(":")[-1] if ":" in a.symbol else a.symbol
            _mult = 2.5 if _base in ("BTCUSD", "ETHUSD", "SOLUSD", "DOGEUSD", "ADAUSD", "AVAXUSD") else 2.0
            _min_sl = a.atr_m15 * _mult
            atr_line = (
                f"\n- ATR(14) on M15: {a.atr_m15:.5f}"
                f"\n- Minimum SL distance ({_mult}x ATR): {_min_sl:.5f}  ← SL must be at least this far from entry"
            )
        else:
            atr_line = "\n- ATR(14) on M15: UNAVAILABLE — use percentage floor fallback (see RULES)"

        # Fibonacci extension TP levels (if available)
        fib_line = ""
        fib_tp = getattr(a, "fib_tp_levels", [])
        if fib_tp:
            fib_line = f"\n- Fib Extension TPs: 1.272={fib_tp[0]:,.2f}, 1.618={fib_tp[1]:,.2f}, 2.0={fib_tp[2]:,.2f}, 2.618={fib_tp[3]:,.2f}"

        # Volume profile (POC/VAH/VAL + node counts on M15)
        vp_line = ""
        vp_poc = getattr(a, "vp_poc", 0.0)
        if vp_poc:
            vp_vah = getattr(a, "vp_vah", 0.0)
            vp_val = getattr(a, "vp_val", 0.0)
            vp_hvn = len(getattr(a, "vp_hvn_zones", []) or [])
            vp_lvn = len(getattr(a, "vp_lvn_zones", []) or [])
            vp_line = f"\n- VolumeProfile: POC={vp_poc:,.2f} VA=[{vp_val:,.2f}-{vp_vah:,.2f}] HVNs={vp_hvn} LVNs={vp_lvn}"

        # Advanced ICT context
        adv_line = ""
        adv_factors = getattr(a, "advanced_factors", [])
        if adv_factors:
            adv_line = f"\n- Advanced ICT: {', '.join(adv_factors)} (score: {getattr(a, 'advanced_score', 0):.0f}/100)"

        # Judas Swing context
        judas_line = ""
        if getattr(a, "has_judas_swing", False):
            judas_line = f"\n- JUDAS SWING detected ({getattr(a, 'judas_direction', '?')}) — manipulation phase complete, distribution move expected"

        # Asian Range context
        asian_line = ""
        asian_rng = getattr(a, "asian_range", None)
        if asian_rng:
            asian_line = f"\n- Asian Range: {asian_rng[0]:,.2f} \u2013 {asian_rng[1]:,.2f} (sweep of this range = high-probability entry)"

        # Key institutional opens from liquidity map
        opens_line = ""
        key_opens = getattr(a, "key_opens", {})
        if key_opens:
            parts = []
            for label, price in key_opens.items():
                parts.append(f"{label}={price:,.2f}")
            opens_line = f"\n- Key Opens: {', '.join(parts)}"

        # IPDA ranges — institutional delivery framework
        ipda_line = ""
        ipda = getattr(a, "ipda_ranges", None)
        if ipda:
            parts = []
            for label, rng in ipda.items():
                position = "AT LOW" if rng["pct"] < 10 else "AT HIGH" if rng["pct"] > 90 else f"{rng['pct']:.0f}%"
                parts.append(f"{label}: {rng['low']:,.2f}-{rng['high']:,.2f} (price at {position})")
            ipda_line = f"\n- IPDA Ranges: {' | '.join(parts)}"

        # Quarterly shift context
        qshift_line = ""
        q_shift = getattr(a, "quarterly_shift", None)
        if q_shift:
            qshift_line = f"\n- QUARTERLY SHIFT: {q_shift['direction']} ({q_shift['strength']}, {q_shift['weeks_confirmed']} weeks)"

        # HTF premium/discount context
        htf_pd_line = ""
        htf_pd = getattr(a, "htf_pd_zone", "")
        if htf_pd:
            htf_aligned = getattr(a, "htf_pd_aligned", False)
            htf_pd_line = f"\n- H4 P/D Zone: {htf_pd} | Aligned: {htf_aligned}"
            if not htf_aligned:
                htf_pd_line += " *** MACRO ZONE CONFLICT ***"

        # Multi-timeframe alignment
        mtf_line = ""
        mtf_alignment = getattr(a, "mtf_alignment", "")
        if mtf_alignment:
            aligned = getattr(a, "mtf_aligned", False)
            mtf_line = f"\n- MTF Bias: {mtf_alignment} | {'ALL ALIGNED' if aligned else 'CONFLICT \u2014 reduced conviction'}"

        # FVG entry zone — optimal limit entry price
        fvg_entry_line = ""
        fvg_entry = getattr(a, "fvg_entry_price", 0)
        if fvg_entry > 0:
            fvg_zone = getattr(a, "fvg_entry_zone", "")
            fvg_entry_line = f"\n- OPTIMAL ENTRY: {fvg_zone} \u2014 use CE as entry_price instead of current market price"

        # HTF FVG obstacle warning
        # HTF pullback warning
        pullback_line = ""
        if getattr(a, "htf_pullback_active", False):
            bars = getattr(a, "htf_pullback_bars", 0)
            pullback_line = f"\n- *** HTF PULLBACK ACTIVE: H4 has {bars} consecutive closes against {a.direction} bias -- retracement NOT complete. Wait for sweep + displacement before entering. ***"

        htf_fvg_line = ""
        if getattr(a, "htf_fvg_obstacle", False):
            zone = getattr(a, "htf_fvg_obstacle_zone", "")
            htf_fvg_line = f"\n- *** HTF FVG OBSTACLE: {zone} \u2014 price is entering an opposing H4 FVG (high-probability rejection zone) ***"

        # Intermarket context for Claude prompt
        intermarket_line = ""
        imkt_expl = getattr(a, "intermarket_explanation", "")
        if imkt_expl:
            conflict = getattr(a, "intermarket_conflict", False)
            vix_mult = getattr(a, "vix_risk_multiplier", 1.0)
            intermarket_line = f"\n- INTERMARKET: {imkt_expl}"
            if conflict:
                intermarket_line += " *** CONFLICT \u2014 reduce conviction ***"
            if vix_mult < 1.0:
                intermarket_line += f" | VIX elevated \u2014 risk multiplier {vix_mult}x"

        # Upcoming news events
        news_line = ""
        if getattr(a, "news_event", ""):
            news_line = f"\n- NEWS WARNING: {a.news_event} ({a.news_minutes}min away) \u2014 reduce size or avoid"

        # Load rules for score display
        rules = _load_rules_json()
        profile = rules.get("symbol_profiles", {}).get(a.symbol, {})
        risk_overrides = profile.get("risk_overrides", {})
        grade_key = f"grade_{a.grade.lower()}" if a.grade in ("A", "B", "C") else "grade_c"
        max_risk = risk_overrides.get(grade_key, 0.01)

        return f"""SIGNAL:
- Symbol: {a.symbol} @ ${a.current_price:,.2f}
- Direction: {a.direction} | Grade: {a.grade} ({a.total_score:.0f}/100)
- Confluence: {', '.join(a.confluence_factors) if a.confluence_factors else 'None'}
- Session: {a.session_type} | Kill Zone: {a.is_kill_zone} | Silver Bullet: {a.is_silver_bullet}
- P/D Zone (M15): {a.pd_zone or 'unknown'} | Aligned: {a.pd_aligned}{htf_pd_line}
- Displacement: {'CONFIRMED (sweep + FVG reversal)' if a.displacement_confirmed else 'NOT CONFIRMED'}{ea_line}{atr_line}{fib_line}{vp_line}{adv_line}{judas_line}{asian_line}{opens_line}{ipda_line}{qshift_line}{htf_fvg_line}{pullback_line}{mtf_line}{fvg_entry_line}{intermarket_line}{news_line}

SCORE BREAKDOWN (sub-scores are additive \u2014 0 in one component does NOT disqualify):
- Structure: {a.structure_score:.0f}/30{' (strong)' if a.structure_score >= 20 else ' (partial)' if a.structure_score >= 10 else ' (weak)'}
- Order Block: {a.ob_score:.0f}/20{' (confirmed)' if a.ob_score >= 12 else ' (nearby)' if a.ob_score > 0 else ' (none detected \u2014 use FVG/structure for entry)'}
- FVG: {a.fvg_score:.0f}/18{' (displacement confirmed)' if a.fvg_score >= 12 else ' (present)' if a.fvg_score > 0 else ' (none \u2014 use OB/structure)'}
- Session: {a.session_score:.0f}/12 | OTE: {a.ote_score:.0f}/12{' (at optimal level)' if a.ote_score >= 8 else ' (partial retracement)' if a.ote_score > 0 else ' (not at retracement \u2014 acceptable)'}
- SMT: {a.smt_score:.0f}/8{' (divergence confirmed)' if a.smt_score > 0 else ' (no divergence \u2014 optional confluence, not required)'}

Max allowed risk_pct for this call: {max_risk:.4f}"""

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

    # Phrases in Claude's reasoning that indicate a hard-gate violation it
    # recognized but tried to rationalize past. When any of these appear,
    # the trade is rejected regardless of the Grade field Claude returned.
    # Calibrated against 11 BROKER_CLOSE trades (Apr 19-24): catches 4/5
    # losers, passes both winners. Phrases like "mtf conflict" and "macro
    # zone conflict" were REMOVED because winners mention them too — the
    # loser signal is Claude's *self-downgrade* language plus specific
    # disqualifiers (outside-kill-zone, PO3-accumulation, score-decay).
    _REASONING_HARD_GATE_PHRASES: tuple[str, ...] = (
        # Self-downgrade — Claude admits the trade isn't really Grade A.
        # This is the bulletproof signal: if Claude says it's actually B/C,
        # it IS B/C, and the ICT model says Grade C/D = don't trade.
        "reduce conviction to grade b",
        "reduce conviction to grade c",
        "reduce conviction to b/c",
        "reduced conviction to grade b",
        "reduced conviction to grade c",
        "reduced conviction to b/c",
        "b/c threshold",
        "grade b/c execution",
        # "C-equivalent" variants — Claude's softer way of saying the same
        # thing. Added 2026-04-27 after UKOIL SELL -$222 loss: Claude wrote
        # "Grade A signal downgraded to C-equivalent conviction due to macro
        # zone conflict, W1 bullish MTF conflict, partial structure (18/30),
        # and IPDA at 73%" then took the trade with confidence=72.
        # Backtest scan over all logged Claude entries: 1 match (the UKOIL
        # loss). 0 false positives on winners. See bench_c_equivalent_phrase.py.
        "c-equivalent",
        "to c-equivalent",
        "downgraded to c",
        "downgrade to c",
        "grade reduction to c",
        "force grade reduction to c",
        "reduction to c-equivalent",
        # PO3 accumulation — trade is early, price hasn't started directional move.
        # "distribution not yet started" is specific enough to be safe.
        "distribution not yet started",
        "not yet distributed",
        # Score decay — entry price drifted away from the FVG/OB, setup is stale.
        "score decay",
        # REMOVED after 17-trade backtest (blocked winners):
        #   "accumulation phase"  — blocked BTC BUY +$485 (phase was accurate observation, not disqualifying)
        #   "no active kill zone" — blocked ETH SELL +$159 (kill zone absence is contextual)
        #   "outside kill zone"   — (same reasoning; move to contextual scoring instead)
        #   "kill zone inactive"  — (same reasoning)
        # The three removed phrases catch too many winners to be worth the loss prevention.
        # Opposing-sweep and IPDA-extreme gates (below) cover the kill-zone-absence cases more precisely.
    )

    # Opposing-sweep phrases. After a sweep of a low, price typically reverses UP
    # to seek upside liquidity — so a SELL after a low sweep is fading the
    # reversal. Same logic inverted for sweep-of-high + BUY.
    # Backtest (Apr 19-24, 17 matched trades): caught 2 losers (SOL -$35,
    # ETH -$174), 0 false positives, +$210 net.
    _SWEPT_LOW_PHRASES: tuple[str, ...] = (
        "sweep of low", "sweep of asian low", "sweep of pdl", "sweep of pwl",
        "sweep of pml", "sweep of lo.l", "sweep of d_low", "sweep of session low",
        "swept low", "swept the low", "swept pdl", "swept pwl", "swept asian low",
        "liquidity sweep of low", "liq sweep of low", "sweep of equal lows",
        "sweep of lol", "sweep of london low",
        "sweep of d_open+pdl", "sweep of pdl+",
    )
    _SWEPT_HIGH_PHRASES: tuple[str, ...] = (
        "sweep of high", "sweep of asian high", "sweep of pdh", "sweep of pwh",
        "sweep of pmh", "sweep of lo.h", "sweep of d_high", "sweep of session high",
        "swept high", "swept the high", "swept pdh", "swept pwh", "swept asian high",
        "liquidity sweep of high", "liq sweep of high", "sweep of equal highs",
        "sweep of loh", "sweep of london high",
        "sweep of d_open+pdh", "sweep of pdh+",
    )

    # IPDA-extreme fade phrases. Selling at a multi-day high or buying at a
    # multi-day low without HTF bias confirmation is the classic "catch a
    # falling knife / short the top" trap.
    #
    # Extended 2026-04-24 after GBPJPY SELL entry slipped through: Claude writes
    # "IPDA 20/40/60d high" (no "extreme") when placing SL at the multi-day high
    # level. Same trap pattern as the EURJPY loss — SL sits ON the level, so the
    # slightest liquidity grab sweeps it out. The expanded phrase list captures
    # both forms. Re-backtested: still zero false positives on 17-trade history.
    _IPDA_HIGH_EXTREME_PHRASES: tuple[str, ...] = (
        # With "extreme"
        "ipda 20/40/60d high extreme", "ipda 20/40/60 high extreme",
        "ipda 20d high extreme", "ipda 40d high extreme", "ipda 60d high extreme",
        "multi-day high extreme", "ipda high extreme",
        # Without "extreme" — Claude often just names the level factually
        "ipda 20/40/60d high", "ipda 20/40/60 high",
        "ipda 20d high", "ipda 40d high", "ipda 60d high",
        "at ipda high", "at 20d high", "at 40d high", "at 60d high",
    )
    _IPDA_LOW_EXTREME_PHRASES: tuple[str, ...] = (
        "ipda 20/40/60d low extreme", "ipda 20/40/60 low extreme",
        "ipda 20d low extreme", "ipda 40d low extreme", "ipda 60d low extreme",
        "multi-day low extreme", "ipda low extreme",
        "ipda 20/40/60d low", "ipda 20/40/60 low",
        "ipda 20d low", "ipda 40d low", "ipda 60d low",
        "at ipda low", "at 20d low", "at 40d low", "at 60d low",
    )

    def _check_opposing_sweep(self, decision: TradeDecision, reasoning: str) -> str | None:
        """Return rejection reason if trade direction fades a recent sweep."""
        side = decision.action
        swept_low = any(p in reasoning for p in self._SWEPT_LOW_PHRASES)
        swept_high = any(p in reasoning for p in self._SWEPT_HIGH_PHRASES)
        if side == "BUY" and swept_high and not swept_low:
            return "BUY after sweep of high (fading reversal — price seeks downside next)"
        if side == "SELL" and swept_low and not swept_high:
            return "SELL after sweep of low (fading reversal — price seeks upside next)"
        return None

    def _check_ipda_extreme_fade(self, decision: TradeDecision, reasoning: str) -> str | None:
        """Return rejection reason if trade fades an IPDA 20/40/60d extreme."""
        side = decision.action
        at_high = any(p in reasoning for p in self._IPDA_HIGH_EXTREME_PHRASES)
        at_low = any(p in reasoning for p in self._IPDA_LOW_EXTREME_PHRASES)
        if side == "SELL" and at_high:
            return "SELL at IPDA high extreme (shorting multi-day top)"
        if side == "BUY" and at_low:
            return "BUY at IPDA low extreme (catching multi-day bottom)"
        return None

    def _post_gate(self, decision: TradeDecision, analysis: SymbolAnalysis | None = None) -> str | None:
        """
        Validate a trade decision after Claude responds.
        Returns rejection reason or None if valid.
        """
        if not decision.is_trade:
            return None

        # Reasoning self-contradiction gate: Claude sometimes marks a trade
        # Grade A while its own reasoning admits a hard-gate violation
        # (MTF conflict, zone conflict, outside kill zone, etc.) and says
        # "conviction reduced to B/C". Honor Claude's own words — reject.
        reasoning_lower = (decision.reasoning or "").lower()
        sym = analysis.symbol if analysis else "?"
        for phrase in self._REASONING_HARD_GATE_PHRASES:
            if phrase in reasoning_lower:
                print(
                    f"  [{sym}] REASONING GATE: Claude's reasoning contains '{phrase}' "
                    f"(Grade {decision.grade}). Rejecting — honor stated violation.",
                    flush=True,
                )
                return f"Reasoning admits gate violation: '{phrase}'"

        # Opposing-sweep gate: after a low sweep, price seeks upside — SELL fights
        # the reversal. After a high sweep, price seeks downside — BUY fights it.
        sweep_reject = self._check_opposing_sweep(decision, reasoning_lower)
        if sweep_reject:
            print(
                f"  [{sym}] OPPOSING-SWEEP GATE: {sweep_reject} "
                f"(Grade {decision.grade}). Rejecting.",
                flush=True,
            )
            return f"Opposing-sweep: {sweep_reject}"

        # IPDA-extreme-fade gate: trying to short a multi-day high or buy a
        # multi-day low against momentum. Backtest caught EURJPY 2026-04-24 loss.
        ipda_reject = self._check_ipda_extreme_fade(decision, reasoning_lower)
        if ipda_reject:
            print(
                f"  [{sym}] IPDA-EXTREME GATE: {ipda_reject} "
                f"(Grade {decision.grade}). Rejecting.",
                flush=True,
            )
            return f"IPDA-extreme fade: {ipda_reject}"

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

        # Hard gate: NEUTRAL HTF bias caps non-scalp trades to Grade B
        # No H4 directional confirmation = reduced conviction
        if analysis and decision.grade == "A":
            trade_type = _classify_trade_type(analysis)
            if trade_type != "scalp" and analysis.htf_analysis:
                if analysis.htf_analysis.bias == "NEUTRAL":
                    print(f"  [{analysis.symbol}] GRADE DOWNGRADE A->B: HTF bias NEUTRAL (no H4 confirmation for {trade_type})", flush=True)
                    decision.grade = "B"

        # Hard gate: P/D zone mismatch caps grade to B
        # BUY not in discount or SELL not in premium = wrong zone, reduce sizing
        if analysis and analysis.pd_zone and not analysis.pd_aligned and decision.grade == "A":
            zone = analysis.pd_zone
            direction = analysis.direction
            is_zone_mismatch = (
                (direction == "BULLISH" and zone != "discount") or
                (direction == "BEARISH" and zone != "premium")
            )
            if is_zone_mismatch:
                print(f"  [{analysis.symbol}] GRADE DOWNGRADE A->B: {direction} in {zone} (not aligned with ICT zone rule)", flush=True)
                decision.grade = "B"

        # Hard gate: OTE 0/12 caps grade to B
        # Zero optimal trade entry = entry timing is off, reduce sizing
        if analysis and analysis.ote_score == 0 and decision.grade == "A":
            print(f"  [{analysis.symbol}] GRADE DOWNGRADE A->B: OTE score 0/12 (no retracement confirmation)", flush=True)
            decision.grade = "B"

        # Risk % sanity
        if decision.risk_pct > 0.02:
            return f"Risk {decision.risk_pct:.1%} exceeds 2% max"

        # Clamp to per-symbol risk override
        if analysis:
            rules = _load_rules_json()
            profiles = rules.get("symbol_profiles", {})
            profile = profiles.get(analysis.symbol, {})
            overrides = profile.get("risk_overrides", {})
            grade_key = f"grade_{(decision.grade or 'c').lower()}"
            max_risk = overrides.get(grade_key, 0.01)  # default 1%
            if decision.risk_pct > max_risk:
                print(f"  [{analysis.symbol}] Risk clamped: {decision.risk_pct:.4f} → {max_risk:.4f} (per-symbol max for {grade_key})", flush=True)
                decision.risk_pct = max_risk

        # Minimum SL distance: must be at least 2x ATR(14) on M15
        # AND at least 0.5% of price for crypto (sweeps go deep), 0.2% for metals
        # Prevents premature stopouts from liquidity sweeps
        # ALWAYS enforce percentage floor even without ATR data
        if analysis and decision.entry_price > 0 and decision.sl_price > 0:
            sl_distance = abs(decision.entry_price - decision.sl_price)
            # 2.5x ATR for crypto (sweeps regularly exceed 2x), 2x for everything else
            base_sym_check = analysis.symbol.split(":")[-1]
            atr_mult = 2.5 if base_sym_check in ("BTCUSD", "ETHUSD", "SOLUSD", "DOGEUSD") else 2.0
            min_sl_atr = analysis.atr_m15 * atr_mult if analysis.atr_m15 > 0 else 0

            # Per-asset-class minimum as % of price — HARD FLOOR regardless of ATR
            base_sym = analysis.symbol.split(":")[-1]
            if base_sym in ("BTCUSD", "ETHUSD", "SOLUSD", "DOGEUSD"):
                min_sl_pct = decision.entry_price * 0.005  # 0.5% for crypto
            elif base_sym in ("XAUUSD", "XAGUSD"):
                min_sl_pct = decision.entry_price * 0.003  # 0.3% for metals
            elif base_sym in ("US500.cash", "US100.cash", "US30.cash", "GER40.cash"):
                min_sl_pct = decision.entry_price * 0.003  # 0.3% for indices
            else:
                min_sl_pct = decision.entry_price * 0.0015  # 0.15% for forex (was 0.1%)

            min_sl = max(min_sl_atr, min_sl_pct)

            if sl_distance < min_sl:
                return (
                    f"SL too tight: {sl_distance:.5f} < min({min_sl:.5f}). "
                    f"ATR={analysis.atr_m15:.5f}, min_atr={min_sl_atr:.5f}, min_pct={min_sl_pct:.5f}"
                )

        # SL placement validation: SL must be BEYOND liquidity, not sitting on it.
        # Correct ICT placement: BUY SL below swing lows, SELL SL above swing highs.
        # Wrong placement: SL at or above a swing low (BUY) / at or below a swing high (SELL)
        # — that's right in the hunt zone where institutions sweep.
        if analysis and analysis.atr_m15 > 0 and decision.sl_price > 0:
            buffer = analysis.atr_m15 * 0.3  # small buffer for "at the level"
            if decision.action == "BUY" and analysis.swing_lows:
                for swing_low in analysis.swing_lows:
                    # SL should be BELOW swing low. If SL is at or above it,
                    # it's sitting in the liquidity pool — will get hunted.
                    if decision.sl_price >= swing_low - buffer and decision.sl_price <= swing_low + buffer:
                        # Auto-fix: push SL below the swing low instead of blocking
                        new_sl = swing_low - buffer * 2
                        print(
                            f"  [{analysis.symbol}] SL LIQUIDITY FIX: {decision.sl_price:.5f} was AT swing low "
                            f"{swing_low:.5f} — moved to {new_sl:.5f} (below hunt zone)",
                            flush=True,
                        )
                        decision.sl_price = round(new_sl, 5)
                        break
            elif decision.action == "SELL" and analysis.swing_highs:
                for swing_high in analysis.swing_highs:
                    # SL should be ABOVE swing high. If SL is at or below it,
                    # it's sitting in the liquidity pool — will get hunted.
                    if decision.sl_price <= swing_high + buffer and decision.sl_price >= swing_high - buffer:
                        # Auto-fix: push SL above the swing high instead of blocking
                        new_sl = swing_high + buffer * 2
                        print(
                            f"  [{analysis.symbol}] SL LIQUIDITY FIX: {decision.sl_price:.5f} was AT swing high "
                            f"{swing_high:.5f} — moved to {new_sl:.5f} (above hunt zone)",
                            flush=True,
                        )
                        decision.sl_price = round(new_sl, 5)
                        break

        # Entry price must be close to current market price (we execute at market).
        # When Claude returns an FVG CE entry, it may be far from market price
        # (meaning "wait for pullback"). Since we don't support limit orders yet,
        # use market price instead of blocking the trade entirely.
        if analysis and analysis.current_price > 0:
            entry_dist_pct = abs(decision.entry_price - analysis.current_price) / analysis.current_price
            if entry_dist_pct > 0.005:  # 0.5% max distance
                # Instead of blocking, use current market price as entry
                print(
                    f"  [{analysis.symbol}] Entry adjusted: Claude FVG CE {decision.entry_price:.5f} "
                    f"is {entry_dist_pct:.2%} from market {analysis.current_price:.5f} — using market price",
                    flush=True,
                )
                decision.entry_price = analysis.current_price
                # Recalculate R:R with new entry
                rr = decision.risk_reward_ratio
                if rr > 0 and rr < self.min_rr:
                    return f"R:R {rr:.1f} below minimum {self.min_rr} after entry adjustment"

        return None

    # ------------------------------------------------------------------
    # Fallback rule-based
    # ------------------------------------------------------------------

    def _rule_based_decision(self, analysis: SymbolAnalysis) -> TradeDecision:
        """
        Fallback when Claude API is unavailable.
        NEVER auto-enters — always SKIPs. No trade without Claude's judgment.
        """
        return TradeDecision(
            action="SKIP",
            symbol=analysis.symbol,
            entry_price=analysis.current_price,
            reasoning=f"API unavailable — SKIP (Grade {analysis.grade}, {len(analysis.confluence_factors)} confluences). No trades without Claude.",
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
