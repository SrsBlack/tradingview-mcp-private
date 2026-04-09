# Deep Interview Spec: Autonomous ICT Trading System

## Metadata
- Interview ID: trading-system-2026-04-08
- Rounds: 12
- Final Ambiguity Score: 9%
- Type: brownfield
- Generated: 2026-04-08
- Threshold: 20%
- Status: PASSED

## Clarity Breakdown
| Dimension | Score | Weight | Weighted |
|-----------|-------|--------|----------|
| Goal Clarity | 0.93 | 40% | 0.372 |
| Constraint Clarity | 0.90 | 30% | 0.270 |
| Success Criteria | 0.88 | 30% | 0.264 |
| **Total Clarity** | | | **0.906** |
| **Ambiguity** | | | **9%** |

## Goal
Build a fully autonomous 24/7 ICT-based trading system that mirrors how the user trades: liquidity sweep as a mandatory gate, CHoCH + FVG/OB entry, 2-stage TP exits, per-symbol confidence weighting from MT5 backtest data, and Claude AI reviewing every Grade A and B setup — all running without human intervention.

## Constraints
- **Liquidity sweep is a hard binary gate** — no sweep = no trade, regardless of ICT score
- **SL placement:** HTF swing trades → SL beyond the swept level; intraday trades → SL beyond the OB/FVG used for entry
- **TP structure:** Two-stage exit — partial close at HTF FVG or Order Block; final target at next liquidity pool
- **Daily loss kill switch:** Hard stop at 2% daily drawdown (prop-firm safe, below FTMO 5% limit)
- **Claude in loop:** Grade A (Sonnet) and Grade B (Haiku) both require Claude final decision before entry
- **Symbols:** All 7 current symbols (BTCUSD, ETHUSD, SOLUSD, EURUSD, US30.cash, XAUUSD, UKOIL.cash) plus expandable; confidence weighted by MT5 backtest Sharpe/PF
- **Operation:** Fully autonomous 24/7 — no human screen time required
- **Alerts:** Telegram alerts on: every entry, every close (with P&L), daily summary, kill switch trigger

## Non-Goals
- Manual discretionary trading support (no "should I take this?" flow)
- Consecutive-loss-based kill switch (user confirmed: daily loss limit only)
- London-only or session-restricted operation (runs 24/7 across all sessions)
- Removing Claude from the decision loop

## Acceptance Criteria
- [ ] A setup with no liquidity sweep scores Grade D or lower regardless of other confluence (hard gate enforced)
- [ ] A setup with all three (sweep + CHoCH + FVG/OB) scores Grade A (≥80)
- [ ] A setup with CHoCH + one of FVG/OB (no sweep) is downgraded to Grade C maximum
- [ ] SL is placed beyond swept level for HTF swing mode; beyond OB/FVG for intraday mode
- [ ] Every trade has two TP levels: TP1 at HTF FVG/OB (partial close), TP2 at next liquidity pool
- [ ] Trading halts automatically when daily P&L reaches -2% of account balance
- [ ] Telegram alert sent on: entry (with grade, direction, SL, TP1, TP2), close (with R:R), daily summary, kill switch
- [ ] Claude reviews every Grade A (Sonnet) and Grade B (Haiku) setup before entry
- [ ] Paper mode Grade A win rate is trackable and reported in daily summary
- [ ] System continues running 24/7 without manual restart

## Assumptions Exposed & Resolved
| Assumption | Challenge | Resolution |
|------------|-----------|------------|
| Liquidity sweep = scoring dimension | "Does it have to be a hard gate?" | YES — no sweep = no trade, binary |
| One TP target | "Do you scale out?" | Two-stage: partial at HTF FVG/OB, final at liquidity pool |
| FTMO 5% daily limit | "What's your actual daily limit?" | Self-imposed 2% (more conservative) |
| Human monitors for safety | "What triggers auto-pause?" | Daily loss limit only (not consecutive losses) |
| Session-restricted trading | "Which sessions do you trade?" | 24/7 autonomous — all sessions |
| Claude only for Grade A | "Grade B too?" | Claude on both A and B |

## Technical Context (Brownfield)

### Files to Modify
| File | What Changes |
|------|-------------|
| `bridge/ict_pipeline.py` | Add `sweep_detected` boolean output to ICT result |
| `bridge/orchestrator.py` | Add liquidity sweep hard gate before scoring; add daily loss tracker; add 2-stage TP logic |
| `bridge/paper_executor.py` | Support two TP levels (tp1_price, tp2_price) + partial close at TP1 |
| `bridge/live_executor.py` | Same dual-TP support for live MT5 orders |
| `bridge/claude_decision.py` | Add sweep context to prompt; output tp1 and tp2 in JSON response |
| `bridge/decision_types.py` | Add `tp1_price`, `tp2_price`, `partial_close_pct` to TradeDecision |
| `bridge/alerts.py` | Add TP1/TP2 to entry alert; add R:R to close alert; add daily summary |
| `bridge/risk_bridge.py` | Update daily loss limit to 2% (from current FTMO 5%) |
| `rules.json` | Add `liquidity_sweep_required: true`, `daily_loss_limit_pct: 2.0`, TP config |

### Current Scoring (7 dimensions, 100pts)
- Structure: 25pts
- Liquidity: 20pts ← currently includes sweep scoring (needs to become hard gate instead)
- Order Block: 15pts
- FVG: 15pts
- Session: 10pts
- OTE: 10pts
- SMT: 5pts

### Proposed Scoring Change
- Liquidity sweep: **hard gate** (binary pass/fail before scoring)
- If sweep present: run full 7-dimension scoring (adjust weights since sweep is no longer scored)
- If no sweep: auto Grade D/skip regardless of other score

## Ontology (Key Entities)

| Entity | Type | Fields | Relationships |
|--------|------|--------|---------------|
| LiquiditySweep | Core domain | level_swept, direction, bar_index | Gates → Entry |
| CHoCH | Core domain | direction, bar_index, confirmed | Follows → LiquiditySweep |
| FVG/OB | Core domain | type, price_range, quality | Entry zone after → CHoCH |
| Entry | Core domain | symbol, direction, price, trade_type | Requires → all three above |
| StopLoss | Supporting | price, method (swing/intraday) | Attached to → Entry |
| TakeProfit | Supporting | tp1_price, tp2_price, partial_pct | Attached to → Entry |
| HTF Context | Supporting | bias, tf, fvg_levels, ob_levels | Informs → Entry, TakeProfit |
| MT5BacktestData | External system | sharpe, pf, symbol, confidence_multiplier | Weights → Entry scoring |
| DailyLossTracker | Supporting | current_pct, limit_pct, triggered | Kill switch for → all trading |

## Ontology Convergence
| Round | Entity Count | New | Changed | Stable | Stability |
|-------|-------------|-----|---------|--------|-----------|
| 1 | 3 | 3 | - | - | N/A |
| 2 | 4 | 1 | 0 | 3 | N/A |
| 3 | 5 | 1 | 0 | 4 | 80% |
| 4 | 6 | 1 | 0 | 5 | 83% |
| 5 | 7 | 1 | 0 | 6 | 86% |
| 6 | 7 | 0 | 0 | 7 | 100% |
| 7 | 8 | 1 | 0 | 7 | 88% |
| 8-12 | 9 | 1 | 0 | 8 | 100% |
