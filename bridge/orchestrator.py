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
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bridge.config import get_bridge_config, BridgeConfig, price_in_range
from bridge.price_verify import PriceVerifier
from bridge.tv_client import TVClient
from bridge.ict_pipeline import ICTPipeline
from bridge.claude_decision import ClaudeDecisionMaker
from bridge.decision_types import (
    TradeDecision, KillSwitchState, CooldownState, SignalAnchorState,
)
from bridge.paper_executor import PaperExecutor
from bridge.session_store import SessionStore
from bridge.state_store import StateStore
from bridge.risk_bridge import RiskBridge
from bridge.alerts import BridgeAlerts
from bridge.strategy_engine import StrategyEngine

# New decomposed modules
from bridge.trading_hours import (
    now_utc, is_m15_boundary, ny_hour, is_lunch_pause,
    is_high_impact_news_window, symbol_is_active, is_trading_hours,
    load_strategy_knowledge,
)
from bridge.live_executor_adapter import LiveExecutorAdapter
from bridge.trade_drawings import TradeDrawingManager
from bridge.analysis_pipeline import AnalysisPipeline
from bridge.position_manager import PositionManager
from bridge.health_monitor import HealthMonitor


class Orchestrator:
    """
    Main orchestrator for the auto-trading bridge.

    Modes:
        "paper" — simulated execution with PaperExecutor
        "live"  — real MT5 execution via LiveExecutorAdapter
    """

    def __init__(
        self,
        mode: str = "paper",
        symbols: list[str] | None = None,
        initial_balance: float = 100_000.0,
        analysis_interval: int = 60,
        position_interval: int = 30,
        health_interval: int = 60,
        single_cycle: bool = False,
    ):
        self.mode = mode
        self.config = get_bridge_config()
        self.symbols = symbols or self.config.watchlist
        self.single_cycle = single_cycle

        # --- Core components ---
        self.pipeline = ICTPipeline()
        self.decision_maker = ClaudeDecisionMaker()
        self.paper_shadow = PaperExecutor(initial_balance=initial_balance)
        if mode == "live":
            self.executor = LiveExecutorAdapter(initial_balance=initial_balance)
            print("[ORCH] Mode: LIVE — trades will be sent to MT5", flush=True)
            print("[ORCH] Shadow paper executor running in parallel for audit", flush=True)
        else:
            self.executor = PaperExecutor(initial_balance=initial_balance)
        self.session = SessionStore()
        self.state_store = StateStore()
        self.paper_state_store = StateStore(
            path=Path.home() / ".tradingview-mcp" / "paper_shadow_state.json"
        )
        self.tv_client = TVClient()
        self.risk_bridge = RiskBridge()
        self.alerts = BridgeAlerts()
        self.strategy_engine = StrategyEngine()
        self.price_verifier = PriceVerifier()

        # Strategy knowledge
        self._knowledge = load_strategy_knowledge()
        self._rules = {}
        rules_path = Path(__file__).parent.parent / "rules.json"
        if rules_path.exists():
            try:
                self._rules = json.loads(rules_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Intervals
        self.analysis_interval = analysis_interval
        self.position_interval = position_interval
        self.health_interval = health_interval

        # Loop state
        self._running = False
        self._cycle_count = 0

        # --- Shared mutable state (passed to sub-managers by reference) ---
        self._kill_switch = KillSwitchState()
        self._cooldown = CooldownState()
        self._signal_anchor = SignalAnchorState()

        # --- Decomposed managers ---
        self.drawings = TradeDrawingManager(self.tv_client)

        self.analysis = AnalysisPipeline(
            config=self.config,
            pipeline=self.pipeline,
            decision_maker=self.decision_maker,
            strategy_engine=self.strategy_engine,
            risk_bridge=self.risk_bridge,
            executor=self.executor,
            paper_shadow=self.paper_shadow,
            session=self.session,
            state_store=self.state_store,
            paper_state_store=self.paper_state_store,
            tv_client=self.tv_client,
            alerts=self.alerts,
            drawings=self.drawings,
            knowledge=self._knowledge,
            rules=self._rules,
            mode=self.mode,
            kill_switch=self._kill_switch,
            cooldown=self._cooldown,
            signal_anchor=self._signal_anchor,
        )

        self.position_manager = PositionManager(
            executor=self.executor,
            paper_shadow=self.paper_shadow,
            tv_client=self.tv_client,
            price_verifier=self.price_verifier,
            session=self.session,
            state_store=self.state_store,
            paper_state_store=self.paper_state_store,
            drawings=self.drawings,
            mode=self.mode,
        )

        self.health_monitor = HealthMonitor(
            executor=self.executor,
            paper_shadow=self.paper_shadow,
            tv_client=self.tv_client,
            session=self.session,
            state_store=self.state_store,
            alerts=self.alerts,
            mode=self.mode,
            paper_state_store=self.paper_state_store,
        )

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the orchestrator with all concurrent loops."""
        self._running = True

        # Restore state from previous session
        restored_positions = self.state_store.restore_into(self.executor, self.mode)

        # Restore paper shadow state
        if self.paper_shadow is not self.executor:
            paper_restored = self.paper_state_store.restore_into(self.paper_shadow, "paper_shadow")
            if paper_restored:
                print(f"[PAPER] Restored {len(paper_restored)} shadow position(s) from state", flush=True)

        # Mirror live MT5 positions into paper shadow
        if self.paper_shadow is not self.executor and restored_positions:
            self.position_manager.mirror_live_to_paper(
                restored_positions,
                fallback_lots_fn=self.analysis._fallback_paper_lots,
            )

        # Load today's closed trades for startup display
        today_trades = HealthMonitor.load_todays_trades()

        # Reconcile restored positions against live prices
        if restored_positions:
            await asyncio.get_running_loop().run_in_executor(
                None, self.position_manager.reconcile_restored
            )

        # Clean up stale trade drawings
        try:
            active_tickets: set[str] = set()
            for ticket in self.executor.open_positions:
                active_tickets.add(str(ticket))
            if self.paper_shadow is not self.executor:
                for ticket in self.paper_shadow.open_positions:
                    active_tickets.add(str(ticket))
            await asyncio.get_running_loop().run_in_executor(
                None, self.drawings.cleanup_stale, active_tickets
            )
        except Exception as e:
            print(f"  [DRAW] Startup cleanup error (non-fatal): {e}", flush=True)

        # --- Startup banner ---
        self._print_startup_banner(restored_positions, today_trades)

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
            now = now_utc()

            if not is_trading_hours(now):
                print(f"[ANALYSIS] Outside trading hours (NY {ny_hour(now)}:00). Sleeping 5m.", flush=True)
                await asyncio.sleep(300)
                continue

            can_trade, reason = self.risk_bridge.can_trade(
                self.executor.balance, self.executor.initial_balance,
                self.executor.daily_pnl, self.executor.peak_balance,
            )
            if not can_trade:
                print(f"[ANALYSIS] Trading paused: {reason}. Sleeping 5m.", flush=True)
                await asyncio.sleep(300)
                continue

            # Reset kill switch at midnight UTC
            today = now_utc().strftime("%Y-%m-%d")
            if self._kill_switch.triggered and self._kill_switch.date != today:
                self._kill_switch.triggered = False
                print("[ANALYSIS] Kill switch reset for new trading day.", flush=True)

            if self._kill_switch.triggered:
                print("[ANALYSIS] Kill switch active (2% daily loss limit). Sleeping 5m.", flush=True)
                await asyncio.sleep(300)
                continue

            if not self.health_monitor.tv_healthy:
                print("[ANALYSIS] TradingView disconnected — skipping analysis. Sleeping 30s.", flush=True)
                await asyncio.sleep(30)
                continue

            near_news, news_event = is_high_impact_news_window(now)
            if near_news:
                print(f"[ANALYSIS] Near high-impact news: {news_event} — skipping cycle. Sleeping 5m.", flush=True)
                await asyncio.sleep(300)
                continue

            try:
                await self._analysis_cycle()
            except Exception as e:
                print(f"[ANALYSIS] Error: {e}", flush=True)
                traceback.print_exc()

            self._cycle_count += 1
            await asyncio.sleep(self.analysis_interval)

    async def _analysis_cycle(self) -> None:
        """Run one full analysis cycle across all symbols."""
        now = now_utc()
        print(f"\n[CYCLE {self._cycle_count}] Starting analysis @ {now.strftime('%H:%M:%S')} UTC", flush=True)

        for symbol in self.symbols:
            if not symbol_is_active(symbol, now):
                print(f"  [{symbol}] Outside session window — skipping", flush=True)
                continue
            try:
                self.analysis.run(symbol)
            except Exception as e:
                print(f"[CYCLE] {symbol} error: {e}", flush=True)

    # ------------------------------------------------------------------
    # Position loop
    # ------------------------------------------------------------------

    async def _position_loop(self) -> None:
        """Check open positions for SL/TP/trailing stop hits."""
        print("[POSITIONS] Loop started", flush=True)

        while self._running:
            if self.executor.open_positions:
                try:
                    events = await asyncio.get_running_loop().run_in_executor(
                        None, self.position_manager.check_positions_sync
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
                        self.state_store.save(self.executor, self.mode)
                        self.drawings.remove(event.get("ticket"))
                        await self.alerts.send_trade_close(
                            symbol=event["symbol"],
                            direction=event.get("direction", ""),
                            exit_price=event.get("exit_price", 0.0),
                            pnl=event["pnl"], r_multiple=event["r_multiple"],
                            reason=event["reason"], balance=event["balance"],
                            ticket=event["ticket"], mode=self.mode,
                        )
                except Exception as e:
                    print(f"[POSITIONS] Error: {e}", flush=True)

            # Paper shadow position check
            if self.paper_shadow is not self.executor and self.paper_shadow.open_positions:
                try:
                    shadow_events = self.position_manager.check_paper_positions()
                    for event in shadow_events:
                        pnl_str = f"+${event['pnl']:.2f}" if event['pnl'] >= 0 else f"-${abs(event['pnl']):.2f}"
                        print(
                            f"  [PAPER] {event['symbol']} {event['reason']} "
                            f"PnL={pnl_str} ({event['r_multiple']:.1f}R) "
                            f"Paper balance=${event['balance']:,.2f}",
                            flush=True,
                        )
                        self.session.log_trade({
                            "event": "PAPER_CLOSE",
                            **event,
                            "mode": "paper_shadow",
                        })
                        self.drawings.remove(f"paper_{event.get('ticket')}")
                    if shadow_events:
                        self.paper_state_store.save(self.paper_shadow, "paper_shadow")
                except Exception as e:
                    print(f"[PAPER] Shadow position check error: {e}", flush=True)

            await asyncio.sleep(self.position_interval)

    # ------------------------------------------------------------------
    # Health loop
    # ------------------------------------------------------------------

    async def _health_loop(self) -> None:
        """Periodic health check and state logging."""
        print("[HEALTH] Loop started", flush=True)

        while self._running:
            await asyncio.sleep(self.health_interval)
            self.health_monitor.run_check(self._cycle_count, self.symbols)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _save_end_of_day(self) -> None:
        """Save end-of-day summary."""
        self.health_monitor.save_end_of_day(self._cycle_count, self.symbols)

    def _print_startup_banner(self, restored_positions: list[dict], today_trades: list[dict]) -> None:
        """Print the startup banner with system status."""
        knowledge_loaded = bool(self._knowledge.get("symbol_profiles"))
        n_profiles = len(self._knowledge.get("symbol_profiles", {}))
        W = 62
        print(f"\n{'='*W}", flush=True)
        print(f"  Auto-Trading Bridge — {self.mode.upper()} MODE", flush=True)
        print(f"  Started : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", flush=True)
        print(f"  Symbols : {', '.join(self.symbols)}", flush=True)
        print(f"  Balance : ${self.executor.balance:,.2f}  "
              f"(initial=${self.executor.initial_balance:,.2f})", flush=True)
        daily_pnl = self.executor.balance - self.executor.initial_balance
        pnl_sign = "+" if daily_pnl >= 0 else ""
        print(f"  Daily P&L: {pnl_sign}${daily_pnl:,.2f}  "
              f"W={self.executor.wins} L={self.executor.losses}", flush=True)
        print(f"  Strategy: {'LOADED' if knowledge_loaded else 'NOT FOUND'} "
              f"({n_profiles} profiles)", flush=True)
        print(f"  Interval: {self.analysis_interval}s", flush=True)

        if restored_positions:
            print(f"\n  RESTORED {len(restored_positions)} OPEN POSITION(S):", flush=True)
            for p in restored_positions:
                opened = p.get("opened_at", "")[:16].replace("T", " ")
                print(
                    f"    #{p['ticket']} {p['direction']} {p['symbol']}  "
                    f"Entry={p['entry_price']:,.4f}  SL={p['sl_price']:,.4f}  "
                    f"Grade={p.get('ict_grade','?')}  @ {opened} UTC",
                    flush=True,
                )
        else:
            print(f"\n  No open positions restored.", flush=True)

        # Today's trades display
        closed = [t for t in today_trades if t.get("event") in ("CLOSE", "PAPER_CLOSE")]
        opens  = [t for t in today_trades if t.get("event") in ("OPEN", "PAPER_OPEN")]
        closed_tickets = {t.get("ticket") for t in closed}
        active_tickets = set(self.executor.open_positions.keys())
        if self.paper_shadow is not self.executor:
            active_tickets |= set(self.paper_shadow.open_positions.keys())
        still_open = [t for t in opens if t.get("ticket") in active_tickets]
        orphaned_open = [t for t in opens
                         if t.get("ticket") not in active_tickets
                         and t.get("ticket") not in closed_tickets]

        if today_trades:
            print(f"\n  TODAY'S TRADES ({len(still_open)} open, {len(closed)} closed"
                  f"{f', {len(orphaned_open)} untracked' if orphaned_open else ''}):", flush=True)
            for t in today_trades:
                evt = t.get("event", "?")
                sym = t.get("symbol", "")
                ts  = t.get("timestamp", "")[:16].replace("T", " ")
                ticket = t.get("ticket", "")
                if evt in ("OPEN", "PAPER_OPEN"):
                    if ticket in active_tickets:
                        label = "OPEN "
                    elif ticket in closed_tickets:
                        continue
                    else:
                        label = "GONE "
                    print(
                        f"    {label} #{ticket} {t.get('direction','')} {sym}"
                        f"  @ {t.get('entry_price', t.get('entry', '')):,.4f}"
                        f"  Grade={t.get('ict_grade','?')}  {ts} UTC",
                        flush=True,
                    )
                elif evt in ("CLOSE", "PAPER_CLOSE"):
                    pnl = t.get("pnl", 0)
                    sign = "+" if pnl >= 0 else ""
                    result = "WIN " if pnl >= 0 else "LOSS"
                    print(
                        f"    {result} #{ticket} {t.get('direction','')} {sym}"
                        f"  Entry={t.get('entry','')}  Exit={t.get('exit','')}  "
                        f"PnL={sign}${pnl:.2f} ({t.get('r_multiple',0):+.1f}R)"
                        f"  {ts} UTC",
                        flush=True,
                    )
        else:
            print(f"\n  No trades today yet.", flush=True)

        # Paper shadow positions
        if self.paper_shadow is not self.executor and self.paper_shadow.open_positions:
            print(f"\n  PAPER SHADOW ({len(self.paper_shadow.open_positions)} open):", flush=True)
            for pos in self.paper_shadow.open_positions.values():
                print(
                    f"    P-#{pos.ticket} {pos.direction} {pos.symbol}  "
                    f"Entry={pos.entry_price:,.4f}  SL={pos.sl_price:,.4f}  "
                    f"Grade={pos.ict_grade}  W/L={self.paper_shadow.wins}/{self.paper_shadow.losses}",
                    flush=True,
                )

        print(f"{'='*W}\n", flush=True)


# Keep main() here for backward compatibility (auto_trade.py imports it)
def main():
    from bridge.cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
