"""
Async Orchestrator — main loop for the auto-trading bridge.

Three concurrent tasks:
  1. Analysis loop  — on M15 bar close, run ICT pipeline + Claude decision + execute
  2. Position loop  — every 30s check prices for trailing stops, SL/TP hits
  3. Health loop    — every 60s log state; at NY close save session summary

Usage:
    from bridge.orchestrator import Orchestrator
    import asyncio
    asyncio.run(Orchestrator(mode="paper").run())
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from bridge.config import get_bridge_config, BridgeConfig
from bridge.tv_client import TVClient, TVClientError
from bridge.ict_pipeline import ICTPipeline, SymbolAnalysis
from bridge.claude_decision import ClaudeDecisionMaker
from bridge.decision_types import TradeDecision
from bridge.paper_executor import PaperExecutor
from bridge.live_executor import LiveExecutor
from bridge.session_store import SessionStore
from bridge.risk_bridge import RiskBridge
from bridge.alerts import BridgeAlerts
from bridge.strategy_engine import StrategyEngine, signal_to_decision


# ---------------------------------------------------------------------------
# Strategy knowledge loader
# ---------------------------------------------------------------------------

def _load_strategy_knowledge() -> dict:
    """Load strategy knowledge files for backtest confidence multipliers."""
    knowledge_dir = Path(__file__).parent / "strategy_knowledge"
    result = {"symbol_profiles": {}, "mt5_insights": {}, "session_routing": {}}

    for name, key in [("symbol_profiles.json", "symbol_profiles"),
                      ("mt5_insights.json", "mt5_insights"),
                      ("session_routing.json", "session_routing")]:
        path = knowledge_dir / name
        if path.exists():
            try:
                result[key] = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    return result


def _get_backtest_confidence(symbol: str, knowledge: dict) -> float:
    """Get backtest confidence multiplier for a symbol from MT5 data."""
    # Check symbol_profiles first
    profiles = knowledge.get("symbol_profiles", {})
    profile = profiles.get(symbol, {})
    if profile:
        mt5 = profile.get("mt5_metrics", {})
        sharpe = mt5.get("sharpe_ratio")
        if sharpe is not None:
            if sharpe > 15:
                return 1.4
            elif sharpe > 10:
                return 1.2
            elif sharpe > 5:
                return 1.1
        pf = mt5.get("profit_factor")
        if pf is not None and pf > 2.0:
            return 1.1
        conf = profile.get("risk_profile", {}).get("backtest_confidence_multiplier")
        if conf is not None:
            return conf

    # Check mt5_insights performance tiers
    insights = knowledge.get("mt5_insights", {})
    tiers = insights.get("performance_tiers", {})
    for tier_name, tier_data in tiers.items():
        if symbol in tier_data.get("symbols", []):
            return tier_data.get("confidence_multiplier", 1.0)

    return 1.0  # no data = neutral


def _get_symbol_risk_override(symbol: str, grade: str, rules: dict) -> float | None:
    """Get per-symbol risk override from rules.json."""
    profiles = rules.get("symbol_profiles", {})
    profile = profiles.get(symbol, {})
    overrides = profile.get("risk_overrides", {})
    key = f"grade_{grade.lower()}"
    return overrides.get(key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _is_m15_boundary(dt: datetime) -> bool:
    """Check if current time is within 30s of a 15-minute boundary."""
    return dt.minute % 15 == 0 and dt.second < 30


def _ny_hour(dt: datetime) -> int:
    """Get current hour in New York time (UTC-4 EDT / UTC-5 EST)."""
    # Simplified: assume EDT (UTC-4) for now
    ny = dt - timedelta(hours=4)
    return ny.hour


def _is_lunch_pause(dt: datetime) -> bool:
    """12:00-13:00 NY = low-volume lunch hour."""
    h = _ny_hour(dt)
    return h == 12


def _utc_hour(dt: datetime) -> int:
    return dt.hour


# Per-symbol trading windows (UTC hours, inclusive start, exclusive end)
# Each entry is a list of (start_utc, end_utc) tuples
_SYMBOL_SESSIONS: dict[str, list[tuple[int, int]]] = {
    # Indices — London open + NY session only (futures market hours)
    "CBOT:YM1!":  [(7, 21)],           # London 7 UTC + NY close 21 UTC
    # Forex — London + NY (7am-5pm UTC)
    "OANDA:EURUSD": [(7, 17)],
    # Crypto — 24/7
    "BITSTAMP:BTCUSD":  [(0, 24)],
    "COINBASE:ETHUSD":  [(0, 24)],
    "COINBASE:SOLUSD":  [(0, 24)],
    # Gold — Asia (2-7 UTC) + London (7-12 UTC) + NY (13-17 UTC)
    "OANDA:XAUUSD": [(2, 12), (13, 17)],
    # Oil — London + NY only
    "TVC:UKOIL":  [(7, 17)],
}

# Symbols that trade 24/7 (no gate needed)
_ALWAYS_ON = {"BITSTAMP:BTCUSD", "COINBASE:ETHUSD", "COINBASE:SOLUSD"}


def _symbol_is_active(symbol: str, dt: datetime) -> bool:
    """Check if a symbol should be analyzed at the given UTC time."""
    if symbol in _ALWAYS_ON:
        return True
    sessions = _SYMBOL_SESSIONS.get(symbol)
    if not sessions:
        # Unknown symbol — fall back to London+NY window (7-21 UTC)
        h = _utc_hour(dt)
        return 7 <= h < 21
    h = _utc_hour(dt)
    return any(start <= h < end for start, end in sessions)


def _is_trading_hours(dt: datetime) -> bool:
    """At least one symbol is tradeable right now."""
    return any(_symbol_is_active(s, dt) for s in _SYMBOL_SESSIONS)


# ---------------------------------------------------------------------------
# LiveExecutorAdapter — wraps LiveExecutor with PaperExecutor-compatible interface
# ---------------------------------------------------------------------------

class LiveExecutorAdapter:
    """
    Thin adapter that gives LiveExecutor the same synchronous interface
    as PaperExecutor so the Orchestrator can use either without branching.
    """

    def __init__(self, initial_balance: float = 10_000.0):
        self._live = LiveExecutor(max_positions=3)
        self._live.confirm_session()  # auto-confirm for fully autonomous mode
        self._config = get_bridge_config()  # for TV→MT5 symbol name translation
        self._mt5_connector = None
        self._connect_mt5()  # initialize + login to MT5 before any trades
        self.open_positions: dict[int, Any] = {}  # mirrors PaperExecutor interface
        self.closed_positions: list = []
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.grade_a_wins = 0
        self.grade_a_losses = 0
        # Balance pulled from MT5 on each check; use initial as fallback
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.peak_balance = initial_balance

    def _connect_mt5(self) -> None:
        """Initialize and login to MT5. Must be called before any order submission."""
        try:
            from data.mt5_connector import MT5Connector
            self._mt5_connector = MT5Connector()
            self._mt5_connector.connect()
            print("[LIVE] MT5 connected and logged in.", flush=True)
        except ImportError:
            print("[LIVE] WARNING: MetaTrader5 package not installed. Install with: pip install MetaTrader5", flush=True)
        except Exception as e:
            print(f"[LIVE] WARNING: MT5 connection failed: {e}", flush=True)
            print("[LIVE] Make sure MT5 is running and credentials are correct in .env", flush=True)

    @property
    def daily_pnl(self) -> float:
        return round(self.balance - self.initial_balance, 2)

    def open_position(self, decision: TradeDecision) -> dict:
        """Submit trade to MT5 and mirror state."""
        if not decision.is_trade:
            return {"success": False, "ticket": 0, "message": "Not a trade"}

        # Calculate lot size from risk %
        risk_amount = self.balance * decision.risk_pct
        risk_dist = abs(decision.entry_price - decision.sl_price)
        lot_size = round(risk_amount / risk_dist, 4) if risk_dist > 0 else 0.01

        # Run async submit_trade in a dedicated thread with its own event loop
        # (safe from any async context — threads always get a fresh loop)
        import concurrent.futures
        import threading

        result_holder: list[Any] = []
        error_holder: list[Exception] = []

        # Translate TV symbol (e.g. "CBOT:YM1!") to MT5 symbol (e.g. "US30")
        mt5_decision = decision
        mt5_symbol = self._config.internal_symbol(decision.symbol)
        if mt5_symbol != decision.symbol:
            import copy
            mt5_decision = copy.copy(decision)
            mt5_decision.symbol = mt5_symbol

        def _run_in_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                r = loop.run_until_complete(self._live.submit_trade(mt5_decision, lot_size))
                result_holder.append(r)
            except Exception as e:
                error_holder.append(e)
            finally:
                loop.close()

        t = threading.Thread(target=_run_in_thread, daemon=True)
        t.start()
        t.join(timeout=15)

        if error_holder:
            return {"success": False, "ticket": 0, "message": f"MT5 error: {error_holder[0]}"}
        if not result_holder:
            return {"success": False, "ticket": 0, "message": "MT5 submit timed out"}
        result = result_holder[0]

        if result["success"]:
            # Store minimal position info so position loop can track it
            from bridge.decision_types import PaperPosition
            pos = PaperPosition(
                ticket=result["ticket"],
                symbol=decision.symbol,
                direction=decision.action,
                entry_price=result["fill_price"] or decision.entry_price,
                sl_price=decision.sl_price,
                tp_price=decision.tp_price,
                tp2_price=decision.tp2_price,
                trade_type=decision.trade_type,
                lot_size=lot_size,
                risk_pct=decision.risk_pct,
                opened_at=datetime.now(timezone.utc).isoformat(),
                ict_grade=decision.grade,
                ict_score=decision.ict_score,
                reasoning=decision.reasoning,
                current_price=result["fill_price"] or decision.entry_price,
                trailing_sl=decision.sl_price,
            )
            self.open_positions[result["ticket"]] = pos

        return result

    def check_positions(self, current_prices: dict[str, float]) -> list[dict]:
        """
        Check MT5 positions against current prices.
        MT5 manages SL/TP natively — this just syncs our local state.
        """
        events = []
        to_remove = []

        for ticket, pos in self.open_positions.items():
            price = current_prices.get(pos.symbol)
            if price is None:
                continue

            pos.current_price = price
            if pos.direction == "BUY":
                pos.floating_pnl = (price - pos.entry_price) * pos.lot_size
            else:
                pos.floating_pnl = (pos.entry_price - price) * pos.lot_size

            # Check if MT5 closed the position (TP/SL hit on broker side)
            # We detect this by checking if price has passed SL or TP
            closed_reason = None
            if pos.direction == "BUY":
                if price <= pos.sl_price:
                    closed_reason = "SL"
                elif price >= (pos.tp2_price if pos.tp2_price > 0 else pos.tp_price):
                    closed_reason = "TP"
            else:
                if price >= pos.sl_price:
                    closed_reason = "SL"
                elif price <= (pos.tp2_price if pos.tp2_price > 0 else pos.tp_price):
                    closed_reason = "TP"

            if closed_reason:
                pnl = pos.floating_pnl
                risk = abs(pos.entry_price - pos.sl_price)
                r_mult = pnl / (risk * pos.lot_size) if risk > 0 and pos.lot_size > 0 else 0.0
                to_remove.append(ticket)
                self.balance += pnl
                self.peak_balance = max(self.peak_balance, self.balance)
                if pnl >= 0:
                    self.wins += 1
                    self.consecutive_losses = 0
                    if pos.ict_grade == "A":
                        self.grade_a_wins += 1
                else:
                    self.losses += 1
                    self.consecutive_losses += 1
                    if pos.ict_grade == "A":
                        self.grade_a_losses += 1
                events.append({
                    "ticket": ticket, "symbol": pos.symbol,
                    "pnl": round(pnl, 2), "r_multiple": round(r_mult, 2),
                    "reason": closed_reason, "balance": round(self.balance, 2),
                })

        for t in to_remove:
            self.open_positions.pop(t, None)

        return events

    def get_account_summary(self) -> dict:
        daily_pnl_pct = self.daily_pnl / self.initial_balance if self.initial_balance else 0
        total_dd = (self.peak_balance - self.balance) / self.peak_balance if self.peak_balance > 0 else 0
        grade_a_total = self.grade_a_wins + self.grade_a_losses
        return {
            "balance": round(self.balance, 2),
            "initial_balance": self.initial_balance,
            "daily_pnl": self.daily_pnl,
            "daily_pnl_pct": f"{daily_pnl_pct:.2%}",
            "total_drawdown_pct": f"{total_dd:.2%}",
            "open_positions": len(self.open_positions),
            "closed_today": len(self.closed_positions),
            "wins": self.wins,
            "losses": self.losses,
            "consecutive_losses": self.consecutive_losses,
            "grade_a_win_rate": f"{self.grade_a_wins/grade_a_total:.0%}" if grade_a_total > 0 else "N/A",
            "can_trade": True,
        }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """
    Main orchestrator for the auto-trading bridge.

    Modes:
        "paper" — simulated execution with PaperExecutor
        "live"  — real MT5 execution (future Phase 6)
    """

    def __init__(
        self,
        mode: str = "paper",
        symbols: list[str] | None = None,
        initial_balance: float = 10_000.0,
        analysis_interval: int = 60,
        position_interval: int = 30,
        health_interval: int = 60,
        single_cycle: bool = False,
    ):
        self.mode = mode
        self.config = get_bridge_config()
        self.symbols = symbols or self.config.watchlist
        self.single_cycle = single_cycle

        # Components
        self.pipeline = ICTPipeline()
        self.decision_maker = ClaudeDecisionMaker()
        if mode == "live":
            self.executor = LiveExecutorAdapter(initial_balance=initial_balance)
            print("[ORCH] Mode: LIVE — trades will be sent to MT5", flush=True)
        else:
            self.executor = PaperExecutor(initial_balance=initial_balance)
        self.session = SessionStore()
        self.tv_client = TVClient()
        self.risk_bridge = RiskBridge()
        self.alerts = BridgeAlerts()
        self.strategy_engine = StrategyEngine()

        # Strategy knowledge (MT5 backtests + ChartFanatics strategies)
        self._knowledge = _load_strategy_knowledge()
        self._rules = {}
        rules_path = Path(__file__).parent.parent / "rules.json"
        if rules_path.exists():
            try:
                self._rules = json.loads(rules_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Intervals (seconds)
        self.analysis_interval = analysis_interval
        self.position_interval = position_interval
        self.health_interval = health_interval

        # State
        self._running = False
        self._last_analysis_bar: dict[str, str] = {}  # symbol -> last bar timestamp
        self._cycle_count = 0
        self._kill_switch_triggered = False
        self._kill_switch_date: str = ""  # date when triggered (YYYY-MM-DD UTC)
        self._trade_drawings: dict[int, list[str]] = {}  # ticket -> [entity_id, ...]
        self._last_eod_date: str = ""  # date of last end-of-day summary (YYYY-MM-DD ET)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the orchestrator with all concurrent loops."""
        self._running = True
        knowledge_loaded = bool(self._knowledge.get("symbol_profiles"))
        print(f"\n{'='*60}", flush=True)
        print(f"  Auto-Trading Bridge — {self.mode.upper()} MODE", flush=True)
        print(f"  Symbols: {', '.join(self.symbols)}", flush=True)
        print(f"  Balance: ${self.executor.balance:,.2f}", flush=True)
        print(f"  Analysis interval: {self.analysis_interval}s", flush=True)
        print(f"  Strategy knowledge: {'LOADED' if knowledge_loaded else 'NOT FOUND'}", flush=True)
        if knowledge_loaded:
            n_profiles = len(self._knowledge.get("symbol_profiles", {}))
            print(f"  Symbol profiles: {n_profiles} | Backtest-informed decisions: ON", flush=True)
        print(f"{'='*60}\n", flush=True)

        if self.single_cycle:
            await self._analysis_cycle()
            return

        # Run concurrent loops
        try:
            await asyncio.gather(
                self._analysis_loop(),
                self._position_loop(),
                self._health_loop(),
            )
        except asyncio.CancelledError:
            print("\n[ORCH] Shutting down...", flush=True)
        finally:
            self._save_end_of_day()

    def stop(self) -> None:
        """Signal the orchestrator to stop."""
        self._running = False

    # ------------------------------------------------------------------
    # Analysis loop
    # ------------------------------------------------------------------

    async def _analysis_loop(self) -> None:
        """Run ICT analysis on M15 bar boundaries."""
        print("[ANALYSIS] Loop started", flush=True)

        while self._running:
            now = _now_utc()

            # Only analyze during trading hours
            if not _is_trading_hours(now):
                print(f"[ANALYSIS] Outside trading hours (NY {_ny_hour(now)}:00). Sleeping 5m.", flush=True)
                await asyncio.sleep(300)
                continue

            # Check if we can trade (FTMO limits via risk bridge + executor limits)
            can_trade, reason = self.risk_bridge.can_trade(
                self.executor.balance, self.executor.initial_balance,
                self.executor.daily_pnl, self.executor.peak_balance,
            )
            if not can_trade:
                print(f"[ANALYSIS] Trading paused: {reason}. Sleeping 5m.", flush=True)
                await asyncio.sleep(300)
                continue

            # Reset kill switch at midnight UTC (new trading day)
            today = _now_utc().strftime("%Y-%m-%d")
            if self._kill_switch_triggered and self._kill_switch_date != today:
                self._kill_switch_triggered = False
                print("[ANALYSIS] Kill switch reset for new trading day.", flush=True)

            # Check 2% daily kill switch
            if self._kill_switch_triggered:
                print("[ANALYSIS] Kill switch active (2% daily loss limit). Sleeping 5m.", flush=True)
                await asyncio.sleep(300)
                continue

            # Run analysis cycle
            try:
                await self._analysis_cycle()
            except Exception as e:
                print(f"[ANALYSIS] Error: {e}", flush=True)
                traceback.print_exc()

            self._cycle_count += 1
            await asyncio.sleep(self.analysis_interval)

    async def _analysis_cycle(self) -> None:
        """Run one full analysis cycle across all symbols."""
        now = _now_utc()
        print(f"\n[CYCLE {self._cycle_count}] Starting analysis @ {now.strftime('%H:%M:%S')} UTC", flush=True)

        for symbol in self.symbols:
            if not _symbol_is_active(symbol, now):
                print(f"  [{symbol}] Outside session window — skipping", flush=True)
                continue
            try:
                self._analyze_and_decide(symbol)
            except Exception as e:
                print(f"[CYCLE] {symbol} error: {e}", flush=True)

    # Sanity price ranges per symbol — catches chart cross-contamination
    _PRICE_RANGES: dict[str, tuple[float, float]] = {
        "BITSTAMP:BTCUSD":  (10_000, 500_000),
        "COINBASE:ETHUSD":  (100, 50_000),
        "COINBASE:SOLUSD":  (1, 5_000),
        "OANDA:EURUSD":     (0.5, 2.5),
        "CBOT:YM1!":        (10_000, 100_000),
        "CBOT_MINI_DL:YM1!":(10_000, 100_000),
        "OANDA:XAUUSD":     (500, 15_000),
        "TVC:UKOIL":        (10, 500),
    }

    def _analyze_and_decide(self, symbol: str) -> None:
        """Analyze a single symbol and make a trade decision."""
        # Run ICT pipeline (synchronous — subprocess calls)
        analysis = self.pipeline.analyze_symbol(symbol)

        # Skip if data could not be fetched (chart still loading, symbol unavailable, etc.)
        if analysis.error == "DATA_UNAVAILABLE":
            print(f"  [{symbol}] DATA_UNAVAILABLE — skipping (chart not loaded)", flush=True)
            return

        # Price sanity check — reject if price is outside expected range for this symbol
        price_range = self._PRICE_RANGES.get(symbol)
        if price_range and analysis.current_price > 0:
            lo, hi = price_range
            if not (lo <= analysis.current_price <= hi):
                print(f"  [{symbol}] PRICE_ERROR — got {analysis.current_price:.4f}, expected {lo}-{hi}. Chart not switched correctly, skipping.", flush=True)
                return

        # Sweep gate: liquidity sweep is required for any trade
        if not analysis.sweep_detected:
            print(f"  [{symbol}] NO_SWEEP — skipping (no liquidity sweep detected)", flush=True)
            return

        # Run EA+ICT strategy ensemble in parallel
        ea_signals = []
        try:
            ea_signals = self.strategy_engine.process_symbol(symbol)
        except Exception as e:
            print(f"  [{symbol}] EA ensemble error: {e}", flush=True)

        # Log analysis
        log_entry = {
            "symbol": analysis.symbol,
            "grade": analysis.grade,
            "score": analysis.total_score,
            "direction": analysis.direction,
            "confluence": analysis.confluence_factors,
            "ea_signals": len(ea_signals),
        }
        if ea_signals:
            log_entry["ea_direction"] = ea_signals[0].direction.value
            log_entry["ea_score"] = ea_signals[0].final_score
        self.session.log_analysis(log_entry)

        ea_info = ""
        if ea_signals:
            sig = ea_signals[0]
            ea_info = f" | EA: {sig.direction.value} {sig.final_score:.0f}/100 ({sig.strategy_count} strats)"

        print(
            f"  [{symbol}] Grade {analysis.grade} ({analysis.total_score:.0f}/100) "
            f"{analysis.direction} | {len(analysis.confluence_factors)} confluence{ea_info}",
            flush=True,
        )

        # Skip if BOTH ICT pipeline and EA ensemble show no trade potential
        if analysis.grade in ("D", "INVALID") and not ea_signals:
            return

        # If ICT pipeline is low-grade but EA ensemble has a strong signal, upgrade
        if analysis.grade in ("D", "INVALID") and ea_signals:
            sig = ea_signals[0]
            if sig.final_score >= 65:  # Grade B+ from EA ensemble
                print(f"  [{symbol}] EA ensemble override: {sig.grade.value} ({sig.final_score:.0f})", flush=True)
                # Build a synthetic analysis from EA signal for Claude
                analysis.total_score = sig.final_score
                analysis.grade = sig.grade.value
                direction_str = "BULLISH" if sig.direction.value == "bullish" else "BEARISH"
                analysis.direction = direction_str
                analysis.confluence_factors.append(f"EA_ensemble({sig.strategy_count}_strategies)")

        # Apply backtest confidence multiplier to ICT score
        bt_confidence = _get_backtest_confidence(symbol, self._knowledge)
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
            # Re-grade after score adjustment
            thresholds = self.config.grade_thresholds if hasattr(self.config, 'grade_thresholds') else {"A": 80, "B": 65, "C": 50, "D": 35}
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

        # Claude decision (ICT analysis + EA context + strategy knowledge in prompt)
        decision = self.decision_maker.evaluate(analysis)

        # Log decision
        self.session.log_decision(decision.to_dict())

        print(
            f"  [{symbol}] Decision: {decision.action} "
            f"(confidence={decision.confidence}, model={decision.model_used})"
            f"{f' bt_conf={bt_confidence}x' if bt_confidence != 1.0 else ''}",
            flush=True,
        )

        if decision.reasoning:
            print(f"  [{symbol}] Reason: {decision.reasoning}", flush=True)

        # Check daily loss kill switch (2%)
        daily_pnl_pct = self.executor.daily_pnl / self.executor.initial_balance if self.executor.initial_balance else 0
        if daily_pnl_pct <= -0.02 and not self._kill_switch_triggered:
            self._kill_switch_triggered = True
            self._kill_switch_date = _now_utc().strftime("%Y-%m-%d")
            print(f"[KILL SWITCH] Daily loss limit 2% reached ({daily_pnl_pct:.2%}). Trading HALTED.", flush=True)
            asyncio.get_event_loop().call_soon(
                lambda: asyncio.ensure_future(self.alerts.send_raw(
                    f"🛑 *KILL SWITCH: Daily loss limit 2% reached* ({daily_pnl_pct:.2%})\nTrading paused until midnight UTC."
                ))
            )
            return

        # Execute if it's a trade
        if decision.is_trade:
            # Apply per-symbol risk override from rules.json
            risk_override = _get_symbol_risk_override(symbol, decision.grade, self._rules)
            if risk_override is not None and decision.risk_pct > risk_override:
                print(f"  [{symbol}] Risk override: {decision.risk_pct:.1%} -> {risk_override:.1%} (per-symbol limit)", flush=True)
                decision.risk_pct = risk_override

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

            result = self.executor.open_position(decision)
            if result["success"]:
                print(f"  [{symbol}] OPENED: {result['message']}", flush=True)
                self.session.log_trade({"event": "OPEN", **result})
                # Draw trade levels on TradingView chart (save IDs to remove on close)
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
                        self._trade_drawings[result["ticket"]] = entity_ids
                        print(f"  [{symbol}] Chart: {len(entity_ids)} lines drawn (IDs: {entity_ids})", flush=True)
                except Exception:
                    pass
                # Send alert (fire-and-forget)
                _dec, _lot, _ticket = decision, lot_size, result["ticket"]
                asyncio.get_event_loop().call_soon(
                    lambda d=_dec, l=_lot, t=_ticket: asyncio.ensure_future(
                        self.alerts.send_trade_open(
                            symbol=d.symbol, direction=d.action,
                            entry_price=d.entry_price, sl_price=d.sl_price,
                            tp_price=d.tp_price, tp2_price=d.tp2_price,
                            lot_size=l, grade=d.grade, score=d.ict_score,
                            confidence=d.confidence, reasoning=d.reasoning,
                            ticket=t, mode=self.mode,
                        )
                    )
                )
            else:
                print(f"  [{symbol}] Rejected: {result['message']}", flush=True)

    # ------------------------------------------------------------------
    # Position management loop
    # ------------------------------------------------------------------

    async def _position_loop(self) -> None:
        """Check open positions for SL/TP/trailing stop hits."""
        print("[POSITIONS] Loop started", flush=True)

        while self._running:
            if self.executor.open_positions:
                try:
                    events = await asyncio.get_event_loop().run_in_executor(
                        None, self._check_positions_sync
                    )
                    for event in events:
                        pnl_str = f"+${event['pnl']:.2f}" if event['pnl'] >= 0 else f"-${abs(event['pnl']):.2f}"
                        print(
                            f"  [CLOSE] {event['symbol']} {event['reason']} "
                            f"PnL={pnl_str} ({event['r_multiple']:.1f}R) "
                            f"Balance=${event['balance']:,.2f}",
                            flush=True,
                        )
                        self.session.log_trade({"event": "CLOSE", **event})
                        # Remove only this trade's chart lines (never touches other drawings)
                        ticket = event.get("ticket")
                        if ticket and ticket in self._trade_drawings:
                            try:
                                self.tv_client.draw_remove_trade(self._trade_drawings.pop(ticket))
                            except Exception:
                                pass
                        # Send close alert
                        await self.alerts.send_trade_close(
                            symbol=event["symbol"], direction="",
                            pnl=event["pnl"], r_multiple=event["r_multiple"],
                            reason=event["reason"], balance=event["balance"],
                            ticket=event["ticket"], mode=self.mode,
                        )
                except Exception as e:
                    print(f"[POSITIONS] Error: {e}", flush=True)

            await asyncio.sleep(self.position_interval)

    def _check_positions_sync(self) -> list[dict]:
        """Get current prices and check positions."""
        prices: dict[str, float] = {}
        for pos in self.executor.open_positions.values():
            try:
                self.tv_client.set_symbol(pos.symbol)
                time.sleep(0.5)
                quote = self.tv_client.get_quote()
                price = quote.get("last") or quote.get("lp") or quote.get("close", 0)
                if price:
                    prices[pos.symbol] = float(price)
            except TVClientError:
                pass

        if prices:
            return self.executor.check_positions(prices)
        return []

    # ------------------------------------------------------------------
    # Health / monitoring loop
    # ------------------------------------------------------------------

    async def _health_loop(self) -> None:
        """Periodic health check and state logging."""
        print("[HEALTH] Loop started", flush=True)

        while self._running:
            await asyncio.sleep(self.health_interval)

            summary = self.executor.get_account_summary()
            now = _now_utc()

            # Compact status line
            open_pos = summary["open_positions"]
            positions_info = ""
            if open_pos > 0:
                for pos in self.executor.open_positions.values():
                    positions_info += f" | {pos.symbol} {pos.direction} {pos.floating_pnl:+.2f}"

            print(
                f"[HEALTH {now.strftime('%H:%M')}] "
                f"Balance=${summary['balance']:,.2f} "
                f"PnL={summary['daily_pnl_pct']} "
                f"DD={summary['total_drawdown_pct']} "
                f"Open={open_pos} "
                f"W/L={summary['wins']}/{summary['losses']}"
                f"{positions_info}",
                flush=True,
            )

            # Save periodic snapshot
            self.session.save_snapshot(summary)

            # Daily summary at 5 PM ET — fires once per day, does NOT stop the loop
            # (system runs 24/7 for crypto; summary is informational only)
            ny_h = _ny_hour(now)
            et_date = (now - timedelta(hours=4)).strftime("%Y-%m-%d")
            if ny_h == 17 and now.minute < 2 and self._last_eod_date != et_date:
                self._last_eod_date = et_date
                self._save_end_of_day()
                print("[HEALTH] 5pm ET — daily summary sent.", flush=True)

    # ------------------------------------------------------------------
    # End of day
    # ------------------------------------------------------------------

    def _save_end_of_day(self) -> None:
        """Save end-of-day summary to session store and send alert."""
        summary = self.executor.get_account_summary()
        summary["cycles_run"] = self._cycle_count
        summary["mode"] = self.mode
        summary["symbols"] = self.symbols
        self.session.set_summary(summary)
        print(f"[SESSION] Saved to {self.session.session_file}", flush=True)

        # Send daily summary alert (best-effort)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.alerts.send_daily_summary(summary, self.mode))
            else:
                loop.run_until_complete(self.alerts.send_daily_summary(summary, self.mode))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Auto-Trading Bridge Orchestrator")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper",
                        help="Execution mode (default: paper)")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Override watchlist symbols")
    parser.add_argument("--balance", type=float, default=10_000.0,
                        help="Initial paper balance (default: 10000)")
    parser.add_argument("--interval", type=int, default=60,
                        help="Analysis interval in seconds (default: 60, 0=single cycle)")
    parser.add_argument("--single", action="store_true",
                        help="Run a single analysis cycle and exit")

    args = parser.parse_args()

    orch = Orchestrator(
        mode=args.mode,
        symbols=args.symbols,
        initial_balance=args.balance,
        analysis_interval=max(args.interval, 10) if args.interval > 0 else 60,
        single_cycle=args.single or args.interval == 0,
    )

    # Graceful shutdown on Ctrl+C
    def handle_signal(sig, frame):
        print("\n[SIGNAL] Received interrupt, shutting down...", flush=True)
        orch.stop()

    signal.signal(signal.SIGINT, handle_signal)

    asyncio.run(orch.run())


if __name__ == "__main__":
    main()
