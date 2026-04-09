# Strategy Master Plan: Auto-Trading Decision Intelligence Upgrade

> **Goal:** Integrate 33 ChartFanatics strategies + MT5 backtest insights + ICT concepts into the
> TradingView MCP Jackson auto-trading pipeline to produce higher-conviction, data-backed trade decisions.
>
> **Created:** 2026-04-08
> **Project:** tradingview-mcp-jackson (bridge/ pipeline)

---

## Table of Contents

1. [Current System Audit](#1-current-system-audit)
2. [Strategy Knowledge Base](#2-strategy-knowledge-base)
3. [MT5 Backtest Intelligence](#3-mt5-backtest-intelligence)
4. [Gap Analysis: What's Missing](#4-gap-analysis-whats-missing)
5. [Unified Strategy Taxonomy](#5-unified-strategy-taxonomy)
6. [Implementation Plan](#6-implementation-plan)
7. [New Scoring Architecture](#7-new-scoring-architecture)
8. [Per-Symbol Strategy Profiles](#8-per-symbol-strategy-profiles)
9. [Risk & Position Sizing Upgrade](#9-risk--position-sizing-upgrade)
10. [File Structure](#10-file-structure)

---

## 1. Current System Audit

### What We Have (Working)

| Component | Status | Details |
|-----------|--------|---------|
| **ICT Pipeline** | Active | 7-dimension scoring (structure, liquidity, OB, FVG, session, OTE, SMT) |
| **EA Ensemble** | Active | 33 strategies via trading-ai-v2 EAEngine + cluster voting |
| **ICT Strategies** | Active | 4 strategies (Reversal, Continuation, Liquidity Run, OB Bounce) |
| **Claude Decision Layer** | Active | Pre-gate → Model routing → Post-gate with R:R filter |
| **Risk Bridge** | Active | FTMO compliance (5% daily, 10% total DD, 2 positions max) |
| **Paper Executor** | Active | Full simulation with trailing SL/TP |
| **Signal Flow** | Active | 60s analysis loop → 30s position management |

### Current Watchlist
`BTCUSD`, `ETHUSD`, `SOLUSD` (crypto only)

### Current ICT Scoring Weights
```
structure: 25% | liquidity: 20% | order_block: 15% | fvg: 15%
session: 10%   | ote: 10%       | smt: 5%
```

### Current Grade Thresholds
`A: >=80 | B: 65-79 | C: 50-64 | D: 35-49 | INVALID: <35`

---

## 2. Strategy Knowledge Base

### 2A. ICT Core Concepts (Already Implemented)

These are the foundation — all ChartFanatics strategies that use ICT concepts map back to these:

| Concept | Implementation | Used By |
|---------|---------------|---------|
| **Market Structure (BOS/CHoCH/MSS)** | `analysis.structure` | Structure+OTE, AMD, PO3+OTE+ADR, Intraday Liq |
| **Order Blocks** | `analysis.order_blocks` | Structure+OTE, OB Bounce, PO3+OTE+ADR |
| **Fair Value Gaps (FVG/IFVG)** | `analysis.fvg` | AMD, ICT Continuation, Trident/High RR |
| **Liquidity Sweeps** | `analysis.liquidity` | Liquidity Strategy, AMD, Intraday Liq |
| **Kill Zones** | `analysis.sessions` | PO3+OTE+ADR, High RR, Intraday Liq |
| **OTE (Fibonacci 0.618-0.786)** | Scorer | PO3+OTE+ADR, Structure+OTE |
| **SMT Divergence** | `analysis.smt` | SMT Divergence+PO3 |

### 2B. ChartFanatics Strategies — Full Catalog (33 Strategies)

Organized by **strategy archetype** (how they make decisions):

#### TIER 1: ICT-Native (Directly map to existing ICT pipeline)

| # | Strategy | Core Concept | Key Entry Signal | Applicable Markets |
|---|----------|-------------|-----------------|-------------------|
| 1 | **Structure + OTE** (Trader Mayne) | HTF structure break → LTF entry at OTE | MSB + liquidity sweep + breaker block in discount/premium | Futures, Forex, Crypto |
| 2 | **SMT Divergence + PO3** (Trader Kane) | PO3 phases + SMT between NQ/ES | Manipulation at 10am ET + SMT divergence + inversion zone | Futures, Crypto |
| 3 | **PO3, OTE + ADR** (NBB Trader) | Market Maker Model + Fibonacci | PD Array touch + SMR confirmation + OTE retracement (0.50-0.79) | Forex |
| 4 | **AMD Model** (Tanja Trades) | Accumulation → Manipulation → Distribution | Displacement leaving FVG → retrace into FVG | Futures |
| 5 | **Liquidity Strategy** (Marco Trades) | Buy below lows, sell above highs | Liquidity taken + internal structure confirms trap | Futures, Forex, Crypto |
| 6 | **Intraday Liquidity & Volatility** (Jade Cap) | Session liquidity raids | PDH/PDL/Asian/London sweep + MSS/FVG confirm on LTF | Forex, Futures |
| 7 | **Unique High RR / Trident** (TG Capital) | FVG + doji at London kill zone | 3-candle FVG + doji wicking into FVG 50% + EMA stack | Forex (6 pairs + XAUUSD) |

**Integration approach:** These 7 strategies can be expressed as **scoring modifiers** within the existing ICT pipeline. They don't need new analysis modules — they need new *combination rules* for existing signals.

#### TIER 2: Price Action / Structure (Need minor analysis additions)

| # | Strategy | Core Concept | Key Entry Signal | Applicable Markets |
|---|----------|-------------|-----------------|-------------------|
| 8 | **Break & Retest** (Desiano) | Clean break of key level → retest confirm | Level break with momentum + pullback + confirmation candle | Stocks, Futures |
| 9 | **Trendline Break Pocket** (Crooks) | Trendline break after touching HTF key level | HTF key level reaction + trendline break + swing break + 21 EMA pullback | Forex |
| 10 | **Trendline Strategy** (Tori Trades) | Trendline bounce or break with safety line | 2-3 touchpoint trendline + bounce/break confirmation | All |
| 11 | **Support & Resistance** (Brando) | HTF S/R levels + catalyst alignment | Price at major S/R + FOMC/CPI catalyst + confirmation | Options |
| 12 | **5 Stage Framework** (Umar Ashraf) | Trader development stages | *Meta-framework, not tradeable signals* | — |

**Integration approach:** Need a `trendline` detection module and enhanced `key_level` scoring. The Break & Retest pattern is partially covered by OB analysis but needs explicit level-break detection.

#### TIER 3: Order Flow / Volume Profile (Need new data sources)

| # | Strategy | Core Concept | Key Entry Signal | Applicable Markets |
|---|----------|-------------|-----------------|-------------------|
| 13 | **OrderFlow Trading** (Rosato) | DOM/heatmap/footprint reading | Absorption + liquidity wall + stop run patterns | Futures, Stocks |
| 14 | **Volume Profile** (Forrest Knight) | High/Low Value Areas + POC | Price touches VP edge + signal candle at HVA/LVA boundary | Futures, Options |
| 15 | **Low Volume Node** (Rosato) | Volume-by-price thin zones | Price returns to LVN + order flow confirmation | Futures |
| 16 | **Auction Market Theory** (Cimitan) | Balance/imbalance cycles | Failed auction (absorption) or breakout with acceptance | Futures |
| 17 | **Market Auction Theory** (Dhall) | Same concept, different instructor | Balance → imbalance → new balance | Futures |
| 18 | **Market DNA** (Awtani) | Tape reading + DNA zones | Aggressive buyer/seller absorption at DNA level | Futures, Stocks |

**Integration approach:** These require **volume profile data** from TradingView. We can extract this via `data_get_pine_lines` from Volume Profile indicators already on charts, or add VP-specific Pine scripts. The order flow concepts (absorption, stop runs) overlap heavily with existing ICT liquidity analysis.

#### TIER 4: Momentum / Mean Reversion (Complementary signals)

| # | Strategy | Core Concept | Key Entry Signal | Applicable Markets |
|---|----------|-------------|-----------------|-------------------|
| 19 | **Mean Reversion** (Breitstein) | Extreme move → revert to mean | Rapid acceleration + volume spike + structure break confirms reversal | Stocks |
| 20 | **Episodic Pivot** (Bonde) | Neglected stock + catalyst = repricing | Heavy volume gap + follow-through on Day 1 | Stocks |
| 21 | **Parabolic Short** (Stamatoudis) | Extended parabolic move → short | *Stock-specific, less applicable to crypto/forex* | Stocks |
| 22 | **First Red Day** (Williams/Temiz) | Momentum stock's first red day after run | Gap + first red close after multi-day run | Stocks |
| 23 | **Measured Move Trend** (Silfrain) | Continuation after measured pullback | *Details not extracted* | Futures, Stocks, Crypto |
| 24 | **Real Simple Strategy** (Hernandez) | Simplified price action | *Stock-focused* | Stocks |
| 25 | **Shorting Strategy** (Verma) | Short selling patterns | *Stock-focused* | Stocks |
| 26 | **Universal Strategy** (Traveling Trader) | Multi-market framework | *Generic framework* | Multiple |

**Integration approach:** Mean Reversion is the most useful here — it provides a **counter-signal** filter. When the ICT pipeline says "continuation" but price has moved parabolically away from the 20 EMA, mean reversion logic should reduce confidence or flag caution.

#### TIER 5: Systematic / Algorithmic (Meta-strategies)

| # | Strategy | Core Concept | Key Entry Signal | Applicable Markets |
|---|----------|-------------|-----------------|-------------------|
| 27 | **Algorithmic Strategy** (Taief) | Portfolio of 2-3 rule systems | No-code system building with max 2-3 rules per system | Stocks, Futures |
| 28 | **Futures Trading** (Crudele) | *Details not extracted* | — | Futures |
| 29 | **Options Masterclass** (Ashraf) | *Options-specific* | — | Options |
| 30 | **Full Psychology** (Tendler) | *Mental game, not signals* | — | — |
| 31 | **VIX Futures** (O'Neil) | *VIX-specific* | — | Futures |
| 32 | **Market Auction** (Valentini) | *Similar to #16* | — | Futures |
| 33 | **First Red Day** (Temiz variant) | *Similar to #22* | — | Stocks |

**Integration approach:** The Algorithmic Strategy's principle of "max 2-3 rules per strategy, avoid curve-fitting" is a **design principle** we should apply when building new strategy modules. The rest are either stock-specific, options-specific, or psychology frameworks.

---

## 3. MT5 Backtest Intelligence

### 3A. Data Overview

| Metric | Value |
|--------|-------|
| **Total optimization passes** | 375,094 |
| **Valid configurations (20+ trades)** | 226,202 |
| **Symbols tested** | 19 |
| **Primary EA** | TrendFollowingPurgeRevert_EA |
| **Secondary EA** | StructureOTE_EA |
| **Timeframes tested** | H1 (95.9%), H2 (4.1%) |
| **Date range** | 2020-2026 |
| **Report files** | 41 XML files, 491.7 MB |

### 3B. Top Performers by Symbol

| Rank | Symbol | Profit Factor | Sharpe Ratio | Max DD | Net Profit | Verdict |
|------|--------|:------------:|:------------:|:------:|:----------:|---------|
| 1 | **EURUSD** | 2.208 | **18.52** | 7.95% | $103,426 | **Best consistency** |
| 2 | **US30.cash** | 2.009 | **17.39** | 14.24% | $55,371 | **Best index** |
| 3 | **JP225.cash** | — | **9.39** | 0.90% | — | **Lowest drawdown** |
| 4 | **XAUUSD** | — | **9.57** | — | — | **Strong precious metal** |
| 5 | **UKOIL.cash** | — | **9.48** | — | — | **Stable commodity** |
| 6 | **XAGAUD** | **12.290** | 2.06 | 7.22% | $74,236 | **Highest PF** |
| 7 | **BCHUSD** | — | — | — | $104,918 | **Best crypto** |
| 8 | **ETHUSD** | 2.98 | — | — | — | **Strong crypto** |
| 9 | **SOLUSD** | 4.224 | — | — | — | **Undertested (1 file)** |
| 10 | **BTCUSD** | 2.27 | — | — | — | **Average crypto** |
| 11 | **COFFEE.c** | — | — | 41.94% | $7,630,472 | **Extreme profit, extreme DD** |

### 3C. Key Patterns from Backtest Data

**Pattern 1 — Forex/Index Dominance:**
EURUSD and US30 have Sharpe ratios 2x higher than anything else. The EA strategies are fundamentally *trend-following purge-revert* patterns — they work best on instruments with **clear institutional structure** (EURUSD = most liquid pair; US30 = most liquid index).

**Pattern 2 — Cross-Symbol Failure:**
The AI analysis found **0 cross-symbol winners** — parameters that work on BTCUSD don't work on ETHUSD. This means each symbol needs its own parameter set. The auto-trading system must maintain **per-symbol strategy profiles**.

**Pattern 3 — Timeframe Blindspot:**
95.9% of passes are H1. The system has barely tested H4 or D1. Since our ICT pipeline runs on H4/H1/M15, we're missing backtest validation for the primary bias timeframe.

**Pattern 4 — The EA Name Tells the Strategy:**
`TrendFollowingPurgeRevert_EA` = This is essentially the **AMD Model** (Accumulation → Manipulation → Distribution) implemented as an EA. The "purge" = liquidity sweep/manipulation, the "revert" = return to value/distribution.

**Pattern 5 — StructureOTE_EA on US30:**
This directly maps to the **Structure + OTE** ChartFanatics strategy. 54,674 trades on US30 with PF 1.254 / Sharpe 4.69 = high frequency, moderate edge.

### 3D. Actionable Insights for Auto-Trading

1. **Expand watchlist:** Add EURUSD, US30, XAUUSD (proven performers)
2. **Per-symbol parameters:** Load optimal EA parameters from `data/ai_analysis.json` per symbol
3. **Confidence boost:** When ICT pipeline + EA agree on a symbol where backtest PF > 2.0, boost confidence
4. **Backtest validation flag:** Track which strategy/symbol combos have backtest backing vs. untested
5. **H4 backtesting gap:** Prioritize running H4 optimizations for bias-timeframe validation

---

## 4. Gap Analysis: What's Missing

### 4A. Strategy Gaps (ChartFanatics vs. Current Implementation)

| Gap | Impact | Difficulty | Priority |
|-----|--------|-----------|----------|
| **No trendline detection** | Missing Break & Retest, Trendline Pocket signals | Medium | P1 |
| **No volume profile integration** | Missing VP, LVN, Auction Market signals | Medium | P1 |
| **No session-specific strategy routing** | Same strategy runs in Asian and NY (wrong) | Easy | P0 |
| **No mean reversion filter** | System can chase parabolic moves | Easy | P0 |
| **No EMA stack confirmation** | Missing Trident/High RR EMA filter | Easy | P1 |
| **No PO3 phase detection** | Accum/Manip/Dist phases not explicitly tracked | Medium | P1 |
| **No time-of-day filters** | Strategies like AMD need 9:50-10:10 ET window | Easy | P0 |
| **No ADR (Average Daily Range) tracking** | Can't measure if daily range is exhausted | Easy | P1 |
| **No Fibonacci auto-plotting** | OTE zone exists but no auto-fib levels for entries | Easy | P2 |
| **No absorption/delta detection** | Order flow strategies need footprint data | Hard | P3 |
| **No per-symbol parameter profiles** | Same weights for BTC and EURUSD (wrong) | Medium | P0 |
| **No backtest-informed confidence** | Decisions don't know if the strategy works on this symbol | Easy | P0 |

### 4B. Architecture Gaps

| Gap | Impact | Fix |
|-----|--------|-----|
| **Static ICT weights** | Same 25/20/15/15/10/10/5 for all symbols | Per-symbol weight profiles from MT5 data |
| **No strategy selection** | All 4 ICT strategies evaluated equally | Route by session + market condition |
| **No regime detection** | Trending vs. ranging vs. volatile not classified | Add regime classifier (ADX + ATR + structure) |
| **Watchlist too narrow** | Only 3 crypto symbols; best performers not included | Expand to 8-12 symbols across asset classes |
| **No historical performance tracking** | Can't learn which strategies work in practice | Track win/loss per strategy per symbol |

---

## 5. Unified Strategy Taxonomy

The combined system should organize strategies into **archetypes** that the decision engine can select from based on market conditions:

```
STRATEGY ARCHETYPES
===================

[REVERSAL]
  ├── ICT Reversal (Liquidity Sweep + CHoCH)         ← existing
  ├── AMD Distribution Phase                          ← ChartFanatics
  ├── Mean Reversion (extreme displacement)           ← ChartFanatics
  ├── Failed Auction (Auction Market Theory)          ← ChartFanatics
  └── Liquidity Grab Reversal                         ← ChartFanatics

[CONTINUATION]
  ├── ICT Continuation (BOS + FVG)                    ← existing
  ├── Trendline Bounce                                ← ChartFanatics
  ├── Structure + OTE (pullback to discount)          ← ChartFanatics
  ├── PO3 OTE + ADR (retracement to Fib)            ← ChartFanatics
  └── Break & Retest                                  ← ChartFanatics

[LIQUIDITY]
  ├── Liquidity Run (Displacement + Draw)             ← existing
  ├── Order Block Bounce                              ← existing
  ├── Intraday Liquidity & Volatility Model           ← ChartFanatics
  ├── Low Volume Node                                 ← ChartFanatics (needs VP data)
  └── Stop Run / Stop Hunt                            ← ChartFanatics (OrderFlow)

[TIMING]
  ├── Kill Zone Entry (existing session logic)        ← existing
  ├── Silver Bullet (M5 reversal at Asia open)        ← existing
  ├── London Kill Zone Trident (3am-6:30am ET)        ← ChartFanatics
  ├── NY 9:50-10:10 / 10:50-11:10 window            ← ChartFanatics (AMD)
  └── SMT Divergence (10am ET manipulation)           ← ChartFanatics

[CONFLUENCE FILTERS]  (not standalone — modify confidence of above)
  ├── Volume Profile edge alignment                   ← ChartFanatics
  ├── EMA Stack confirmation (5/9/13/21 + 200)       ← ChartFanatics (Trident)
  ├── Mean Reversion warning (extreme from 20 EMA)   ← ChartFanatics
  ├── Backtest performance multiplier                 ← MT5 data
  ├── Regime filter (trending/ranging/volatile)       ← New
  └── ADR exhaustion check                            ← ChartFanatics
```

---

## 6. Implementation Plan

### Phase 0: Quick Wins (No code changes to analysis modules)

**Goal:** Better decisions with existing infrastructure.

| Task | File(s) | Effort |
|------|---------|--------|
| 0.1 Expand watchlist to include EURUSD, US30, XAUUSD, UKOIL | `rules.json` | 5 min |
| 0.2 Add time-of-day filters to rules.json (AMD windows, kill zones) | `rules.json` | 15 min |
| 0.3 Add per-symbol bias overrides based on MT5 Sharpe rankings | `rules.json` | 30 min |
| 0.4 Add mean reversion warning to Claude decision prompt | `claude_decision.py` | 30 min |
| 0.5 Load `data/ai_analysis.json` top configs as confidence boosters | `orchestrator.py` | 1 hr |

### Phase 1: Strategy Knowledge Layer

**Goal:** A structured knowledge base that Claude (and future models) can use for better decisions.

| Task | File(s) | Effort |
|------|---------|--------|
| 1.1 Create `bridge/strategy_knowledge/` directory | New | 10 min |
| 1.2 Create `strategies.json` — all 33 strategies with rules, conditions, markets | New | 2 hr |
| 1.3 Create `symbol_profiles.json` — per-symbol optimal strategies + MT5 parameters | New | 1 hr |
| 1.4 Create `session_routing.json` — which strategies apply per session/time window | New | 1 hr |
| 1.5 Create `regime_strategies.json` — strategy selection by market regime | New | 1 hr |
| 1.6 Update `claude_decision.py` to include relevant strategy context in prompts | Edit | 2 hr |

### Phase 2: New Analysis Modules

**Goal:** Detect signals that current modules miss.

| Task | File(s) | Effort |
|------|---------|--------|
| 2.1 Add `analysis.trendlines` — auto-detect trendlines + breaks | New in trading-ai-v2 | 4 hr |
| 2.2 Add `analysis.regime` — ADX + ATR + structure regime classifier | New in trading-ai-v2 | 3 hr |
| 2.3 Add `analysis.volume_profile` — parse VP data from TradingView Pine | New in trading-ai-v2 | 4 hr |
| 2.4 Add `analysis.ema_stack` — EMA 5/9/13/21/200 alignment scoring | New in trading-ai-v2 | 2 hr |
| 2.5 Add `analysis.adr` — Average Daily Range tracking + exhaustion | New in trading-ai-v2 | 2 hr |
| 2.6 Add `analysis.po3` — PO3 phase detection (Accumulation/Manipulation/Distribution) | New in trading-ai-v2 | 3 hr |
| 2.7 Add `analysis.mean_reversion` — Distance from 20 EMA + Bollinger + volume spike | New in trading-ai-v2 | 2 hr |

### Phase 3: Enhanced ICT Pipeline

**Goal:** Upgrade the scoring system with new signals.

| Task | File(s) | Effort |
|------|---------|--------|
| 3.1 Expand ICT scoring to 12 dimensions (add trendline, regime, VP, EMA, ADR) | `ict_pipeline.py` | 3 hr |
| 3.2 Per-symbol weight profiles loaded from `symbol_profiles.json` | `ict_pipeline.py` | 2 hr |
| 3.3 Session-based strategy routing (only evaluate relevant strategies per session) | `orchestrator.py` | 2 hr |
| 3.4 PO3 phase overlay — detect current phase, route to matching strategy archetype | `ict_pipeline.py` | 3 hr |
| 3.5 Backtest confidence multiplier from MT5 data | `orchestrator.py` | 2 hr |

### Phase 4: Performance Learning Loop

**Goal:** The system improves itself over time.

| Task | File(s) | Effort |
|------|---------|--------|
| 4.1 Track win/loss per strategy per symbol per session in `session_store.py` | `session_store.py` | 2 hr |
| 4.2 Weekly strategy performance report (which combos are working) | New script | 3 hr |
| 4.3 Dynamic weight adjustment — increase weights for winning combos | New module | 4 hr |
| 4.4 A/B testing framework — paper trade new strategies alongside current | New module | 4 hr |

---

## 7. New Scoring Architecture

### Current (7 dimensions, static weights)
```
Total = structure(25%) + liquidity(20%) + OB(15%) + FVG(15%) + session(10%) + OTE(10%) + SMT(5%)
```

### Proposed (12 dimensions, per-symbol dynamic weights)

```
CORE ICT (60% base weight — proven edge)
├── structure:     20%  (was 25% — still primary but shared)
├── liquidity:     15%  (was 20% — validated by MT5)
├── order_block:   10%  (was 15%)
├── fvg:           10%  (was 15%)
└── ote:            5%  (was 10%)

TIMING & CONTEXT (20% base weight — high impact per ChartFanatics)
├── session:       8%   (was 10% — remains critical)
├── po3_phase:     5%   (NEW — Accumulation/Manipulation/Distribution detection)
├── kill_zone:     4%   (NEW — specific time windows from AMD, Trident, PO3)
└── smt:           3%   (was 5% — limited pairs)

CONFLUENCE FILTERS (20% base weight — new from ChartFanatics + MT5)
├── regime:        5%   (NEW — trending/ranging/volatile classifier)
├── ema_stack:     4%   (NEW — 5/9/13/21/200 alignment)
├── volume_profile: 4%  (NEW — HVA/LVA/POC proximity)
├── adr_status:    3%   (NEW — range exhaustion check)
├── trendline:     2%   (NEW — trendline proximity/break)
└── backtest_pf:   2%   (NEW — MT5 profit factor for this symbol)
```

**Per-symbol overrides example:**

```json
{
  "EURUSD": {
    "structure": 0.22, "liquidity": 0.18, "session": 0.12,
    "regime": 0.08, "backtest_pf": 0.05,
    "notes": "Sharpe 18.52 in backtests. Heavily session-dependent. Boost session + regime."
  },
  "BTCUSD": {
    "structure": 0.20, "liquidity": 0.15, "fvg": 0.12,
    "ote": 0.08, "smt": 0.05,
    "notes": "24/7 market. Kill zones less relevant. SMT with ETHUSD is valuable."
  },
  "XAUUSD": {
    "structure": 0.18, "liquidity": 0.20, "volume_profile": 0.08,
    "session": 0.10, "adr_status": 0.06,
    "notes": "Highly volatile. ADR exhaustion critical. London session dominant."
  }
}
```

---

## 8. Per-Symbol Strategy Profiles

Based on MT5 backtests + ChartFanatics strategy fit:

### EURUSD (Best Overall — Sharpe 18.52)
- **Primary strategies:** PO3+OTE+ADR (designed for Forex), Trendline Break Pocket, Structure+OTE
- **Best sessions:** London Open (2-5am ET), NY Open (7-10am ET)
- **Regime fit:** Trending (PF 2.208 from trend-following EA)
- **MT5 optimal params:** Load from `data/ai_analysis.json` row with symbol=EURUSD, top Sharpe
- **Risk:** Standard (1% Grade A, 0.5% Grade B)

### US30.cash (Best Index — Sharpe 17.39)
- **Primary strategies:** AMD Model (9:50-10:10 ET), Break & Retest, Auction Market Theory
- **Best sessions:** NY Open exclusively
- **Regime fit:** Trending + momentum bursts
- **MT5 optimal params:** StructureOTE_EA parameters (54K trades validated)
- **Risk:** Conservative (0.75% Grade A) — 14.24% historical DD

### BTCUSD (Moderate — PF 2.27)
- **Primary strategies:** ICT Reversal, Liquidity Run, Structure+OTE
- **Best sessions:** All (24/7 market, but NY and London overlap strongest)
- **Regime fit:** All regimes — needs regime classifier to select strategy
- **MT5 optimal params:** Load from backtest data
- **Risk:** Standard (1% Grade A)

### ETHUSD (Strong — PF 2.98)
- **Primary strategies:** Same as BTCUSD + SMT Divergence (with BTC correlation)
- **SMT pair:** BTCUSD — when ETH makes new high but BTC doesn't = bearish divergence
- **Risk:** Standard

### SOLUSD (Undertested — PF 4.224 from 1 file)
- **Primary strategies:** ICT Continuation, Liquidity Run
- **NOTE:** Only 10,198 passes tested. Needs more backtesting before full confidence.
- **Risk:** Conservative (0.5% Grade A) until more data

### XAUUSD (Strong — Sharpe 9.57)
- **Primary strategies:** Intraday Liquidity Model, Trident/High RR (London Kill Zone), AMD
- **Best sessions:** London (3-6:30am ET for Trident), NY Open
- **Regime fit:** Highly volatile — ADR exhaustion filter critical
- **Risk:** Conservative (0.75% Grade A) — volatile instrument

### UKOIL.cash (Stable — Sharpe 9.48)
- **Primary strategies:** Trend continuation, Break & Retest
- **Best sessions:** London + NY overlap
- **Regime fit:** Trending (commodity cycles)
- **Risk:** Standard

---

## 9. Risk & Position Sizing Upgrade

### Current System
```
Grade A: 1.0% risk | Grade B: 0.5% risk | Grade C: 0.25% risk
Fixed across all symbols.
```

### Proposed System

**Base risk by grade:**
```
Grade A: 1.0% | Grade B: 0.6% | Grade C: 0.3%
```

**Multipliers (stack multiplicatively):**

| Factor | Multiplier | Source |
|--------|-----------|--------|
| MT5 Sharpe > 10 for this symbol | 1.2x | Backtest data |
| MT5 Sharpe > 15 for this symbol | 1.4x | Backtest data |
| ICT + EA agree on direction | 1.15x | Confluence |
| In optimal session for this symbol | 1.1x | Session routing |
| Regime matches strategy archetype | 1.1x | Regime classifier |
| ADR > 80% exhausted | 0.5x | ADR filter |
| Mean reversion warning (>2 SD from 20 EMA) | 0.6x | Mean reversion |
| No backtest data for this symbol | 0.7x | Data gap penalty |
| 1 consecutive loss | 0.8x | Risk management |
| 2 consecutive losses | 0.0x (stop) | Risk management (existing) |

**Position size cap:** Never exceed 2% risk per trade regardless of multipliers.

### ATR-Based Stop Loss
Currently uses fixed pips. Upgrade to:
```
SL distance = max(ATR(14) * 1.5, minimum_distance)
TP distance = SL distance * target_RR  (where target_RR >= 2.0 for Grade A, >= 2.5 for Grade B)
Lot size = (account_equity * risk_percent) / (SL_distance * pip_value)
```

---

## 10. File Structure

```
tradingview-mcp-jackson/
├── bridge/
│   ├── strategy_knowledge/                    ← NEW DIRECTORY
│   │   ├── strategies.json                    ← All 33 strategies with rules
│   │   ├── symbol_profiles.json               ← Per-symbol configs + MT5 params
│   │   ├── session_routing.json               ← Time-based strategy selection
│   │   ├── regime_strategies.json             ← Regime-based strategy selection
│   │   ├── archetypes.json                    ← Strategy archetype definitions
│   │   └── mt5_insights.json                  ← Distilled MT5 backtest findings
│   │
│   ├── strategy_knowledge_loader.py           ← NEW: Loads + serves strategy context
│   ├── regime_classifier.py                   ← NEW: Trending/ranging/volatile detection
│   ├── confluence_scorer.py                   ← NEW: 12-dimension scoring with dynamic weights
│   ├── session_router.py                      ← NEW: Routes to valid strategies per session
│   ├── performance_tracker.py                 ← NEW: Win/loss tracking per strategy/symbol
│   │
│   ├── orchestrator.py                        ← MODIFY: Use new modules
│   ├── ict_pipeline.py                        ← MODIFY: Expanded scoring dimensions
│   ├── claude_decision.py                     ← MODIFY: Strategy context in prompts
│   └── risk_bridge.py                         ← MODIFY: Dynamic position sizing
│
├── rules.json                                 ← MODIFY: Expanded watchlist + time filters
└── STRATEGY_MASTER_PLAN.md                    ← THIS FILE
```

---

## Priority Execution Order

```
WEEK 1: Phase 0 (Quick Wins)
  ├── Expand watchlist (EURUSD, US30, XAUUSD)
  ├── Add time-of-day filters
  ├── Add mean reversion warning to Claude prompts
  └── Load MT5 top configs as confidence boosters

WEEK 2: Phase 1 (Strategy Knowledge Layer)
  ├── Create strategy_knowledge/ directory + JSON files
  ├── Build strategy context loader
  └── Update Claude decision prompts with strategy context

WEEK 3-4: Phase 2 (New Analysis Modules)
  ├── Regime classifier (ADX + ATR + structure)
  ├── EMA stack scoring
  ├── ADR tracking
  ├── PO3 phase detection
  └── Mean reversion module

WEEK 5: Phase 3 (Enhanced Pipeline)
  ├── 12-dimension scoring
  ├── Per-symbol weight profiles
  ├── Session-based strategy routing
  └── Backtest confidence multiplier

WEEK 6+: Phase 4 (Learning Loop)
  ├── Performance tracking
  ├── Weekly strategy reports
  ├── Dynamic weight adjustment
  └── A/B testing framework
```

---

## Appendix: ChartFanatics Strategy Quick Reference

### Strategies Applicable to Crypto (Our Primary Market)
1. Structure + OTE (**HIGH** — direct ICT fit)
2. SMT Divergence + PO3 (**HIGH** — BTC/ETH correlation)
3. Liquidity Strategy (**HIGH** — crypto is liquidity-driven)
4. AMD Model (**MEDIUM** — session-dependent, crypto is 24/7)
5. Break & Retest (**MEDIUM** — universal pattern)
6. Mean Reversion (**MEDIUM** — crypto has extreme moves)
7. Trendline Strategy (**LOW** — crypto respects trendlines less)

### Strategies Applicable to Forex (Expanding Into)
1. PO3, OTE + ADR (**HIGH** — designed for Forex)
2. Trendline Break Pocket (**HIGH** — Forex-native)
3. Intraday Liquidity & Volatility (**HIGH** — session-based)
4. Unique High RR / Trident (**HIGH** — London kill zone, 6 Forex pairs)
5. Structure + OTE (**HIGH** — universal)

### Strategies Applicable to Indices (Expanding Into)
1. AMD Model (**HIGH** — designed for futures)
2. Auction Market Theory (**HIGH** — futures-native)
3. OrderFlow Trading (**HIGH** — futures have DOM data)
4. Volume Profile (**HIGH** — futures have reliable volume)
5. Break & Retest (**MEDIUM** — universal)

### Strategies NOT Applicable (Skip)
- Episodic Pivot (stocks only — catalyst/earnings driven)
- Parabolic Short (stocks only — day trading)
- First Red Day (stocks only — momentum day trading)
- Options Masterclass (options-specific)
- VIX Futures (VIX-specific)
- Full Psychology (mental framework, not signals)
- 5 Stage Framework (development roadmap, not signals)
