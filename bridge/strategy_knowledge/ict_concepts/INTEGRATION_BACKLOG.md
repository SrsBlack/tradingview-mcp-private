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

## Stubs to fill in (0 cards as of 2026-04-26 — Track 2 COMPLETE)

> All 18 stubs have been filled across 4 batches. The backlog is empty. Next step (separate small commit): tighten `scripts/lint_memory.py::check_kb_schema()` to FAIL (not just warn) on `[NOT YET DEFINED` markers so future regressions are blocked.

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

### Completed 2026-04-26 (Track 2 batch 3)

- `market_maker_model` — already integrated. detect_market_maker_model on M15 (ict_pipeline.py:929) emits MM_MMBM/MM_MMSM advanced_factor at confidence >= 0.6 (75% if distribution close passed, 50% rejected at gate); +2.5 to advanced_bonus and feeds Wyckoff/PO3 synergy +4 (synergy_scorer.py:322-327, 461-466). Code emits MMBM/MMSM only — LRSM/AMD/IPDA-cycle/weekly-profile types from the card are NOT detected as separate types. Concept_injector surfaces it on 'mm_'/'mmbm'/'mmsm' adv_factors. The card's 7-step buy/sell workflow is implemented across the full pipeline (HTF→PD→PO3→sweep→displacement→OTE→DOL) but the MM_<type> factor is just one piece of confluence within that pipeline, not the orchestrator.
- `stop_raid_displacement_retracement` — partially integrated as a chained pattern, not a single detector. Step 1 = significance-filtered sweep (ict_pipeline.py:529-553); Step 2 = displacement_confirmed flag (ict_pipeline.py:558-575: significant sweep + reversal-direction FVG after sweep bar); Step 3 = OB+FVG-stack > FVG-in-OTE > plain-FVG priority sort for fvg_entry_price (ict_pipeline.py:875-911). Composite +7 synergy 'Sweep + displacement + FVG' at synergy_scorer.py:389-394. Anti-step-1-chase enforced by reasoning post-gates (claude_decision.py:1099-1149). Anti-step-2-chase NOT explicitly gated — relies on entry-price = retracement-anchor structurally. SRDR-completion is NOT a hard gate (Grade A/B can clear without all three steps); enforcement is via SCORING + the OB-without-displacement -4 gate.
- `CISD` — already integrated. detect_cisd on M15 last 20 bars (ict_pipeline.py:727-729, max_age_bars=20) sets has_cisd; 'CISD' inserted at front of advanced_factors (line 1124); +2.5 advanced bonus; feeds 'CISD + PO3 phase transition' +5 synergy at synergy_scorer.py:370-375 (NY/overlap session only); contributes to swing trade-type classification (claude_decision.py:351-358). Concept-injector adds CISD pick on has_cisd or 'cisd' substring. NOT structurally linked to OB list (CISD candle is NOT promoted to an OB). HTF-required-for-CISD rule from card is implicitly enforced by the broader HTF Alignment Gate, not CISD-specific code. CISD-aware sizing reduction (card recommends reduced size when no CHoCH yet) is NOT enforced.
- `fibonacci_extensions` — already integrated. fib_tp_levels = [1.272, 1.618, 2.0, 2.618] computed from H4 dealing range when available else M15 (ict_pipeline.py:1044-1054). Drives DOL pre-filter HARD GATE for Grade C/D (claude_decision.py:657-684, 4×ATR rule, Grade A/B exempt). Prompt 'Fib Extension TPs' line at claude_decision.py:877-880. Synergy 'Fib + Equal Levels' +3 at synergy_scorer.py:382-387. Concept-injector inline TP1/TP2 hint at concept_injector.py:221-223. 1.0 (basic TP1) is NOT computed — only the four extension levels. Bridge does NOT auto-snap final TP to a fib level; fibs are advisory (prompt + DOL filter) and Claude's JSON decision determines the executed TP.
- `volume_profile` — ASPIRATIONAL in the bridge. Real bucketed POC/VAH/VAL/HVN/LVN engine exists at trading-ai-v2/analysis/volume_profile.py but is NOT imported by bridge/ict_pipeline.py. No SymbolAnalysis field carries volume-profile coordinates; no prompt line conveys POC/VAH/VAL; concept_injector has no trigger for the card. Only volume-profile-themed signal is the 'OB+HVN' proxy synergy (+3, synergy_scorer.py:264-280, 449-454) which approximates HVN via FVG_stack OR HiddenOB advanced_factors — the docstring explicitly states 'we don't have tick-level volume profile.' Cross-validation rules in cross_correlations.json/conflict_resolution.json reference fields the bridge does not compute and are documentation only.

