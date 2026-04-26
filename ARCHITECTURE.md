# Architecture — Auto-Trading Bridge

> Live, current-state architecture. Read this before reading code.
>
> Style: each section opens with a table or one-line summary for fast skimming, then has prose deep-dives below. Cross-linked so you can jump from any gate to its rationale, from any module to where it's used.
>
> Last verified: 2026-04-26

---

## Table of Contents

1. [TL;DR](#1-tldr) — fastest read; key facts in one screen
2. [Pipeline overview](#2-pipeline-overview) — diagram + per-stage flow
3. [Modules](#3-modules) — every `bridge/` file: purpose / inputs / outputs
4. [Gate inventory](#4-gate-inventory) — every gate, trigger, rationale, where to tune
5. [Bias engine](#5-bias-engine) — structure detection, MTF, history of changes
6. [State and persistence](#6-state-and-persistence) — what survives restarts
7. [Risk model](#7-risk-model) — caps, cooldowns, kill switch
8. [Decision history](#8-decision-history) — chronological log of design choices
9. [Runbooks](#9-runbooks) — how to start/stop/restart/add-symbol/debug

---

## 1. TL;DR

**What this is:** an automated ICT trading bridge that runs against an FTMO-Demo MT5 account. Three async loops on a 15-min cycle: (a) analyze 19 symbols across W1→D1→H4→H1→M15, (b) score with 51-concept ICT engine + 33 ChartFanatics EA strategies, (c) decide via Claude (Sonnet for Grade A, Haiku for Grade B), (d) execute via MT5 with persistent safety state.

| Fact | Value |
|------|-------|
| Account | FTMO-Demo $100k (login 1513140458) |
| Symbols (19) | BTC, ETH, SOL, DOGE, XAU, XAG, UKOIL, US500, US100, YM1!, GER40, EURUSD, GBPUSD, AUDUSD, NZDUSD, USDJPY, EURJPY, GBPJPY, USDCAD (full TV-prefixed list in `rules.json:watchlist`) |
| Cycle | 15 min (900s) |
| Max concurrent positions | 5 (risk-on/risk-off sub-cap of 4) |
| Daily trade cap | dynamic 3/5/7 by Grade A signal count |
| Bridge magic / comment | 99002 / `ICT_Bridge` |
| Model routing | Grade A → Claude Sonnet, Grade B → Haiku, Grade C/D → pre-gate skip |
| R:R minimum | 1.25:1 |
| Min SL | 2.5x ATR (crypto), 2x ATR (other) — crypto floor 0.5%, indices 0.3%, forex 0.15% |
| Daily loss kill | 2% |
| Total drawdown kill | 4% (FTMO is 5% / 10%) |
| Estimated cost | ~$1/day Claude API |

**Repo layout:**

```
tradingview-mcp-jackson/
├── auto_trade.py                  # Entry point — `python auto_trade.py --mode live`
├── bridge/                        # The Python trading system (29 .py files)
├── bridge/strategy_knowledge/ict_concepts/  # 51 ICT concept cards (JSON)
├── rules.json                     # Watchlist + per-symbol risk overrides
├── bridge_safety_state.json       # Restart-safe runtime state
├── archive/                       # Stale 2026-04-08 planning docs
├── verify_*.py                    # Backtest harnesses (gates, MTF invalidation)
└── start_bridge_*.bat             # Windows launchers
```

**Two related repos:**
- `~/Desktop/trading-ai-v2/` — Python lib providing `analysis.*`, `core.types`, `data.mt5_connector`, `execution.*`. Imported by the bridge. Path added via `bridge.config.ensure_trading_ai_path()`.
- `~/mt5-mcp-server/` — separate MT5 MCP server for interactive Claude (magic 99003, comment `ICT_MCP`). Does not interact with the bridge except through MT5 itself.

**Key past incidents that shaped current behavior:**
- 27-trade blowout 2026-04-21: bridge restarts wiped safety state. → `bridge_safety_state.json` persistence.
- GER40 -$956 2026-04-22: Grade A with no HTF data. → HTF Data Gate caps Grade A → B if W1/D1/H4 missing.
- ETH +$38 killed 2026-04-26: crude H4 bias false positive. → Bias engine rewritten with `analysis.structure`.
- SOL "manual close at -$132" wrong premise 2026-04-25: phrase gates added then reverted after broker-truth verification.

See [§ 8 Decision history](#8-decision-history) for the full log.

---

## 2. Pipeline overview

```
                    ┌──────────────────────────────────┐
                    │  start_bridge_mt5only.bat        │ ← user double-clicks
                    │  → python auto_trade.py --mode live │
                    └──────────────┬───────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────┐
                    │  bridge/orchestrator.py          │
                    │  (3 async loops, MT5Connector)   │
                    └──────────────┬───────────────────┘
                                   │
            ┌──────────────────────┼──────────────────────┐
            ▼                      ▼                      ▼
   ┌─────────────────┐  ┌─────────────────┐   ┌─────────────────┐
   │ analysis loop   │  │ position mgmt   │   │ reconcile loop  │
   │ (every 15 min)  │  │ (every 30s)     │   │ (every 60s)     │
   └────────┬────────┘  └────────┬────────┘   └────────┬────────┘
            │                    │                     │
            ▼                    ▼                     ▼
   per-symbol pipeline    trail SL, partial close   sync MT5 ↔ state
            │             check market close
            ▼             check HTF invalidation
   ┌─────────────────────────────────────────────────────────┐
   │ ict_pipeline.py  (5-TF analysis: W1/D1/H4/H1/M15)        │
   │ ─ detect_swings, FVGs, OBs, sweeps, IPDA, kill zones    │
   │ ─ MTF bias via analysis.structure.get_current_bias       │
   │ ─ 51 ICT concept cards available (concept_injector)      │
   └────────────────────┬────────────────────────────────────┘
                        │ SymbolAnalysis
                        ▼
   ┌─────────────────────────────────────────────────────────┐
   │ synergy_scorer.py  (15 synergies, 8 gates)              │
   │ ─ Grade A (≥80), B (65-79), C (50-64), D (35-49)        │
   └────────────────────┬────────────────────────────────────┘
                        │ scored analysis
                        ▼
   ┌─────────────────────────────────────────────────────────┐
   │ claude_decision.py                                       │
   │ ─ _pre_gate() — 9 gates (kill zone, zone, HTF, etc.)    │
   │ ─ Build prompt with concept_injector (8 concepts max)    │
   │ ─ Route: Grade A→Sonnet, Grade B→Haiku                  │
   │ ─ _post_gate() — reasoning self-contradiction + R:R      │
   └────────────────────┬────────────────────────────────────┘
                        │ TradeDecision (BUY/SELL/SKIP/HOLD)
                        ▼
   ┌─────────────────────────────────────────────────────────┐
   │ analysis_pipeline.py  (orchestrates above)               │
   │ ─ news blackout, VIX/heat multipliers                   │
   │ ─ Friday-close gate, weekend gate                       │
   └────────────────────┬────────────────────────────────────┘
                        │
                        ▼
   ┌─────────────────────────────────────────────────────────┐
   │ live_executor_adapter.py → live_executor.py → MT5        │
   │ ─ position sizing (FTMO compliant)                      │
   │ ─ correlation checks (BTC/ETH gate, DXY exposure)       │
   │ ─ persist to bridge_safety_state.json                   │
   └─────────────────────────────────────────────────────────┘
```

### Three loops

| Loop | Interval | What it does |
|------|----------|--------------|
| Analysis | 900s (15 min) | For each symbol: ICT pipeline → score → Claude → maybe execute |
| Position management | ~30s | Trail SL toward TP1, partial close at TP1, check market close (non-crypto), check HTF invalidation |
| Reconciliation | ~60s | Sync MT5 positions ↔ bridge state, detect orphans, mirror to paper shadow |

### Data path

```
MT5 broker (PRIMARY) ──┬─ ict_pipeline.py uses copy_rates_from_pos for OHLCV
                       ├─ strategy_engine.py uses MT5 (fallback to TV)
                       └─ position_manager.py uses MT5 for current price

TradingView via CDP ───── kept warm as fallback only (used if MT5 fails)

Alpaca + Finnhub ─────── price-verification triangulation (entry sanity check)
```

MT5 is primary because TradingView Desktop has symbol-drift issues when switching between 19 symbols (`set_symbol` race conditions). MT5 returns all symbol data simultaneously with zero drift.

---

## 3. Modules

### 3.1 Entry / orchestration

| File | Purpose | Key entry points |
|------|---------|------------------|
| `auto_trade.py` | Thin wrapper, parses CLI args | `bridge.cli.main` |
| `bridge/cli.py` | Argument parsing, mode selection | `main()` |
| `bridge/orchestrator.py` | Three async loops, startup wiring | `Orchestrator.run()` |
| `bridge/config.py` | Config loader, symbol mapping, `ensure_trading_ai_path()` | `get_bridge_config()` |

**Orchestrator startup sequence** (`bridge/orchestrator.py:run()`):
1. Load rules.json + .env
2. `ensure_trading_ai_path()` — adds `~/Desktop/trading-ai-v2/` to sys.path
3. Initialize MT5 connector
4. State store: load `bridge_safety_state.json` → restore counters, cooldowns, trail state
5. `position_manager.reconcile_mt5_on_startup()` — adopt orphan MT5 positions into bridge state
6. `position_manager.reconcile_restored()` — verify restored positions still match MT5
7. Spawn three async tasks (analysis / position mgmt / reconcile)

### 3.2 Analysis layer

| File | Purpose |
|------|---------|
| `bridge/ict_pipeline.py` | 5-TF analysis (W1/D1/H4/H1/M15). Calls `analysis.structure`, `analysis.fvg`, `analysis.liquidity`, `analysis.order_blocks`, `analysis.sessions`, `analysis.smt`. Produces `SymbolAnalysis` dataclass. |
| `bridge/synergy_scorer.py` | 15 confluence synergies + 8 gates. Source of truth: `bridge/strategy_knowledge/ict_concepts/cross_correlations.json`. Outputs total_score 0-100 + grade. |
| `bridge/concept_injector.py` | Loads 51 ICT concept cards from `bridge/strategy_knowledge/ict_concepts/`. Selects up to 8 relevant concepts per signal (1800 char cap) for Claude prompt. |
| `bridge/intermarket.py` | DXY / US10Y / VIX analysis from MT5. Synthetic DXY from EURUSD if no DXY. Conflict gate, VIX risk multiplier. |
| `bridge/economic_calendar.py` | Forex Factory live feed + static schedule. News blackout gate. |
| `bridge/strategy_engine.py` | EA cluster + ICT 4-strategy engine via trading-ai-v2. MT5 primary, TV fallback. |
| `bridge/mt5_data.py` | OHLCV from MT5. Used by ict_pipeline + strategy_engine. |
| `bridge/tv_client.py` / `bridge/tv_data_adapter.py` | TradingView CDP client + bar-to-DataFrame. Fallback only. |

### 3.3 Decision layer

| File | Purpose |
|------|---------|
| `bridge/claude_decision.py` | Pre-gate (9 checks), prompt builder, model routing, post-gate (3 reasoning checks + R:R/SL validators). 9-min decision cache. |
| `bridge/analysis_pipeline.py` | Glue: per-symbol analyze → score → decide → execute. Adds news blackout, VIX/heat multipliers, Friday-close gate. |
| `bridge/decision_types.py` | `TradeDecision`, `SymbolAnalysis`, `PaperPosition` dataclasses |

### 3.4 Execution layer

| File | Purpose |
|------|---------|
| `bridge/live_executor.py` | Wraps trading-ai-v2 `MT5Executor`. Owns `BRIDGE_MAX_POSITIONS=5`, magic 99002, kill switch. |
| `bridge/live_executor_adapter.py` | Sync interface for orchestrator. Holds `open_positions`, balance, daily counters, **HTF invalidation logic**. Persists state. |
| `bridge/paper_executor.py` | Paper-shadow mirror — every live trade replicated, used for A/B comparison. |
| `bridge/risk_bridge.py` | FTMO compliance, position sizing, BTC/ETH contradiction gate, DXY exposure cap, risk-on/risk-off sub-cap. |
| `bridge/position_manager.py` | MT5 ↔ bridge sync, trail SL, partial close at TP1, market-close exit, MT5-on-startup adoption. |
| `bridge/state_store.py` | Position state persistence (separate from safety state). |

### 3.5 Support

| File | Purpose |
|------|---------|
| `bridge/health_monitor.py` | Periodic balance / P&L / cooldown summary line. |
| `bridge/alerts.py` | Telegram bot — sends trade open/close, errors, daily summary. |
| `bridge/session_store.py` | Append-only JSON log of every decision + trade event. Located at `~/.tradingview-mcp/sessions/YYYY-MM-DD.json`. **Source of truth for backtests.** |
| `bridge/trade_drawings.py` | Optional: draws SL/TP lines on TradingView chart. |
| `bridge/trading_hours.py` | Symbol-active checks (kill zones, weekend gate, market closures). |
| `bridge/price_verify.py` | 3-tier price verification (MT5 + Alpaca + Finnhub) for entry sanity. |
| `bridge/symbol_utils.py` | Symbol mapping / class detection helpers. |

---

## 4. Gate inventory

A "gate" is any check that can reject or modify a trade decision. Organized by where it fires.

### 4.1 Pre-gate (before Claude API call)

In `bridge/claude_decision.py:_pre_gate()`. Returns skip reason or None.

| # | Gate | Trigger | Action | Rationale (origin) |
|---|------|---------|--------|--------------------|
| 1 | Analysis error | `analysis.error` set | SKIP | Upstream fail |
| 2 | Min grade | grade ∈ {C, D, INVALID} | SKIP | Don't waste Claude tokens on weak signals |
| 3 | Invalid price | `current_price <= 0` | SKIP | Data integrity |
| 4 | **HTF Data Gate** | Grade A but missing W1/D1/H4 bias | Cap to B (max score 79) | GER40 -$956 2026-04-22: Grade A with no HTF data turned out to be counter-trend bounce. Without macro context, M15 setups score 100 incorrectly. |
| 5 | **HTF Alignment Gate** | non-scalp, signal direction opposes H4 bias | SKIP | Scalps exempt (fast in/out, don't need HTF). Prevents counter-H4 entries. |
| 6 | **Zone Gate** | BUY in M15 premium OR SELL in M15 discount | SKIP | #1 historical loss cause: buying into resistance. Hard block. |
| 7 | HTF Zone | M15 vs H4 premium/discount conflict | Grade A→B downgrade (or skip if already C) | Soft penalty — strong displacement can break macro zones; let Claude evaluate with reduced conviction. |
| 8 | **Kill Zone Gate** | not in (London 2-5AM, NY AM 7-10AM, NY PM 1:30-3PM ET) and not Silver Bullet | SKIP unless Grade A + displacement | ICT 2022 Step 3. Crypto exempt (24/7). JPY pairs allowed during Tokyo window 19:00-23:00 ET. |
| 9 | **Intermarket Conflict Gate** | DXY/US10Y/VIX opposing trade direction (Grade B/C only) | SKIP | Macro confirmation; Grade A trades override. |
| - | DOL pre-filter | no Fib/liquidity target ≥ 4x ATR away | SKIP unless Grade A | ICT 2022 Step 2 (DOL ≥ 2R). |

**Tuning:** `bridge/claude_decision.py:523-672`. Each gate has its own block; modify in isolation.

### 4.2 Post-gate (after Claude returns a decision)

In `bridge/claude_decision.py:_post_gate()`. Three reasoning checks run before R:R/SL validation.

| Check | Trigger | Action | Rationale |
|-------|---------|--------|-----------|
| `_REASONING_HARD_GATE_PHRASES` | Claude's reasoning text contains any phrase in the tuple | Reject | Self-downgrade detection. Only narrowly-scoped phrases survive backtest (e.g. `"reduce conviction to grade b"`, `"distribution not yet started"`, `"score decay"`). See `feedback_reasoning_self_contradiction.md`. |
| `_check_opposing_sweep` | BUY after sweep of high (or SELL after sweep of low) detected in reasoning | Reject | Trading WITH the sweep = joining manipulation. |
| `_check_ipda_extreme_fade` | SELL at IPDA high or BUY at IPDA low | Reject | Counter-trend extreme = catching a falling knife. |

After reasoning checks: standard R:R minimum 1.25:1, SL beyond sweep extreme, ATR floor.

**Backtest:** `verify_new_gates.py` — replay 19-trade history. Current state: 8 blocks, all losers, +$1,103.90 deployment value, 0 winners hit.

**Phrases tried and removed (caught winners or based on wrong premise):**
- `"accumulation phase"` — blocked BTC BUY +$485
- `"no active kill zone"` / `"outside kill zone"` — blocked ETH SELL +$159
- `"no kill zone"` / `"critically impaired"` / `"displacement-no-structure"` — calibrated against the SOL "loser" that turned out to be a manual-close, not a structural loss. Reverted in `f502a00`.

### 4.3 Post-entry guards (during position lifetime)

In `bridge/live_executor_adapter.py`.

| Guard | Trigger | Exemptions | Rationale |
|-------|---------|------------|-----------|
| `_check_market_close_exit` | non-swing, non-crypto position approaching market close (~30 min before) | swing trades, crypto, TP1 hit + r≥1.0 | European indices gap 200-500pts overnight; intraday should not hold through close. |
| `_check_htf_invalidation` | BUY position with H4 AND D1 both BEARISH (or SELL with both BULLISH), trade ≥2h old, r_multiple < 0.0 | scalps, profitable trades (r≥0), TP1 hit, age <2h | A single H4 flip is noise. D1+H4 agreement = structural rotation. **Current state: fires 0/19 historical trades — quiet but no false positives.** |

**HTF invalidation history:**
- Original: H4 max/min over fixed 5-bar windows. Killed ETH +$38 on 0.34% lower-high tie 2026-04-26.
- `2c80deb`: 0.5% threshold + breakeven exempt (immediate stop-bleed).
- `f4cd7df`: replaced crude logic with `analysis.structure` (proper ICT swing detection).
- `15b4ed2`: extended to multi-timeframe (D1+H4 both required).
- `6ce7d76`: backtest harnesses (`verify_mtf_invalidation.py`, `verify_d1_only_invalidation.py`).

### 4.4 Risk gates (in `bridge/risk_bridge.py`)

| Gate | Trigger | Action |
|------|---------|--------|
| Position cap | ≥5 open positions | Block new |
| Same-class cap | ≥4 risk-on (or ≥4 risk-off) | Block new in that class |
| BTC/ETH contradiction | new BUY+SELL on BTC vs ETH | Block (correlation ~0.85, opposite = guaranteed loser unless SMT) |
| DXY exposure | net signed DXY exposure > MAX_DXY_EXPOSURE and reinforcing | Block |
| Daily trade count | dynamic 3/5/7 by Grade A signal volume | Block when reached |
| Per-symbol loss cooldown | 1 loss → 2h cooldown (50% size after); 2+ losses → 4h | Block during cooldown |
| Global loss cooldown | any loss → 60 min global no-new | Block during cooldown |
| Account heat | 3 wins → 0.75x size, 5 wins → 0.5x size | Reduce sizing |
| VIX risk multiplier | VIX 25-35 → 0.75x, VIX >35 → 0.5x | Reduce sizing |
| News blackout | NFP/CPI/FOMC ±45min, other high-impact ±30min | SKIP |
| Kill switch | 3 consecutive losses | Disable trading until reset |
| Daily loss kill | -2% from daily start balance | Disable trading until next day |
| MT5 dedup | duplicate ticket detected at order time | Block |
| Entry staleness | price moved >0.5x SL distance past entry | Block |

---

## 5. Bias engine

The bias engine answers: "for symbol X, what direction is the market on timeframe Y?" It's used in two places:

1. **Pre-trade filtering** (`bridge/ict_pipeline.py:425-478`) — W1/D1/H4 alignment scored as +5 synergy, -4 conflict; HTF Alignment Gate blocks counter-H4 entries.
2. **Post-entry invalidation** (`bridge/live_executor_adapter.py:_check_htf_invalidation`) — position closed if D1 AND H4 both oppose direction.

### 5.1 Implementation

```python
# In ict_pipeline.py and live_executor_adapter.py
swings = detect_swings(df, lookback=N)              # confirmed pivots (symmetric window)
_, events = classify_structure(swings, df=df)        # emits BOS / CHoCH on close-confirmed breaks
bias = get_current_bias(events)                      # majority-rule with CHoCH protection
```

**Source:** `analysis/structure.py` in trading-ai-v2 (imported via `ensure_trading_ai_path()`).

### 5.2 `get_current_bias` rules

Looks at last 5 structure events:
1. If most recent event is a CHoCH and CHoCH direction aligns with dominant BOS → return CHoCH direction (strong confirmation)
2. If CHoCH opposes dominant BOS → return NEUTRAL (ambiguous, prevents counter-trend bounces)
3. No CHoCH → return dominant BOS direction only if `bull_bos >= bear_bos + 2` (or vice versa). Otherwise NEUTRAL.

This means **NEUTRAL is the default** when there's no clear structural break. That's by design — it's better to abstain than mis-call a chop period.

### 5.3 Per-TF tuning (matches pre-trade pipeline)

| TF | bar count | swing lookback | rationale |
|----|-----------|----------------|-----------|
| H4 | 100 (~17 days) | 5 | enough swings for confident verdicts |
| D1 | 60 (~2 months) | 3 | macro structure, fewer swings need lower lookback |
| W1 | 30 (~7 months) | 2 | very macro, fewest data points |

### 5.4 Multi-timeframe usage in invalidation

A single H4 flip is normal market noise (consolidation, retraces). Invalidation requires **both** H4 AND D1 to oppose direction. W1 is read for context but not used as a close trigger (too slow to be actionable; positions hit SL/TP first).

**Current live readings (2026-04-26):**

| Symbol | H4 | D1 | W1 |
|--------|----|----|----|
| ETHUSD | BULLISH | BULLISH | NEUTRAL |
| SOLUSD | BULLISH | NEUTRAL | BEARISH |
| GER40 | BULLISH | BULLISH | BEARISH |
| BTCUSD | BULLISH | BULLISH | BEARISH |
| EURUSD | BEARISH | NEUTRAL | BEARISH |
| XAUUSD | NEUTRAL | BEARISH | NEUTRAL |

### 5.5 Where to tune

- Bar counts and lookbacks: `bridge/live_executor_adapter.py:_get_tf_bias` `tf_config` dict
- Invalidation threshold (currently "both H4 AND D1 must oppose"): `_check_htf_invalidation`, `opposes_h4 and opposes_d1`
- Profitability exempt threshold (currently `r >= 0.0`): same function, the exemption block

### 5.6 What was tried and rejected

- **Crude max/min over 5-bar window** (original): false BEARISH on a 0.34% lower-high tie. Killed ETH +$38 trade. Rejected.
- **0.5% threshold over crude logic** (`2c80deb`): noise floor only, structurally still wrong. Superseded.
- **D1-alone invalidator** (backtest 2026-04-26): net +$255 across 19 trades but kills 2 winners (ETH SELL -$256 worst). Net positive but high variance on n=19. Rejected pending more data.

---

## 6. State and persistence

### 6.1 `bridge_safety_state.json` (the critical one)

Single JSON file at repo root. Loaded at startup, written on every state change. Survives bridge restarts.

```json
{
  "daily_trade_count": 1,
  "daily_trade_date": "2026-04-26",
  "global_loss_cooldown_until": null,
  "symbol_loss_cooldowns": { "SOLUSD": "..." },
  "consecutive_losses": 0,
  "consecutive_wins": 1,
  "grade_a_signals_today": 65,
  "symbol_loss_counts": { ... },
  "kill_switch_triggered": false,
  "kill_switch_date": "",
  "last_updated": "...",
  "trailing_sl_by_ticket": {
    "<ticket>": {
      "trailing_sl": 23948.61,
      "tp1_hit": false,
      "trail_desync": false,
      "desired_sl": 23948.61,
      "tp_price": 25173.98,
      "tp2_price": 25651.47,
      "entry_price": 24151.6,
      "ict_grade": "B",
      "ict_score": 100.0,
      "trade_type": "swing",
      "risk_pct": 0.004,
      "opened_at": "...",
      "reasoning": "..."
    }
  }
}
```

**What persists:**
- Daily counters (trade count, Grade A count, balance, P&L)
- Cooldowns (global + per-symbol)
- Kill switch (date-bound)
- Per-position TP1/TP2/SL/grade/reasoning — required because for two-tier TP trades, MT5's `tp` field is intentionally 0 (bridge manages TP1 partial close + TP2 runner internally). Without this, restart would silently disable TP management.

**Bug history:** before commit `e5c2417` (2026-04-24), only `trailing_sl` and `tp1_hit` persisted. On restart, `tp_price`/`tp2_price`/`reasoning`/`grade` were lost. The bridge would adopt the position with TPs zeroed — appearing to "strip" them. Fixed by persisting all fields needed for adoption.

### 6.2 Session store

Path: `~/.tradingview-mcp/sessions/YYYY-MM-DD.json`. Append-only log of every analysis cycle, every decision, every trade event. Massive (multi-MB per active day). Source of truth for backtests.

Schema:
```
{
  "date": "2026-04-26",
  "started_at": "...",
  "analyses": [...],     // every cycle's per-symbol scoring
  "decisions": [...],    // every Claude decision (BUY/SELL/SKIP/HOLD)
  "trades": [...],       // OPEN / CLOSE events
  "account_snapshots": [...],  // balance/equity over time
  "summary": {...}
}
```

### 6.3 What does NOT persist

- In-memory caches (Claude decision cache, MT5 H4 cache). Rebuilt on restart.
- Live position objects (recreated by `position_manager.reconcile_mt5_on_startup()` reading from MT5).
- The current session store file (it's append-only — no need to persist a "where did I leave off" pointer).

### 6.4 What lives in MT5 (broker-side)

- Open positions with their actual SL price
- For single-tier TP trades: TP price (e.g. EA strategies that fire-and-forget)
- For two-tier TP trades: MT5's `tp` field is **0** — bridge holds TP1/TP2 internally

If the bridge is permanently lost (corrupt state file, code deleted), **MT5 still has SL set on every open position**. Worst case is missing TP fires for two-tier trades, which means TP2 (runner) wouldn't auto-close — but SL still protects downside.

---

## 7. Risk model

### 7.1 FTMO compliance constants

| Constraint | FTMO limit | Bridge limit (more conservative) |
|------------|-----------|----------------------------------|
| Daily loss | 5% | 2% (kill switch) |
| Total drawdown | 10% | 4% (kill switch) |
| Max positions | none | 5 |
| Risk per trade | none specified | 0.5% (Grade A), 0.25% (Grade B) |

### 7.2 Position sizing flow

```
base_risk = grade_risk[grade]            # 0.005 (A), 0.0025 (B)
× heat_mult                              # 1.0 / 0.75 / 0.5 by consecutive wins
× vix_mult                               # 1.0 / 0.75 / 0.5 by VIX bucket
× per_symbol_override (rules.json)       # e.g. crypto might be 0.5x
× cooldown_size_mult                     # 0.5x after 1 loss in cooldown window
= effective_risk_pct

lot_size = (account_balance × effective_risk_pct) / (sl_distance × pip_value)
```

### 7.3 Dynamic daily trade cap

Computed from Grade A signal count today:

| Grade A signals | Daily cap |
|-----------------|-----------|
| 0-9 | 3 |
| 10-19 | 5 |
| 20+ | 7 |

### 7.4 Cooldown rules

| Trigger | Cooldown |
|---------|----------|
| Any loss | 60 min global no-new |
| 1 loss on symbol | 2h symbol cooldown, 50% size after |
| 2+ losses on symbol | 4h symbol cooldown |
| 3 consecutive losses (any symbol) | Kill switch — manual reset required |
| Daily P&L ≤ -2% | Kill switch — auto-resets next trading day |

### 7.5 Account state right now

Read from `bridge_safety_state.json` (most recent values):
- Balance: $96,200 / Equity: ~$96,191
- Open: 2 positions (GER40 +$144, SOL ~flat)
- Day P&L: small loss
- Consecutive wins: 1
- Kill switch: not triggered

---

## 8. Decision history

Most-recent first. Each entry: commit / what / rationale.

| Date | Commit | Change | Why |
|------|--------|--------|-----|
| 2026-04-26 | `15b4ed2` | HTF invalidation requires D1+H4 confirmation | Single H4 flip is noise; D1+H4 agreement is structural |
| 2026-04-26 | `f4cd7df` | Replaced crude H4 bias with `analysis.structure` engine | Pre-trade and post-entry now use one bias method |
| 2026-04-26 | `2c80deb` | Threshold + breakeven exempt for HTF invalidation | Stop-bleed for ETH +$38 false-positive kill |
| 2026-04-26 | `6ce7d76` | Added MTF invalidation backtest harnesses | Verify new gates against 19-trade history |
| 2026-04-25 | `5449baf` | Position cap 3 → 5 (with risk-on sub-cap of 4) | Demo challenge = data-collection mode |
| 2026-04-25 | `f502a00` | Reverted SOL phrase gates from `ed6469b` | MT5 broker truth: SOL was manual close at -$120, not -$613 SL |
| 2026-04-25 | `9211a74` | SOL reclassification scenario harness | Vet phrase additions against broker-verified P&L |
| 2026-04-24 | `e5c2417` | Persist TP/TP2/grade/reasoning on restart | Bridge restarts were stripping TP management |
| 2026-04-24 | `1a8296b` | IPDA-extreme phrase widened (drop "extreme" suffix) | GBPJPY SELL slipped through with "ipda 20d high" |
| 2026-04-24 | `303d18d` | Added opposing-sweep + IPDA-extreme reasoning gates | 4/5 losers had Claude reasoning admitting gate violations |
| 2026-04-22 | `49420d9` | ICT 2022 full implementation, reasoning gate | Hard gates for kill zone, DOL, sweep significance |
| 2026-04-21 | (multiple) | `bridge_safety_state.json` persistence | 27-trade blowout — restart wiped all safety state |
| 2026-04-19 | `45cecbf` | FTMO lot sizing, wider SLs, ICT KB | Crypto SL too tight got swept |

### Patterns that emerge from this log

1. **"Restart wipes state" was the most expensive single bug.** $2,345 lost in one day. Lesson: any in-memory counter has to persist.
2. **Phrase gates require broker-truth backtesting.** Multiple phrase additions (`"accumulation phase"`, `"no active kill zone"`, the SOL set) were calibrated against session-store P&L that turned out to be wrong. Always verify with `mt5.history_deals_get()`.
3. **Pre-trade and post-entry decision logic must use the same engine.** Two bias engines = guaranteed disagreement = killed valid trades.
4. **Conservative defaults beat clever defaults.** Profitability exempt (r≥0), 2-hour age skip, requires-both-TFs invalidation — all of these "didn't fire" on 19-trade backtest, but each one prevents a known historical kill.

---

## 9. Runbooks

### 9.1 Start the bridge

```bash
# Double-click on Windows:
~/Desktop/Launchers/TradingView Bridge - Auto Trade.bat

# Or directly:
~/tradingview-mcp-jackson/start_bridge_mt5only.bat   # MT5-primary mode (current default)
~/tradingview-mcp-jackson/start_bridge_auto.bat       # MT5 + TV fallback
```

The .bat parent loops on exit codes ≠ 2 — bridge auto-restarts on crashes (10s delay).

### 9.2 Stop the bridge cleanly

```powershell
# Find both the .bat parent and the python child
$pids = Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match "auto_trade|start_bridge_mt5only" -and $_.Name -in @('python3.12.exe','python.exe','cmd.exe') } |
  Select-Object -ExpandProperty ProcessId

foreach ($p in $pids) { Stop-Process -Id $p -Force }
```

Killing only the python child causes the .bat to relaunch in 10s. Kill the .bat parent (cmd.exe matching `start_bridge_mt5only`) too.

### 9.3 Restart safely with open positions

1. Confirm `bridge_safety_state.json` has all current tickets in `trailing_sl_by_ticket` with non-zero `tp_price`. Without this, two-tier TP management gets stripped on restart.
2. Stop the bridge (§ 9.2)
3. Verify MT5 still shows positions with broker-side SL set (broker fires SL even when bridge is dead)
4. Restart (§ 9.1)
5. Watch the .bat console for:
   - `[SAFETY] Cached trailing-SL state for N position(s); will restore on MT5 adopt`
   - `[MT5_RECON] ADOPTED #<ticket> ...` for each surviving position with TP1/TP2 visible

If any position is missing from the ADOPTED lines, set TPs manually on MT5 as a fallback.

### 9.4 Add a phrase gate

1. Edit `bridge/claude_decision.py:_REASONING_HARD_GATE_PHRASES` tuple.
2. Run `python verify_new_gates.py` — every existing trade replays through the new phrase list.
3. Verify: net deployment value increases AND zero winners blocked.
4. If a phrase catches a winner → remove and document why.
5. If a phrase only catches one specific trade → suspect over-fitting; require a second occurrence before keeping.
6. Commit with the backtest output in the commit body.

**Important:** never calibrate phrases against session-store P&L without verifying via MT5:
```python
import MetaTrader5 as mt5
mt5.initialize()
hist = mt5.history_deals_get(position=<ticket>)
# check actual fill price + actual P&L
```

The SOL ed6469b incident was caused by trusting a session-store CLOSE event that had a wrong exit price.

### 9.5 Add a symbol

1. Edit `rules.json`:
   - Add to `symbols` list
   - Optionally add per-symbol risk override
2. Edit `bridge/config.py:SYMBOL_MAP` — add TV name → internal name mapping
3. If FTMO uses a different symbol name, also edit the FTMO mapping section
4. Restart bridge — it picks up new watchlist on startup

### 9.6 Debug a missed signal or unexpected SKIP

```bash
# Find the symbol's most recent decision in the session store
python -c "
import json
from pathlib import Path
p = Path.home() / '.tradingview-mcp' / 'sessions' / '2026-04-26.json'
data = json.load(open(p))
for d in data['decisions'][-30:]:
    if 'SOLUSD' in d.get('symbol',''):
        print(d['timestamp'], d['action'], d.get('grade'), '-', d.get('reasoning','')[:200])
"
```

Look for the SKIP reason. If the decision_writer is logging "Pre-gate: KILL ZONE GATE" or similar, that's a deliberate block. If you see Grade A repeatedly skipping for the same reason, the gate may be too tight.

### 9.7 Investigate a closed trade

```python
import MetaTrader5 as mt5
from datetime import datetime, timezone, timedelta
mt5.initialize()

# Position-level
deals = mt5.history_deals_get(position=<ticket>)
for d in deals:
    print(d.ticket, d.entry, d.price, d.profit, datetime.fromtimestamp(d.time, timezone.utc))

# Surrounding price action
rates = mt5.copy_rates_range('SOLUSD', mt5.TIMEFRAME_M1, start, end)
```

Note: MT5 deal `time` is in **broker server time** (FTMO is GMT+3 in DST). Subtract 3h for UTC.

### 9.8 Run backtests against historical trades

```bash
# Reasoning-gate backtest (every CLOSE in 19-trade history through current phrase list)
python verify_new_gates.py

# MTF invalidation backtest
python verify_mtf_invalidation.py

# D1-alone alternative (kept as comparison)
python verify_d1_only_invalidation.py
```

Output format: per-trade BLOCK/PASS with verdict; summary with money-saved / money-cost / net.

### 9.9 Common failures and fixes

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Bridge starts but no decisions logged | TV CDP not on port 9222 | `start_bridge_mt5only.bat` skips TV check |
| Every cycle: "DATA_UNAVAILABLE" | Symbol disabled at broker (weekend, market closed) | Wait or stop bridge if extended closure |
| Position shows `tp=0` on MT5 but bridge persists TP1/TP2 | Two-tier trade — bridge manages internally | Normal; verify in `bridge_safety_state.json` |
| Bridge auto-closed a winning trade | Likely `_check_htf_invalidation` | Check log for `[HTF INVALIDATION]`. Tune in `_get_tf_bias` or exempt rules. |
| Telegram alerts stopped | Token expired or chat_id wrong | Check `.env` for `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` |
| Bridge process running but no log activity | MT5 connection lost | Check `logs/trading.log` for `mt5_connect_attempt` failures; restart MT5 terminal |

---

## Appendix: file count & line count

```
bridge/                29 .py files
bridge/strategy_knowledge/  51 ICT concept JSON cards + cross_correlations.json
verify_*.py            3 backtest harnesses at repo root
*.md                   6 docs at repo root + 4 in archive/
```

---

> If you change anything in this document, also update the relevant code's docstring or comments. Architecture docs that drift from code are worse than no docs.
