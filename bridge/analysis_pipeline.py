"""
Analysis pipeline — runs the full ICT analysis → EA ensemble → Claude decision → execution
pipeline for a single symbol.

Extracted from orchestrator._analyze_and_decide() to keep the orchestrator
focused on async loop management.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from bridge.config import BridgeConfig, price_in_range, PRICE_RANGES
from bridge.decision_types import (
    TradeDecision, KillSwitchState, CooldownState, SignalAnchorState,
)
from bridge.ict_pipeline import ICTPipeline, SymbolAnalysis
from bridge.claude_decision import ClaudeDecisionMaker
from bridge.strategy_engine import StrategyEngine
from bridge.risk_bridge import RiskBridge
from bridge.session_store import SessionStore
from bridge.state_store import StateStore
from bridge.tv_client import TVClient
from bridge.alerts import BridgeAlerts
from bridge.trade_drawings import TradeDrawingManager
from bridge.trading_hours import (
    now_utc, get_backtest_confidence, get_symbol_risk_override,
)


class AnalysisPipeline:
    """Runs the full analysis-to-execution pipeline for a single symbol."""

    def __init__(
        self,
        config: BridgeConfig,
        pipeline: ICTPipeline,
        decision_maker: ClaudeDecisionMaker,
        strategy_engine: StrategyEngine,
        risk_bridge: RiskBridge,
        executor: Any,
        paper_shadow: Any,
        session: SessionStore,
        state_store: StateStore,
        paper_state_store: StateStore,
        tv_client: TVClient,
        alerts: BridgeAlerts,
        drawings: TradeDrawingManager,
        knowledge: dict,
        rules: dict,
        mode: str,
        # Shared mutable state — Orchestrator owns these, pipeline reads/writes
        kill_switch: KillSwitchState,
        cooldown: CooldownState,
        signal_anchor: SignalAnchorState,
    ):
        self.config = config
        self.pipeline = pipeline
        self.decision_maker = decision_maker
        self.strategy_engine = strategy_engine
        self.risk_bridge = risk_bridge
        self.executor = executor
        self.paper_shadow = paper_shadow
        self.session = session
        self.state_store = state_store
        self.paper_state_store = paper_state_store
        self.tv_client = tv_client
        self.alerts = alerts
        self.drawings = drawings
        self._knowledge = knowledge
        self._rules = rules
        self.mode = mode
        self.kill_switch = kill_switch
        self.cooldown = cooldown
        self.signal_anchor = signal_anchor

    def run(self, symbol: str) -> None:
        """Full pipeline for one symbol: analyze → decide → execute."""
        # Kill switch — check BEFORE doing any analysis or API calls
        if self._check_kill_switch(symbol):
            return

        # Per-symbol loss cooldown — skip if this symbol recently hit SL
        if hasattr(self.executor, 'is_symbol_on_loss_cooldown'):
            if self.executor.is_symbol_on_loss_cooldown(symbol):
                print(f"  [{symbol}] On post-loss cooldown — skipping", flush=True)
                return

        # Run ICT pipeline + skip gates
        analysis, ea_signals = self._run_ict_analysis(symbol)
        if analysis is None:
            return

        # EA ensemble override for low-grade ICT signals
        self._apply_ea_override(symbol, analysis, ea_signals)

        # Apply backtest confidence multiplier
        bt_confidence = self._apply_backtest_confidence(symbol, analysis)

        # Score decay for stale signals
        self._apply_score_decay(symbol, analysis)

        # Cooldown check
        if self._is_on_cooldown(symbol):
            return

        # Claude decision
        decision = self._get_claude_decision(symbol, analysis, bt_confidence)

        # Paper shadow — always record
        self._record_paper_shadow(symbol, decision)

        # Check daily loss kill switch trigger
        if self._check_kill_switch_trigger(symbol):
            return

        # Execute live trade
        self._execute_live(symbol, decision)

    def _check_kill_switch(self, symbol: str) -> bool:
        """Returns True if kill switch is active (skip this symbol)."""
        if self.kill_switch.triggered:
            today = now_utc().strftime("%Y-%m-%d")
            if self.kill_switch.date == today:
                print(f"  [{symbol}] Kill switch active — skipping (daily loss limit hit)", flush=True)
                return True
            else:
                self.kill_switch.triggered = False
                self.kill_switch.date = ""
        return False

    def _run_ict_analysis(self, symbol: str) -> tuple[SymbolAnalysis | None, list]:
        """Run ICT pipeline and apply skip gates. Returns (analysis, ea_signals) or (None, [])."""
        analysis = self.pipeline.analyze_symbol(symbol)
        ea_signals = []
        skip_reason = None

        if analysis.error == "DATA_UNAVAILABLE":
            skip_reason = "DATA_UNAVAILABLE"
            print(f"  [{symbol}] DATA_UNAVAILABLE — skipping (chart not loaded)", flush=True)
        elif analysis.current_price > 0 and not price_in_range(symbol, analysis.current_price):
            rng = PRICE_RANGES.get(symbol.split(":")[-1], ("?", "?"))
            skip_reason = f"PRICE_ERROR: got {analysis.current_price:.4f}, expected {rng[0]}-{rng[1]}"
            print(f"  [{symbol}] {skip_reason}. Chart not switched correctly, skipping.", flush=True)
        elif not analysis.sweep_detected:
            # No sweep is a warning, not a hard gate — the score already penalizes
            # missing sweeps. Grade D setups are still filtered downstream.
            print(f"  [{symbol}] NO_SWEEP — reduced confidence (no liquidity sweep detected)", flush=True)

        if not skip_reason:
            try:
                # Reuse ICT pipeline's verified OHLCV data instead of re-collecting
                # from TradingView (avoids chart contention and contamination)
                verified_dfs = getattr(self.pipeline, '_last_collected_dfs', None)
                ea_signals = self.strategy_engine.process_symbol(
                    symbol, dataframes=verified_dfs if verified_dfs else None
                )
            except Exception as e:
                print(f"  [{symbol}] EA ensemble error: {e}", flush=True)

        # Log full analysis
        log_entry = analysis.to_dict()
        log_entry["ea_signals"] = len(ea_signals)
        if ea_signals:
            log_entry["ea_direction"] = ea_signals[0].direction.value
            log_entry["ea_score"] = ea_signals[0].final_score
        if skip_reason:
            log_entry["skip_reason"] = skip_reason
        self.session.log_analysis(log_entry)

        if skip_reason:
            return None, []

        # Print analysis summary
        ea_info = ""
        if ea_signals:
            sig = ea_signals[0]
            ea_info = f" | EA: {sig.direction.value} {sig.final_score:.0f}/100 ({sig.strategy_count} strats)"

        print(
            f"  [{symbol}] Grade {analysis.grade} ({analysis.total_score:.0f}/100) "
            f"{analysis.direction} | {len(analysis.confluence_factors)} confluence{ea_info} | "
            f"struct={analysis.structure_score:.0f} ob={analysis.ob_score:.0f} fvg={analysis.fvg_score:.0f} "
            f"sess={analysis.session_score:.0f} ote={analysis.ote_score:.0f} smt={analysis.smt_score:.0f} "
            f"sweep={'Y' if analysis.sweep_detected else 'N'} disp={'Y' if analysis.displacement_confirmed else 'N'} "
            f"pd={analysis.pd_zone or '?'}{'Y' if analysis.pd_aligned else 'N'} kz={'Y' if analysis.is_kill_zone else 'N'}",
            flush=True,
        )

        # Skip if BOTH ICT and EA show no trade potential
        if analysis.grade in ("D", "INVALID") and not ea_signals:
            return None, []

        return analysis, ea_signals

    def _apply_ea_override(self, symbol: str, analysis: SymbolAnalysis, ea_signals: list) -> None:
        """Add EA ensemble as informational context, DO NOT override ICT grade."""
        if analysis.grade in ("D", "INVALID") and ea_signals:
            sig = ea_signals[0]
            if sig.final_score >= 65:
                # Add as informational context, DO NOT override ICT grade
                analysis.confluence_factors.append(
                    f"EA_ensemble({sig.direction.value}, score={sig.final_score:.0f}) — ICT grade maintained"
                )
                print(f"  [{symbol}] EA ensemble agrees ({sig.direction.value}, {sig.final_score:.0f}) but ICT grade {analysis.grade} maintained", flush=True)
                # Previously: analysis.direction = sig.direction; analysis.total_score = sig.final_score; analysis.grade = ...
                # REMOVED: Grade override bypasses ICT quality gates

    def _apply_backtest_confidence(self, symbol: str, analysis: SymbolAnalysis) -> float:
        """Apply backtest confidence multiplier and re-grade. Returns the multiplier."""
        bt_confidence = get_backtest_confidence(symbol, self._knowledge)
        if bt_confidence != 1.0 and analysis.total_score > 0:
            original_score = analysis.total_score
            analysis.total_score = min(100, analysis.total_score * bt_confidence)
            if bt_confidence > 1.0:
                analysis.confluence_factors.append(
                    f"MT5_backtest_boost({bt_confidence}x, {original_score:.0f}->{analysis.total_score:.0f})"
                )
            else:
                analysis.confluence_factors.append(
                    f"MT5_undertested({bt_confidence}x, {original_score:.0f}->{analysis.total_score:.0f})"
                )
            self._regrade(analysis)
        return bt_confidence

    def _apply_score_decay(self, symbol: str, analysis: SymbolAnalysis) -> None:
        """Penalize stale signals where price moves against the thesis."""
        if analysis.current_price <= 0 or analysis.direction not in ("BULLISH", "BEARISH"):
            return

        prev = self.signal_anchor.anchors.get(symbol)
        if prev and prev[0] == analysis.direction:
            anchor_price = prev[1]
            if analysis.direction == "BULLISH":
                adverse_move_pct = (anchor_price - analysis.current_price) / anchor_price * 100
            else:
                adverse_move_pct = (analysis.current_price - anchor_price) / anchor_price * 100

            # Use asset-class-aware decay threshold
            asset_class = "crypto" if any(c in symbol.upper() for c in ["BTC", "ETH", "SOL", "DOGE"]) else "forex"
            if asset_class == "crypto":
                decay_threshold = 0.3  # 0.3% before decay starts (crypto is volatile)
            else:
                decay_threshold = 0.1  # 0.1% for forex/indices

            if adverse_move_pct > (decay_threshold / 2):
                decay_factor = min(adverse_move_pct / decay_threshold * 0.03, 0.20)
                original_score = analysis.total_score
                analysis.total_score *= (1.0 - decay_factor)
                analysis.confluence_factors.append(
                    f"score_decay(-{decay_factor*100:.0f}%, price moved {adverse_move_pct:.2f}% against {analysis.direction})"
                )
                self._regrade(analysis)
                print(
                    f"  [{symbol}] Score decay: {original_score:.0f} -> {analysis.total_score:.0f} "
                    f"(price {adverse_move_pct:.2f}% against {analysis.direction})",
                    flush=True,
                )
        else:
            self.signal_anchor.anchors[symbol] = (analysis.direction, analysis.current_price)

        # Reset anchor if direction flips
        if prev and prev[0] != analysis.direction:
            self.signal_anchor.anchors[symbol] = (analysis.direction, analysis.current_price)

    def _is_on_cooldown(self, symbol: str) -> bool:
        """Check if we recently got a BUY/SELL for this symbol."""
        last_decision_time = self.cooldown.decisions.get(symbol, 0)
        if time.time() - last_decision_time < self.cooldown.seconds:
            mins_left = int((self.cooldown.seconds - (time.time() - last_decision_time)) / 60)
            print(f"  [{symbol}] Cooldown active ({mins_left}m remaining) — skipping Claude call", flush=True)
            return True
        return False

    def _get_claude_decision(self, symbol: str, analysis: SymbolAnalysis, bt_confidence: float) -> TradeDecision:
        """Call Claude for trade decision."""
        decision = self.decision_maker.evaluate(analysis)

        if not decision.is_trade:
            # SKIP cooldown: expire 15 min from now regardless of cooldown.seconds.
            # Formula: _is_on_cooldown checks (now - stored) < cooldown.seconds,
            # so stored = now - cooldown.seconds + 900 makes it expire after 900s.
            self.cooldown.decisions[symbol] = time.time() - self.cooldown.seconds + 900

        self.session.log_decision(decision.to_dict())

        print(
            f"  [{symbol}] Decision: {decision.action} "
            f"(confidence={decision.confidence}, model={decision.model_used})"
            f"{f' bt_conf={bt_confidence}x' if bt_confidence != 1.0 else ''}",
            flush=True,
        )

        if decision.reasoning:
            print(f"  [{symbol}] Reason: {decision.reasoning}", flush=True)

        return decision

    def _record_paper_shadow(self, symbol: str, decision: TradeDecision) -> None:
        """Record trade decision in paper shadow for audit (BEFORE any live gates)."""
        if not decision.is_trade or self.paper_shadow is self.executor:
            return

        paper_lot = self._fallback_paper_lots(decision)
        try:
            shadow_result = self.paper_shadow.open_position(decision, lot_size=paper_lot)
            if shadow_result["success"]:
                # Set cooldown after successful paper execution
                self.cooldown.decisions[symbol] = time.time()
                paper_ticket = shadow_result["ticket"]
                print(f"  [{symbol}] Paper #{paper_ticket}: {decision.action} @ {decision.entry_price:.2f}", flush=True)
                self.session.log_trade({
                    "event": "PAPER_OPEN",
                    "ticket": paper_ticket,
                    "symbol": decision.symbol,
                    "direction": decision.action,
                    "entry_price": decision.entry_price,
                    "sl_price": decision.sl_price,
                    "tp_price": decision.tp_price,
                    "tp2_price": decision.tp2_price,
                    "lot_size": paper_lot,
                    "risk_pct": decision.risk_pct,
                    "ict_grade": decision.grade,
                    "ict_score": decision.ict_score,
                    "confidence": decision.confidence,
                    "reasoning": decision.reasoning,
                    "mode": "paper_shadow",
                })
                try:
                    entity_ids = self.tv_client.draw_trade(
                        symbol=decision.symbol,
                        direction=decision.action,
                        entry=decision.entry_price,
                        sl=decision.sl_price,
                        tp1=decision.tp_price,
                        tp2=decision.tp2_price,
                        grade=f"P-{decision.grade}",
                        ticket=paper_ticket,
                    )
                    if entity_ids:
                        self.drawings.add(f"paper_{paper_ticket}", entity_ids)
                except Exception:
                    pass
                self.paper_state_store.save(self.paper_shadow, "paper_shadow")
        except Exception as e:
            print(f"  [{symbol}] Paper shadow error: {e}", flush=True)

    def _check_kill_switch_trigger(self, symbol: str) -> bool:
        """Check if daily loss has reached 2% — halt trading if so."""
        # Use day-start balance as denominator (matches daily_pnl which is
        # now balance - day_start_balance, not balance - initial_balance).
        denom = getattr(self.executor, '_day_start_balance', self.executor.initial_balance)
        daily_pnl_pct = self.executor.daily_pnl / denom if denom else 0
        if daily_pnl_pct <= -0.02 and not self.kill_switch.triggered:
            self.kill_switch.triggered = True
            self.kill_switch.date = now_utc().strftime("%Y-%m-%d")
            print(f"[KILL SWITCH] Daily loss limit 2% reached ({daily_pnl_pct:.2%}). Trading HALTED.", flush=True)
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(asyncio.ensure_future, self.alerts.send_raw(
                    f"\U0001f6d1 *KILL SWITCH: Daily loss limit 2% reached* ({daily_pnl_pct:.2%})\nTrading paused until midnight UTC."
                ))
            except RuntimeError:
                pass
            return True
        return False

    def _execute_live(self, symbol: str, decision: TradeDecision) -> None:
        """Apply risk gates and execute the trade if approved."""
        if not decision.is_trade:
            return

        # Per-symbol minimum grade gate (fallback to global default "B")
        profile = self._rules.get("symbol_profiles", {}).get(symbol, {})
        min_grade = profile.get("min_grade_live") or self._rules.get("min_grade_live", "B")
        if min_grade:
            grade_order = {"A": 0, "B": 1, "C": 2, "D": 3}
            if grade_order.get(decision.grade, 3) > grade_order.get(min_grade, 0):
                print(f"  [{symbol}] BLOCKED: Grade {decision.grade} below min_grade_live={min_grade}", flush=True)
                return

        # Minimum confidence gate for live trades
        min_confidence = self._rules.get("min_live_confidence", 65)
        if decision.confidence < min_confidence:
            print(f"  [{symbol}] BLOCKED: Confidence {decision.confidence} below min {min_confidence}", flush=True)
            return

        # Per-symbol risk override
        risk_override = get_symbol_risk_override(symbol, decision.grade, self._rules)
        if risk_override is not None and decision.risk_pct > risk_override:
            print(f"  [{symbol}] Risk override: {decision.risk_pct:.1%} -> {risk_override:.1%} (per-symbol limit)", flush=True)
            decision.risk_pct = risk_override

        # Correlation gate
        corr_ok, corr_reason = self.risk_bridge.check_correlation(
            decision.symbol, decision.action, self.executor.open_positions
        )
        if not corr_ok:
            print(f"  [{symbol}] BLOCKED (live): {corr_reason}", flush=True)
            self.session.log_decision(decision)
            return

        # Risk gate: FTMO compliance + position sizing
        approved, lot_size, risk_msg = self.risk_bridge.check_trade(
            symbol=decision.symbol,
            direction=decision.action,
            entry_price=decision.entry_price,
            sl_price=decision.sl_price,
            risk_pct=decision.risk_pct,
            balance=self.executor.balance,
            initial_balance=self.executor.initial_balance,
            daily_pnl=self.executor.daily_pnl,
            peak_balance=self.executor.peak_balance,
        )

        if not approved:
            print(f"  [{symbol}] Risk rejected: {risk_msg}", flush=True)
            return

        print(f"  [{symbol}] Risk: {risk_msg}", flush=True)

        result = self.executor.open_position(decision, lot_size=lot_size)
        if result["success"]:
            # Set cooldown after successful live execution
            self.cooldown.decisions[symbol] = time.time()
            print(f"  [{symbol}] OPENED: {result['message']}", flush=True)
            self.session.log_trade({
                "event": "OPEN",
                "ticket": result["ticket"],
                "symbol": decision.symbol,
                "direction": decision.action,
                "entry_price": decision.entry_price,
                "sl_price": decision.sl_price,
                "tp_price": decision.tp_price,
                "tp2_price": decision.tp2_price,
                "lot_size": lot_size,
                "risk_pct": decision.risk_pct,
                "ict_grade": decision.grade,
                "ict_score": decision.ict_score,
                "trade_type": decision.trade_type,
                "confidence": decision.confidence,
                "reasoning": decision.reasoning,
                "mode": self.mode,
            })
            self.state_store.save(self.executor, self.mode)
            try:
                entity_ids = self.tv_client.draw_trade(
                    symbol=decision.symbol,
                    direction=decision.action,
                    entry=decision.entry_price,
                    sl=decision.sl_price,
                    tp1=decision.tp_price,
                    tp2=decision.tp2_price,
                    grade=decision.grade,
                    ticket=result["ticket"],
                )
                if entity_ids:
                    self.drawings.add(result["ticket"], entity_ids)
                    print(f"  [{symbol}] Chart: {len(entity_ids)} lines drawn (IDs: {entity_ids})", flush=True)
            except Exception:
                pass
            # Send alert
            try:
                _loop = asyncio.get_running_loop()
                _loop.call_soon_threadsafe(asyncio.ensure_future,
                    self.alerts.send_trade_open(
                        symbol=decision.symbol, direction=decision.action,
                        entry_price=decision.entry_price, sl_price=decision.sl_price,
                        tp_price=decision.tp_price, tp2_price=decision.tp2_price,
                        lot_size=lot_size, grade=decision.grade, score=decision.ict_score,
                        confidence=decision.confidence, reasoning=decision.reasoning,
                        ticket=result["ticket"], mode=self.mode,
                    )
                )
            except RuntimeError:
                pass
        else:
            print(f"  [{symbol}] Rejected: {result['message']}", flush=True)

    def _regrade(self, analysis: SymbolAnalysis) -> None:
        """Re-calculate grade from score using config thresholds."""
        thresholds = self.config.grade_thresholds
        if analysis.total_score >= thresholds.get("A", 80):
            analysis.grade = "A"
        elif analysis.total_score >= thresholds.get("B", 65):
            analysis.grade = "B"
        elif analysis.total_score >= thresholds.get("C", 50):
            analysis.grade = "C"
        elif analysis.total_score >= thresholds.get("D", 35):
            analysis.grade = "D"
        else:
            analysis.grade = "INVALID"

    def _fallback_paper_lots(self, decision: TradeDecision, symbol: str = "") -> float:
        """Calculate a reasonable paper lot size when risk gate doesn't provide one."""
        if decision.entry_price <= 0 or decision.sl_price <= 0:
            return 1.0
        from bridge.risk_bridge import PAPER_SYMBOL_SPECS, _clamp_to_hard_max
        risk_usd = self.paper_shadow.balance * (decision.risk_pct or 0.0075)
        sl_distance = abs(decision.entry_price - decision.sl_price)
        if sl_distance == 0:
            return 0.01
        sym = symbol or decision.symbol or ""
        sym = sym.split(":")[-1] if ":" in sym else sym
        spec = PAPER_SYMBOL_SPECS.get(sym)
        if spec and spec.tick_size > 0 and spec.tick_value > 0:
            sl_ticks = sl_distance / spec.tick_size
            cost_per_lot = sl_ticks * spec.tick_value
            if cost_per_lot > 0:
                lots = round(risk_usd / cost_per_lot, 2)
                return _clamp_to_hard_max(sym, lots)
        return _clamp_to_hard_max(sym, round(risk_usd / sl_distance, 4))