### Findings surfaced during batch 3 (NOT yet acted on)

1. **`volume_profile` real wiring opportunity** — analysis.volume_profile already exists in trading-ai-v2; importing it in bridge/ict_pipeline.py (build_volume_profile on M15 session window) and replacing the FVG_stack/HiddenOB proxy in synergy_scorer._ob_at_high_volume with real 'OB overlaps HVN bucket' would convert ~5 cross_correlations rules from documentation-only into actual graded signals. Tracked as a separate code task — do not bundle with stub-fill commits.
2. **CISD candle → OB promotion** — card claims 'the CISD candle IS an order block' but has_cisd and ob_score are independent fields. No path promotes a CISD candle into the OB list. Open design question: should detect_order_blocks pick up CISD candles, or should CISD set a synthetic OB? Tracked as a future code change.
3. **CISD-aware sizing reduction** — card recommends reduced size for CISD-only entries (no CHoCH confirmation yet). Sizing logic in risk_bridge / live_executor_adapter is ATR-based and structure-agnostic. If CISD-only entries should be downsized, that belongs in sizing — not in stub-fill commits.
4. **SRDR-completion hard gate** — currently not enforced; a Grade A/B trade can clear pre-gates without sweep_detected AND/OR displacement_confirmed AND/OR a valid FVG. If atomic SRDR completion should be a hard requirement (it is for the card's 'core 3-step ICT trade pattern'), a new pre-gate would be needed. Tracked as a future code change.
5. **MMM 7-step orchestration** — market_maker_model card defines a 7-step workflow but the bridge enforces those steps via 7+ INDEPENDENT gates that don't know about each other. Could consolidate into an explicit MMM-completion check. Tracked as design question.

### Completed 2026-04-26 (Track 2 batch 4 — FINAL)

- `CRT_candle_range_theory` — partially integrated. detect_crt (Desktop/trading-ai-v2/analysis/ict/advanced.py:232) runs on M15 with lookback=1, bullish/bearish single-bar CRT (wick beyond prior-bar extreme + close back inside). Hits surface as 'CRT(N)' in advanced_factors (ict_pipeline.py:815-816), contribute +2.5 (capped at +10) to total_score (ict_pipeline.py:1126-1132), render into the Claude prompt's 'Advanced ICT' line (claude_decision.py:884-886). NOT a hard gate, NO dedicated synergy in synergy_scorer.py, concept_injector.py:162-279 has NO trigger for the CRT card. Higher-level CRT framing (daily/weekly/session-fractal, weekly-PO3 Mon=accum/Wed=manip/Thu=distrib, candle-anatomy-as-PO3, session-candle-as-CRT) is captured implicitly via PDH/PDL/PWH/PWL in build_liquidity_map + SessionInfo + KILL ZONE GATE + daily_bias PO3, but no separate engine treats prior daily/weekly candles as CRT ranges with explicit BSL/SSL/equilibrium fields.
- `liquidity_void` — NOT INTEGRATED. Grep for 'void' across bridge/*.py and Desktop/trading-ai-v2/**/*.py returns no detection/scoring/gating/prompt-injection matches. Root cause: void detection requires volume-profile data and the bridge does not import analysis.volume_profile (per volume_profile.json's bridge_integration). Closest proxy is _ob_at_high_volume in synergy_scorer.py:264-280 ('OB+HVN' +3 synergy at line 449-454), which approximates HVN via FVG_stack OR HiddenOB advanced_factors — explicitly noted in the docstring as 'we don't have tick-level volume profile.' Cross-validation rules in cross_correlations.json that mention voids reference fields the bridge does not compute and are documentation only. The card's full toolkit (void-as-draw, void-as-TP, SL-on-the-other-side-of-an-unfilled-void, 5-session partial-fill window, stale-void priority decay, FVG+void priority ranking) is informational only. Closing this gap is paired with the volume_profile wiring task (batch 3 finding #1).
- `market_philosophy` — INFORMATIONAL ONLY by design. Grep for 'philosophy' across bridge/*.py returns no matches; concept_injector.py has no trigger that loads this card; no scoring weight, no synergy, no gate references it. Listed in _index.json layer_0_macro and referenced as a depends_on by quarterly_shifts.json + time_price_theory.json so the BFS walker COULD pull it in transitively, but the _MAX_CONCEPTS=8 cap and priority order mean operationally it almost never reaches Claude. Its 5 core_trading_rules ARE enforced operationally in concrete gates: rule_1 (trade FROM liquidity) = DOL pre-filter at claude_decision.py:657-684; rule_2 (draw determines direction) = HTF Alignment Gate + daily_bias; rule_3 (manipulation precedes distribution) = sweep+displacement prerequisites at ict_pipeline.py:558-575 + SRDR composite synergy; rule_4 (time validates price) = KILL ZONE GATE; rule_5 (HTF controls) = HTF Zone Check + HTF FVG obstacle gate. Treat as durable design documentation; do NOT add an injector trigger or 'philosophy passes' factor.

### Findings surfaced during batch 4 (NOT yet acted on)

1. **CRT injector trigger missing** — concept_injector.py:240-279 has triggers for `cisd`, `breaker`, `turtle`, `unicorn`, `venom`, `mm_/mmbm/mmsm` advanced_factors, but NO trigger for `crt` despite `CRT(N)` being the most commonly-emitted advanced_factor on M15. Adding `if "crt" in adv_factors: picks.append(("CRT_candle_range_theory", "Single-bar sweep+reversal — micro CRT setup"))` would surface the methodology to Claude when it fires. **RESOLVED 2026-04-26 in commit c09ca81.**
2. **CRT lookback is hardcoded to 1** — only single-bar CRTs are detected. The card's daily/weekly/session-fractal applications would need separate calls with `lookback={N for daily, M for weekly}` against the relevant-timeframe df. Open design question whether the M15-lookback=1 detector is sufficient or if multi-timeframe CRT detection is needed. **RESOLVED 2026-04-26 in commits 82f8f51, 9c1be34, b32476a (see "Completed 2026-04-26 (multi-TF CRT)" section below).**
3. **Volume-profile wiring is the prerequisite for liquidity_void** — see batch 3 finding #1. The two cards' integration status is paired; both unblock together when build_volume_profile is wired into ict_pipeline. **RESOLVED 2026-04-26 in commits ec29820, 7cb3319, a9f753d (see "Completed 2026-04-26 (volume-profile follow-up)" section below).**

---

## Completed 2026-04-26 (volume-profile follow-up — Track 2 batch 3 finding #1 + batch 4 finding #3)

Three sequential commits closed the volume-profile gap end-to-end:

- **`ec29820` — feat(bridge): wire analysis.volume_profile into ict_pipeline + claude prompt.** Imports build_volume_profile from trading-ai-v2/analysis/volume_profile.py (previously unreferenced from the bridge). Adds five fields to SymbolAnalysis: vp_poc, vp_vah, vp_val, vp_hvn_zones, vp_lvn_zones. HVN/LVN zones stored as (low, high) tuples derived from VolumeNode midpoints +/- bucket_width/2 for clean range-overlap arithmetic downstream. New step 8e7 in ict_pipeline.py mirrors the cbdr_data block: builds the profile on the last ~96 M15 bars (24h) with 30 buckets. claude_decision._build_prompt gains a vp_line ("VolumeProfile: POC=X VA=[L-H] HVNs=N LVNs=M") interpolated next to fib_line.
- **`7cb3319` — feat(synergy): OB+HVN uses real volume-profile bucket overlap (was FVG_stack proxy).** synergy_scorer._ob_at_high_volume rewritten to iterate ob_zones x vp_hvn_zones with standard range-overlap arithmetic; legacy FVG_stack/HiddenOB approximation deleted along with the "we don't have tick-level volume profile" docstring caveat. ob_score >= 10 gate preserved. ict_pipeline now exposes active OBs (post get_active_obs filter) on SymbolAnalysis.ob_zones immediately after order-block detection. Smoke-tested 6 cases (overlap / no-overlap / low ob_score / empty hvn / empty ob / boundary equality).
- **`a9f753d` — feat(bridge): detect unfilled liquidity voids from volume-profile LVN zones.** Adds SymbolAnalysis.liquidity_voids — LVN zones that sit clearly above OR below current price (zones containing current price are excluded as already-being-traversed). concept_injector now triggers the liquidity_void card whenever liquidity_voids is non-empty, with directional hint distinguishing "N above (bullish draw)", "N below (bearish draw)", and "mixed magnets". First time the card has ever been auto-injected. Updates liquidity_void.json bridge_integration with new file:line citations replacing the previous "NOT INTEGRATED" text.

Net effect: volume_profile.json bridge_integration moves from ASPIRATIONAL to REAL with file:line citations. liquidity_void.json bridge_integration moves from NOT INTEGRATED to REAL. The "OB+HVN" +3 synergy is now backed by actual bucketed volume data instead of a proxy. ~5 cross_correlations rules around volume coordinates that previously lived as documentation can now be enforced when written into code (next iteration). Lint stays at 0 stubs / 51 cards.

### Findings surfaced during volume-profile follow-up (NOT yet acted on)

1. **No POC/VAH/VAL proximity trigger** — concept_injector currently does not auto-inject the volume_profile card when current_price is within tolerance of POC/VAH/VAL. The prompt line gives Claude the coordinates; the methodological card is reachable only via dependency walker. If Claude is observed misinterpreting POC/VA dynamics, add an explicit trigger.
2. **No caching** — build_volume_profile recomputes every cycle. Fast on 96 bars x 30 buckets but worth profiling if symbol count grows.
3. **M15-only profile** — H1/H4 volume profiles would add HTF context for swing trades but aren't computed.
4. **Value-area pct fixed at 70%** — not exposed as config. If different markets need different VA widths, plumb through.
5. **Void-as-TP / SL-on-other-side-of-void NOT enforced** — voids surface as prompt context only, not as hard checks against entry/SL placement. Conservative first integration; tighten when there's evidence of misuse.
6. **OB-at-LVN suspicion rule NOT yet enforced** — cross_correlations rule 'OB at LVN = reduce conviction' is still documentation. Could become a -2 anti-synergy alongside the +3 OB+HVN if backtests show the asymmetry.

---

## Completed 2026-04-26 (multi-TF CRT — Track 2 batch 4 finding #2)

Three commits closed the M15-only CRT gap:

- **`82f8f51` — feat(ict): multi-TF CRT detection (D1/H4/M15) with tf_label tagging.** `analysis.ict.advanced.detect_crt` (trading-ai-v2) gains a `tf_label: str = "M15"` parameter; `CRTSetup` gains a matching `tf_label` field. The bridge (`bridge/ict_pipeline.py` step 8f) now calls `detect_crt` explicitly on `df_d1[:-1]` and `df_htf_closed` using already-collected dataframes (no new fetches), appending tagged setups onto `adv.crt_setups`. Factor emission switches from generic `CRT(N)` to per-TF `CRT_D1(N)` / `CRT_H4(N)` / `CRT_M15(N)`. `concept_injector.py` differentiates hints by highest-TF firing — D1="major reversal", H4="swing-tradable", M15="intrabar". Note: the `analysis/ict/advanced.py` change lives in `trading-ai-v2` (still on initial commit baseline; the file has 245 unrelated uncommitted lines from prior sessions). The CRT changes are 5 small edits buried in that diff — kept on disk as untracked WIP. Runtime behavior is unaffected.
- **`9c1be34` — feat(score): per-TF CRT weighting (D1=+4, H4=+3, M15=+2, cap=+10) — backtest-validated.** Replaces the flat +2.5/factor cap=+10 advanced-bonus formula with per-TF weighting for CRT factors. Other advanced_factors keep +2.5. Backtest harness (`scripts/bench_multi_tf_crt.py`, 1260 cycles across XAUUSD/EURUSD/GBPUSD/BTCUSD/ETHUSD): mean delta vs old formula is +0.00 when baseline >=3 non-CRT factors (cap binds either way) and +1.48 when baseline=0 — both within the +/-2.0 ship gate.
- **`b32476a` — feat(synergy): MultiTF_CRT +5 when D1 and H4 CRT both fire.** New synergy in `synergy_scorer._SYNERGY_CHECKS` predicated on `_multi_tf_crt` (substring match on lowercased advanced_factors for "crt_d1" AND "crt_h4"). Backtest harness (`scripts/bench_multi_tf_synergy.py`, 1220 fresh-setup cycles) showed D1+H4 alignment fires in 7.3% of cycles — sweet spot for a +5 conviction premium (rare enough not to be flat inflation, common enough to actually trigger).

Net effect: CRT moves from M15-single-bar to fractal D1/H4/M15 detection, the score formula correctly weights conviction by timeframe, and the +5 MultiTF_CRT synergy explicitly rewards the card's "fractal alignment" framing. CRT_candle_range_theory.json bridge_integration text rewritten with new file:line citations and decision rationale.

## Completed 2026-04-26 (Weekly CRT + Wed-PO3 — multi-TF CRT followups A + C)

Two commits closed two of the four findings above:

- **`504d8a9` — feat(ict): weekly CRT detection (W1 tf_label, +5 score weight).** `bridge/ict_pipeline.py` step 8f calls `_detect_crt_mtf(df_w1[:-1], lookback=1, tf_label="W1")` before the existing D1/H4 calls; aggregation loop iterates `("W1", "D1", "H4", "M15")`. `_CRT_TF_WEIGHTS` adds `"crt_w1": 5.0` (one notch above D1=+4 because weekly CRT is institutional swing-trade reversal at PWH/PWL). `concept_injector.py` adds a W1 hint branch ahead of D1. `scripts/bench_multi_tf_crt.py` now resamples to W1 too — gate passes: avg_mean=+0.12, max|symbol_mean|=0.17 at baseline=0; +0.00 at baseline>=3 (cap binds). Total score still capped at +10.
- **`057694c` — feat(ict): Wednesday-PO3 manipulation gate (sweep Mon-Tue range).** Day-specific detector reusing `_detect_crt_mtf` with a 3-bar `[Mon, Tue, Wed_live]` window and `tf_label="WedPO3"`, gated on `df_d1.index[-1].weekday() == 2`. Wed daily bar IS the sweep, so the live (unfinished) bar is intentionally included. Factor emits as `CRT_WedPO3(N)` at +3.0 weight (rare but high-conviction; equivalent to H4 swing-tradable). Concept injector priority chain reordered to `W1 > D1 > WedPO3 > H4 > M15`.

Net effect: CRT now spans the full institutional fractal — weekly swing reversal at the top, Wednesday manipulation as a day-specific gate, daily/H4/M15 filling the intraday tiers. Two of the four open findings (#1 weekly, #3 Wed-PO3) closed; #2 session-candle-as-CRT and #4 counter-D1-CRT gate remain.

### Findings surfaced during multi-TF CRT (NOT yet acted on)

1. ~~**Weekly CRT not detected**~~ — **RESOLVED 2026-04-26 (commit `504d8a9`)**. `_detect_crt_mtf(df_w1[:-1], lookback=1, tf_label="W1")` runs in step 8f before D1; `crt_w1=+5.0` weight (highest tier) + W1 hint in concept_injector. Bench gate passes (avg_mean=+0.12, max|symbol_mean|=0.17 across 1260 cycles).
2. **Session-candle-as-CRT not detected** — Asian/London/NY explicit fractal (Asian range = accum, London = manip, NY = distrib) is captured implicitly via SessionInfo + KILL ZONE GATE but no `detect_session_crt` engine exists.
3. ~~**Mon/Wed/Thu daily PO3 not enforced**~~ — **RESOLVED 2026-04-26 (commit `057694c`)**. `_detect_crt_mtf(df_d1.iloc[-3:], lookback=1, tf_label="WedPO3")` runs only when `df_d1.index[-1].weekday() == 2`. Emits `CRT_WedPO3(N)` factor at +3.0 weight + dedicated injector hint. Day-restricted (~14% of trading days) so no separate bench gate needed.
4. **No D1-CRT counter-trend gate** — counter-D1-CRT trades (e.g. shorting after a bullish D1 CRT) are not blocked. Bigger behavioral change; defer until a backtest shows asymmetry.

### Findings still open after Followup A + C (2026-04-26)

1. **Session-candle-as-CRT** (#2 above) — still unimplemented. Needs new `detect_session_crt` since session candles aren't time-equispaced bars; would emit `SessionCRT_LBuy(N)` / `SessionCRT_LSell(N)` factors with a `SessionCRT+KillZone` synergy. Save for a session where trading-ai-v2's working tree is clean.
2. **MultiTF_CRT_W1_D1 super-synergy** — would fire when both `crt_w1` AND `crt_d1` present (~1% of cycles); deferred until backtest can confirm it's not flat inflation.
3. **Counter-D1-CRT trade gate** (#4 above) — behavioral change, needs explicit backtest evidence.

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
