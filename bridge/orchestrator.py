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

from bridge.config import get_bridge_config, BridgeConfig, price_in_range, PRICE_RANGES
from bridge.price_verify import PriceVerifier
from bridge.tv_client import TVClient, TVClientError
from bridge.ict_pipeline import ICTPipeline, SymbolAnalysis
from bridge.claude_decision import ClaudeDecisionMaker
from bridge.decision_types import TradeDecision
from bridge.paper_executor import PaperExecutor
from bridge.live_executor import LiveExecutor
from bridge.session_store import SessionStore
from bridge.state_store import StateStore
from bridge.risk_bridge import RiskBridge
from bridge.alerts import BridgeAlerts
from bridge.strategy_engine import StrategyEngine


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
    """Get current hour in New York time (handles DST automatically)."""
    from zoneinfo import ZoneInfo
    ny = dt.astimezone(ZoneInfo("America/New_York"))
    return ny.hour


def _is_lunch_pause(dt: datetime) -> bool:
    """12:00-13:00 NY = low-volume lunch hour."""
    h = _ny_hour(dt)
    return h == 12


def _is_high_impact_news_window(dt: datetime) -> tuple[bool, str]:
    """
    Check if we're within 15 minutes of a known high-impact news event.

    Returns (is_near_news, event_name).
    Uses static schedule for recurring monthly events (FOMC, NFP, CPI).
    """
    from zoneinfo import ZoneInfo
    ny = dt.astimezone(ZoneInfo("America/New_York"))
    day = ny.day
    weekday = ny.weekday()  # 0=Mon
    hour, minute = ny.hour, ny.minute
    current_min = hour * 60 + minute

    # NFP: First Friday of month at 8:30 AM ET
    if weekday == 4 and day <= 7:
        nfp_min = 8 * 60 + 30
        if abs(current_min - nfp_min) <= 15:
            return True, "NFP (Non-Farm Payrolls)"

    # CPI: Usually 2nd Tuesday-Thursday of month at 8:30 AM ET
    # Approximate: days 10-14
    if 10 <= day <= 14 and weekday in (1, 2, 3):
        cpi_min = 8 * 60 + 30
        if abs(current_min - cpi_min) <= 15:
            return True, "CPI (Consumer Price Index)"

    # FOMC: Usually Wed at 2:00 PM ET, roughly every 6 weeks
    # Known 2026 FOMC dates (announcement at 14:00 ET):
    # Jan 28-29, Mar 18-19, May 6-7, Jun 17-18, Jul 29-30, Sep 16-17, Nov 4-5, Dec 16-17
    fomc_dates = {
        (1, 29), (3, 19), (5, 7), (6, 18), (7, 30), (9, 17), (11, 5), (12, 17)
    }
    if (ny.month, day) in fomc_dates:
        fomc_min = 14 * 60  # 2:00 PM ET
        if abs(current_min - fomc_min) <= 15:
            return True, "FOMC Rate Decision"

    return False, ""


def _utc_hour(dt: datetime) -> int:
    return dt.hour


# Per-symbol trading windows (UTC hours, inclusive start, exclusive end)
# Each entry is a list of (start_utc, end_utc) tuples
_SYMBOL_SESSIONS: dict[str, list[tuple[int, int]]] = {
    # Indices — London open + NY session only (futures market hours)
    "CBOT:YM1!":  [(7, 22)],           # London 7 UTC + NY close 22 UTC (futures close 5pm ET = 21:00 UTC EDT)
    # US indices CFDs — nearly 24/7 (daily maintenance break 5-6pm ET = 21-22 UTC EDT)
    "CAPITALCOM:US500": [(0, 21), (22, 24)], # S&P 500 CFD — skip 21:00-22:00 UTC maintenance
    "CAPITALCOM:US100": [(0, 21), (22, 24)], # Nasdaq 100 CFD — skip 21:00-22:00 UTC maintenance
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
_ALWAYS_ON = {
    "BITSTAMP:BTCUSD", "COINBASE:ETHUSD", "COINBASE:SOLUSD",
    "COINBASE:AVAXUSD", "COINBASE:LINKUSD", "COINBASE:DOGEUSD",
}


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

    def open_position(self, decision: TradeDecision, lot_size: float | None = None) -> dict:
        """Submit trade to MT5 and mirror state.

        Args:
            decision: Trade decision from Claude
            lot_size: Pre-calculated lot size from RiskBridge (preferred).
                      If not provided, falls back to internal calculation.
        """
        if not decision.is_trade:
            return {"success": False, "ticket": 0, "message": "Not a trade"}

        # Dedup check: same symbol+direction with similar entry price
        for pos in self.open_positions.values():
            if pos.symbol == decision.symbol and pos.direction == decision.action:
                entry_diff = abs(pos.entry_price - decision.entry_price)
                threshold = pos.entry_price * 0.005  # 0.5% tolerance
                if entry_diff < threshold:
                    return {"success": False, "ticket": 0,
                            "message": f"Duplicate: already {pos.direction} {decision.symbol} "
                                       f"@ {pos.entry_price:.4f}"}
            elif pos.symbol == decision.symbol:
                return {"success": False, "ticket": 0,
                        "message": f"Already have {pos.direction} position on {decision.symbol}"}

        # Use pre-calculated lot size from RiskBridge if provided
        if lot_size is None:
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
                closed_at = datetime.now(timezone.utc).isoformat()
                exit_level = pos.sl_price if closed_reason == "SL" else (
                    pos.tp2_price if pos.tp2_price > 0 else pos.tp_price)
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
                    "direction": pos.direction,
                    "entry_price": pos.entry_price,
                    "exit_price": exit_level,
                    "actual_trigger_price": price,
                    "pnl": round(pnl, 2), "r_multiple": round(r_mult, 2),
                    "reason": closed_reason, "balance": round(self.balance, 2),
                    "opened_at": pos.opened_at, "closed_at": closed_at,
                    "sl_price": pos.sl_price, "tp_price": pos.tp_price,
                    "tp2_price": pos.tp2_price, "lot_size": pos.lot_size,
                    "ict_grade": pos.ict_grade, "ict_score": pos.ict_score,
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
        # Shadow paper executor — tracks ALL decisions for auditing, regardless of mode
        self.paper_shadow = PaperExecutor(initial_balance=initial_balance)
        if mode == "live":
            self.executor = LiveExecutorAdapter(initial_balance=initial_balance)
            print("[ORCH] Mode: LIVE — trades will be sent to MT5", flush=True)
            print("[ORCH] Shadow paper executor running in parallel for audit", flush=True)
        else:
            self.executor = PaperExecutor(initial_balance=initial_balance)
        self.session = SessionStore()
        self.state_store = StateStore()
        # Separate state file for paper shadow — survives restarts
        self.paper_state_store = StateStore(
            path=Path.home() / ".tradingview-mcp" / "paper_shadow_state.json"
        )
        self.tv_client = TVClient()
        self.risk_bridge = RiskBridge()
        self.alerts = BridgeAlerts()
        self.strategy_engine = StrategyEngine()
        self.price_verifier = PriceVerifier()

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
        self._trade_drawings: dict[int | str, list[str]] = {}  # ticket/paper_N -> [entity_id, ...]
        self._drawings_path = Path.home() / ".tradingview-mcp" / "trade_drawings.json"
        self._restore_trade_drawings()
        self._last_eod_date: str = ""  # date of last end-of-day summary (YYYY-MM-DD ET)

        # Decision cooldown: don't call Claude for the same symbol if we recently got a
        # BUY/SELL or high-confidence SKIP. Saves API costs on repeated setups.
        self._decision_cooldown: dict[str, float] = {}  # symbol -> UTC timestamp of last decision
        self._cooldown_seconds = 1800  # 30 min cooldown after BUY/SELL decision

        # Score decay: track first signal price per (symbol, direction) to detect
        # when price moves against the thesis — penalizes stale signals.
        self._signal_anchor: dict[str, tuple[str, float]] = {}  # symbol -> (direction, anchor_price)

        # TradingView connectivity tracking
        self._tv_consecutive_failures = 0
        self._tv_healthy = True

    # ------------------------------------------------------------------
    # Trade drawing persistence
    # ------------------------------------------------------------------

    def _save_trade_drawings(self) -> None:
        """Persist trade drawing entity IDs to disk so they survive restarts."""
        try:
            # Convert keys to strings for JSON serialization
            data = {str(k): v for k, v in self._trade_drawings.items()}
            self._drawings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _restore_trade_drawings(self) -> None:
        """Load saved trade drawing entity IDs from disk."""
        if not self._drawings_path.exists():
            return
        try:
            data = json.loads(self._drawings_path.read_text(encoding="utf-8"))
            for k, v in data.items():
                # Restore int keys for numeric tickets, string keys for paper_N
                try:
                    self._trade_drawings[int(k)] = v
                except ValueError:
                    self._trade_drawings[k] = v
        except Exception:
            pass

    def _cleanup_stale_drawings(self) -> None:
        """Remove chart drawings for positions that are no longer open.

        Uses saved entity IDs first (fast, precise). Then falls back to
        scanning chart drawings by text pattern for any orphaned lines
        from sessions where entity IDs weren't persisted.
        """
        # Collect active tickets (live + paper)
        active_tickets: set[str] = set()
        for ticket in self.executor.open_positions:
            active_tickets.add(str(ticket))
        if self.paper_shadow is not self.executor:
            for ticket in self.paper_shadow.open_positions:
                active_tickets.add(str(ticket))

        # Step 1: Remove drawings for closed positions via saved entity IDs
        stale_keys = []
        for key, entity_ids in self._trade_drawings.items():
            key_str = str(key)
            # paper_123 → ticket "123"
            ticket_str = key_str.replace("paper_", "")
            if ticket_str not in active_tickets:
                stale_keys.append(key)

        removed_tracked = 0
        for key in stale_keys:
            entity_ids = self._trade_drawings.pop(key, [])
            try:
                self.tv_client.draw_remove_trade(entity_ids)
                removed_tracked += len(entity_ids)
            except Exception:
                pass

        # Step 2: Scan chart for any orphaned trade drawings (from pre-persistence sessions)
        removed_orphan = self.tv_client.draw_remove_stale_trades(active_tickets)

        total = removed_tracked + removed_orphan
        if total > 0:
            print(f"  [DRAW] Cleaned up {total} stale trade drawing(s) "
                  f"({removed_tracked} tracked, {removed_orphan} orphaned)", flush=True)
            self._save_trade_drawings()

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the orchestrator with all concurrent loops."""
        self._running = True

        # --- Restore state from previous session (if same day, same mode) ---
        restored_positions = self.state_store.restore_into(self.executor, self.mode)

        # --- Restore paper shadow state (always, regardless of mode) ---
        if self.paper_shadow is not self.executor:
            paper_restored = self.paper_state_store.restore_into(self.paper_shadow, "paper_shadow")
            if paper_restored:
                print(f"[PAPER] Restored {len(paper_restored)} shadow position(s) from state", flush=True)

        # --- Mirror live MT5 positions into paper shadow ---
        # Ensures paper always tracks what live is doing, even across restarts
        if self.paper_shadow is not self.executor and restored_positions:
            self._mirror_live_to_paper(restored_positions)

        # --- Load today's closed trades from session store for display ---
        today_trades = self._load_todays_trades()

        # --- Reconcile restored positions against live prices ---
        if restored_positions:
            await asyncio.get_running_loop().run_in_executor(
                None, self._reconcile_restored_positions
            )

        # --- Clean up stale trade drawings from closed/expired positions ---
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, self._cleanup_stale_drawings
            )
        except Exception as e:
            print(f"  [DRAW] Startup cleanup error (non-fatal): {e}", flush=True)

        # --- Startup banner ---
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

        # Restored open positions
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

        # Today's trades — cross-reference OPENs with CLOSEs to avoid showing
        # stale "OPEN" entries for positions that were closed but CLOSE wasn't logged.
        closed = [t for t in today_trades if t.get("event") in ("CLOSE", "PAPER_CLOSE")]
        opens  = [t for t in today_trades if t.get("event") in ("OPEN", "PAPER_OPEN")]
        closed_tickets = {t.get("ticket") for t in closed}
        # An OPEN is only truly open if its ticket is still in the executor
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
                        continue  # CLOSE event will show it
                    else:
                        label = "GONE "  # position lost (bridge restart, no CLOSE logged)
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

            # Check TradingView connectivity
            if not self._tv_healthy:
                print("[ANALYSIS] TradingView disconnected — skipping analysis. Sleeping 30s.", flush=True)
                await asyncio.sleep(30)
                continue

            # High-impact news guard — skip analysis near FOMC/NFP/CPI
            near_news, news_event = _is_high_impact_news_window(now)
            if near_news:
                print(f"[ANALYSIS] Near high-impact news: {news_event} — skipping cycle. Sleeping 5m.", flush=True)
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

    # Price validation uses shared config.PRICE_RANGES / config.price_in_range
    # Single source of truth — no duplicate dicts to go out of sync.

    def _fallback_paper_lots(self, decision: "TradeDecision") -> float:
        """Calculate a reasonable paper lot size when risk gate doesn't provide one."""
        if decision.entry_price <= 0 or decision.sl_price <= 0:
            return 1.0
        risk_usd = self.paper_shadow.balance * (decision.risk_pct or 0.0075)
        sl_distance = abs(decision.entry_price - decision.sl_price)
        if sl_distance <= 0:
            return 1.0
        return round(risk_usd / sl_distance, 4)

    def _analyze_and_decide(self, symbol: str) -> None:
        """Analyze a single symbol and make a trade decision."""
        # Kill switch — check BEFORE doing any analysis or API calls
        if self._kill_switch_triggered:
            today = _now_utc().strftime("%Y-%m-%d")
            if self._kill_switch_date == today:
                print(f"  [{symbol}] Kill switch active — skipping (daily loss limit hit)", flush=True)
                return
            else:
                # New day — reset
                self._kill_switch_triggered = False
                self._kill_switch_date = ""

        # Run ICT pipeline (synchronous — subprocess calls)
        analysis = self.pipeline.analyze_symbol(symbol)

        # Always log the full analysis — even if we skip the trade.
        # This is the core audit trail for verifying setup quality.
        ea_signals = []
        skip_reason = None

        # Skip if data could not be fetched (chart still loading, symbol unavailable, etc.)
        if analysis.error == "DATA_UNAVAILABLE":
            skip_reason = "DATA_UNAVAILABLE"
            print(f"  [{symbol}] DATA_UNAVAILABLE — skipping (chart not loaded)", flush=True)

        # Price sanity check — reject if price is outside expected range for this symbol
        elif analysis.current_price > 0 and not price_in_range(symbol, analysis.current_price):
            rng = PRICE_RANGES.get(symbol.split(":")[-1], ("?", "?"))
            skip_reason = f"PRICE_ERROR: got {analysis.current_price:.4f}, expected {rng[0]}-{rng[1]}"
            print(f"  [{symbol}] {skip_reason}. Chart not switched correctly, skipping.", flush=True)

        # Sweep gate: liquidity sweep is required for any trade
        elif not analysis.sweep_detected:
            skip_reason = "NO_SWEEP"
            print(f"  [{symbol}] NO_SWEEP — skipping (no liquidity sweep detected)", flush=True)

        else:
            # Run EA+ICT strategy ensemble in parallel
            try:
                ea_signals = self.strategy_engine.process_symbol(symbol)
            except Exception as e:
                print(f"  [{symbol}] EA ensemble error: {e}", flush=True)

        # Log full analysis with skip reason if applicable
        log_entry = analysis.to_dict()
        log_entry["ea_signals"] = len(ea_signals)
        if ea_signals:
            log_entry["ea_direction"] = ea_signals[0].direction.value
            log_entry["ea_score"] = ea_signals[0].final_score
        if skip_reason:
            log_entry["skip_reason"] = skip_reason
        self.session.log_analysis(log_entry)

        # If skipped, don't proceed to decision
        if skip_reason:
            return

        ea_info = ""
        if ea_signals:
            sig = ea_signals[0]
            ea_info = f" | EA: {sig.direction.value} {sig.final_score:.0f}/100 ({sig.strategy_count} strats)"

        print(
            f"  [{symbol}] Grade {analysis.grade} ({analysis.total_score:.0f}/100) "
            f"{analysis.direction} | {len(analysis.confluence_factors)} confluence{ea_info} | "
            f"struct={analysis.structure_score:.0f} ob={analysis.ob_score:.0f} fvg={analysis.fvg_score:.0f} "
            f"sess={analysis.session_score:.0f} ote={analysis.ote_score:.0f} smt={analysis.smt_score:.0f} "
            f"sweep={'Y' if analysis.sweep_detected else 'N'} kz={'Y' if analysis.is_kill_zone else 'N'}",
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

        # --- Score decay: penalize when price moves against the signal direction ---
        # If the same direction signal persists but price keeps moving the wrong way,
        # decay the score to prevent repeated entries into a losing thesis.
        if analysis.current_price > 0 and analysis.direction in ("BULLISH", "BEARISH"):
            anchor_key = symbol
            prev = self._signal_anchor.get(anchor_key)
            if prev and prev[0] == analysis.direction:
                # Same direction as before — check if price moved against it
                anchor_price = prev[1]
                if analysis.direction == "BULLISH":
                    adverse_move_pct = (anchor_price - analysis.current_price) / anchor_price * 100
                else:
                    adverse_move_pct = (analysis.current_price - anchor_price) / anchor_price * 100

                if adverse_move_pct > 0.05:  # Price moved against thesis by >0.05%
                    # Decay: 3% score reduction per 0.1% adverse move, capped at 20% total decay
                    decay_factor = min(adverse_move_pct / 0.1 * 0.03, 0.20)
                    original_score = analysis.total_score
                    analysis.total_score *= (1.0 - decay_factor)
                    analysis.confluence_factors.append(
                        f"score_decay(-{decay_factor*100:.0f}%, price moved {adverse_move_pct:.2f}% against {analysis.direction})"
                    )
                    # Re-grade after decay
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
                    print(
                        f"  [{symbol}] Score decay: {original_score:.0f} -> {analysis.total_score:.0f} "
                        f"(price {adverse_move_pct:.2f}% against {analysis.direction})",
                        flush=True,
                    )
            else:
                # New direction or first signal — set anchor
                self._signal_anchor[anchor_key] = (analysis.direction, analysis.current_price)

            # Reset anchor if direction flips
            if prev and prev[0] != analysis.direction:
                self._signal_anchor[anchor_key] = (analysis.direction, analysis.current_price)

        # Cooldown check: skip Claude API call if we recently got a BUY/SELL for this symbol
        import time as _time
        last_decision_time = self._decision_cooldown.get(symbol, 0)
        if _time.time() - last_decision_time < self._cooldown_seconds:
            mins_left = int((self._cooldown_seconds - (_time.time() - last_decision_time)) / 60)
            print(f"  [{symbol}] Cooldown active ({mins_left}m remaining) — skipping Claude call", flush=True)
            return

        # Claude decision (ICT analysis + EA context + strategy knowledge in prompt)
        decision = self.decision_maker.evaluate(analysis)

        # Set cooldown if Claude decided to trade (avoid repeated BUY calls)
        if decision.is_trade:
            self._decision_cooldown[symbol] = _time.time()

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

        # ── Paper shadow: ALWAYS record trade decisions for audit ──
        # Must run BEFORE kill switch / correlation gates so paper captures
        # every trade Claude decides on, even if live blocks it.
        if decision.is_trade and self.paper_shadow is not self.executor:
            paper_lot = self._fallback_paper_lots(decision)
            live_block_reason = ""
            try:
                shadow_result = self.paper_shadow.open_position(decision, lot_size=paper_lot)
                if shadow_result["success"]:
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
                    # Draw paper trade on TradingView (P- prefix distinguishes from live)
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
                            self._trade_drawings[f"paper_{paper_ticket}"] = entity_ids
                            self._save_trade_drawings()
                    except Exception:
                        pass
                    self.paper_state_store.save(self.paper_shadow, "paper_shadow")
            except Exception as e:
                print(f"  [{symbol}] Paper shadow error: {e}", flush=True)

        # Check daily loss kill switch (2%)
        daily_pnl_pct = self.executor.daily_pnl / self.executor.initial_balance if self.executor.initial_balance else 0
        if daily_pnl_pct <= -0.02 and not self._kill_switch_triggered:
            self._kill_switch_triggered = True
            self._kill_switch_date = _now_utc().strftime("%Y-%m-%d")
            print(f"[KILL SWITCH] Daily loss limit 2% reached ({daily_pnl_pct:.2%}). Trading HALTED.", flush=True)
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(asyncio.ensure_future, self.alerts.send_raw(
                    f"🛑 *KILL SWITCH: Daily loss limit 2% reached* ({daily_pnl_pct:.2%})\nTrading paused until midnight UTC."
                ))
            except RuntimeError:
                pass
            return

        # Execute if it's a trade
        if decision.is_trade:
            # Apply per-symbol risk override from rules.json
            risk_override = _get_symbol_risk_override(symbol, decision.grade, self._rules)
            if risk_override is not None and decision.risk_pct > risk_override:
                print(f"  [{symbol}] Risk override: {decision.risk_pct:.1%} -> {risk_override:.1%} (per-symbol limit)", flush=True)
                decision.risk_pct = risk_override

            # Correlation gate: block concentrated risk (same symbol or SMT pair same direction)
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
                print(f"  [{symbol}] OPENED: {result['message']}", flush=True)
                # Log full trade detail — not just the result dict
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
                        self._save_trade_drawings()
                        print(f"  [{symbol}] Chart: {len(entity_ids)} lines drawn (IDs: {entity_ids})", flush=True)
                except Exception:
                    pass
                # Send alert (fire-and-forget)
                _dec, _lot, _ticket = decision, lot_size, result["ticket"]
                try:
                    _loop = asyncio.get_running_loop()
                    _loop.call_soon_threadsafe(asyncio.ensure_future,
                        self.alerts.send_trade_open(
                            symbol=_dec.symbol, direction=_dec.action,
                            entry_price=_dec.entry_price, sl_price=_dec.sl_price,
                            tp_price=_dec.tp_price, tp2_price=_dec.tp2_price,
                            lot_size=_lot, grade=_dec.grade, score=_dec.ict_score,
                            confidence=_dec.confidence, reasoning=_dec.reasoning,
                            ticket=_ticket, mode=self.mode,
                        )
                    )
                except RuntimeError:
                    pass
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
                    events = await asyncio.get_running_loop().run_in_executor(
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
                        self.state_store.save(self.executor, self.mode)
                        # Remove only this trade's chart lines (never touches other drawings)
                        ticket = event.get("ticket")
                        if ticket and ticket in self._trade_drawings:
                            try:
                                self.tv_client.draw_remove_trade(self._trade_drawings.pop(ticket))
                                self._save_trade_drawings()
                            except Exception:
                                pass
                        # Send close alert
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

            # Shadow paper executor — check positions (Alpaca-first for crypto, TV fallback)
            if self.paper_shadow is not self.executor and self.paper_shadow.open_positions:
                try:
                    shadow_prices: dict[str, float] = {}
                    for pos in self.paper_shadow.open_positions.values():
                        try:
                            # Try Alpaca first for crypto — fast, no chart switching
                            alpaca_price = self.price_verifier.get_alpaca_price(pos.symbol)
                            if alpaca_price and alpaca_price > 0 and price_in_range(pos.symbol, alpaca_price):
                                shadow_prices[pos.symbol] = alpaca_price
                                continue

                            # Fallback to TradingView chart quote
                            sym_base = pos.symbol.split(":")[-1]
                            result = self.tv_client.set_symbol(pos.symbol, require_ready=True)
                            if not result.get("chart_ready", False):
                                continue
                            quote = self.tv_client.get_quote()
                            chart_sym = quote.get("symbol", "").split(":")[-1]
                            if chart_sym != sym_base:
                                continue
                            p = float(quote.get("last") or quote.get("lp") or quote.get("close") or 0)
                            if p > 0 and price_in_range(pos.symbol, p):
                                shadow_prices[pos.symbol] = p
                        except Exception:
                            pass
                    if shadow_prices:
                        shadow_events = self.paper_shadow.check_positions(shadow_prices)
                        for event in shadow_events:
                            pnl_str = f"+${event['pnl']:.2f}" if event['pnl'] >= 0 else f"-${abs(event['pnl']):.2f}"
                            print(
                                f"  [PAPER] {event['symbol']} {event['reason']} "
                                f"PnL={pnl_str} ({event['r_multiple']:.1f}R) "
                                f"Paper balance=${event['balance']:,.2f}",
                                flush=True,
                            )
                            # Log paper close to session
                            self.session.log_trade({
                                "event": "PAPER_CLOSE",
                                **event,
                                "mode": "paper_shadow",
                            })
                            # Remove paper trade chart drawings
                            paper_key = f"paper_{event.get('ticket')}"
                            if paper_key in self._trade_drawings:
                                try:
                                    self.tv_client.draw_remove_trade(self._trade_drawings.pop(paper_key))
                                    self._save_trade_drawings()
                                except Exception:
                                    pass
                        # Persist paper shadow state after closes
                        if shadow_events:
                            self.paper_state_store.save(self.paper_shadow, "paper_shadow")
                except Exception as e:
                    print(f"[PAPER] Shadow position check error: {e}", flush=True)

            await asyncio.sleep(self.position_interval)

    def _mirror_live_to_paper(self, restored_positions: list[dict]) -> None:
        """Mirror bridge-opened live positions into paper shadow.

        Only mirrors positions from `restored_positions` (which come from the
        bridge's own state_store.py — NOT from MT5 directly). This means only
        trades opened by our bridge system are mirrored, never trades from
        other EAs or manual trades on the same MT5 account.
        """
        paper_symbols_tickets = {
            (p.symbol, p.entry_price) for p in self.paper_shadow.open_positions.values()
        }

        mirrored = 0
        for pos_dict in restored_positions:
            key = (pos_dict.get("symbol", ""), pos_dict.get("entry_price", 0))
            if key in paper_symbols_tickets:
                continue  # already in paper

            # Build a minimal TradeDecision to open in paper
            from bridge.decision_types import TradeDecision
            decision = TradeDecision(
                action=pos_dict.get("direction", "BUY"),
                symbol=pos_dict.get("symbol", ""),
                entry_price=pos_dict.get("entry_price", 0),
                sl_price=pos_dict.get("sl_price", 0),
                tp_price=pos_dict.get("tp_price", 0),
                tp2_price=pos_dict.get("tp2_price", 0),
                confidence=80,
                risk_pct=pos_dict.get("risk_pct", 0.0075),
                reasoning="Mirrored from live MT5 position on startup",
                grade=pos_dict.get("ict_grade", "B"),
                ict_score=pos_dict.get("ict_score", 0),
                model_used="mirror",
            )
            lot_size = pos_dict.get("lot_size", self._fallback_paper_lots(decision))
            try:
                result = self.paper_shadow.open_position(decision, lot_size=lot_size)
                if result["success"]:
                    mirrored += 1
                    print(f"  [PAPER] Mirrored live #{pos_dict.get('ticket')} "
                          f"{pos_dict.get('symbol')} @ {pos_dict.get('entry_price')}", flush=True)
            except Exception as e:
                print(f"  [PAPER] Mirror error: {e}", flush=True)

        if mirrored:
            self.paper_state_store.save(self.paper_shadow, "paper_shadow")
            print(f"[PAPER] Mirrored {mirrored} live position(s) into paper shadow", flush=True)

    def _reconcile_restored_positions(self) -> None:
        """Check restored positions against live prices — close any that hit SL/TP while bridge was down."""
        if not self.executor.open_positions:
            return

        print("[RECONCILE] Checking restored positions against live prices...", flush=True)

        # --- Step 0: Check MT5 for broker-side closes (SL/TP hit while bridge was down) ---
        if isinstance(self.executor, LiveExecutorAdapter):
            self._sync_mt5_closed_positions()
            # If MT5 closed all positions, we're done
            if not self.executor.open_positions:
                print("  [RECONCILE] All positions were closed broker-side (MT5)", flush=True)
                return

        to_close: list[tuple[int, str, float]] = []

        for ticket, pos in list(self.executor.open_positions.items()):
            try:
                target_sym = pos.symbol.split(":")[-1]
                result = self.tv_client.set_symbol(pos.symbol, require_ready=True)
                if not result.get("chart_ready", False):
                    print(f"  [RECONCILE] {pos.symbol} chart not ready — will check in position loop", flush=True)
                    continue

                quote = self.tv_client.get_quote()
                chart_sym = quote.get("symbol", "").split(":")[-1]
                if chart_sym != target_sym:
                    print(f"  [RECONCILE] Symbol mismatch for {pos.symbol} — skipping", flush=True)
                    continue

                price = float(quote.get("last") or quote.get("lp") or quote.get("close") or 0)
                if price <= 0:
                    continue

                # Alpaca cross-check
                price_ok, _ = self.price_verifier.verify(pos.symbol, price)
                if not price_ok:
                    print(f"  [RECONCILE] {pos.symbol} price verification failed — skipping", flush=True)
                    continue

                if not price_in_range(pos.symbol, price):
                    continue

                # Check if SL or TP was already hit
                if pos.direction == "BUY":
                    if price <= pos.sl_price:
                        to_close.append((ticket, "SL (while offline)", pos.sl_price))
                    elif price >= (pos.tp2_price if pos.tp2_price > 0 else pos.tp_price):
                        exit_p = pos.tp2_price if pos.tp2_price > 0 else pos.tp_price
                        to_close.append((ticket, "TP (while offline)", exit_p))
                    else:
                        pnl = (price - pos.entry_price) * pos.lot_size
                        print(f"  [RECONCILE] #{ticket} {pos.symbol} STILL OPEN — price {price:.4f} (PnL {pnl:+.2f})", flush=True)
                else:
                    if price >= pos.sl_price:
                        to_close.append((ticket, "SL (while offline)", pos.sl_price))
                    elif price <= (pos.tp2_price if pos.tp2_price > 0 else pos.tp_price):
                        exit_p = pos.tp2_price if pos.tp2_price > 0 else pos.tp_price
                        to_close.append((ticket, "TP (while offline)", exit_p))
                    else:
                        pnl = (pos.entry_price - price) * pos.lot_size
                        print(f"  [RECONCILE] #{ticket} {pos.symbol} STILL OPEN — price {price:.4f} (PnL {pnl:+.2f})", flush=True)

            except TVClientError as e:
                print(f"  [RECONCILE] Error checking {pos.symbol}: {e}", flush=True)

        # Close positions that were hit while offline
        for ticket, reason, exit_price in to_close:
            pos = self.executor.open_positions.get(ticket)
            if not pos:
                continue
            if pos.direction == "BUY":
                pnl = (exit_price - pos.entry_price) * pos.lot_size
            else:
                pnl = (pos.entry_price - exit_price) * pos.lot_size
            print(
                f"  [RECONCILE] CLOSING #{ticket} {pos.symbol} — {reason} "
                f"(entry {pos.entry_price:.4f} -> exit {exit_price:.4f}, PnL {pnl:+.2f})",
                flush=True,
            )
            # Use executor's check_positions to handle the close properly
            prices = {pos.symbol: exit_price}
            events = self.executor.check_positions(prices)
            for event in events:
                event["reason"] = reason  # override with offline context
                self.session.log_trade({"event": "CLOSE", **event})

        if to_close:
            self.state_store.save(self.executor, self.mode)
            print(f"  [RECONCILE] Closed {len(to_close)} position(s) that hit SL/TP while offline", flush=True)
        elif self.executor.open_positions:
            print(f"  [RECONCILE] All {len(self.executor.open_positions)} position(s) still valid", flush=True)

    def _check_positions_sync(self) -> list[dict]:
        """Get current prices and check positions.

        Uses Alpaca API directly for crypto (fast, reliable, no chart switching needed).
        Falls back to TradingView chart quotes for non-crypto symbols.
        Also checks MT5 for live positions that may have been closed broker-side.
        """
        prices: dict[str, float] = {}

        # --- Step 1: Check if MT5 already closed any live positions (broker-side SL/TP) ---
        if isinstance(self.executor, LiveExecutorAdapter):
            self._sync_mt5_closed_positions()

        for pos in self.executor.open_positions.values():
            try:
                # Try Alpaca first for crypto — fast, no chart switching
                alpaca_price = self.price_verifier.get_alpaca_price(pos.symbol)
                if alpaca_price and alpaca_price > 0:
                    if price_in_range(pos.symbol, alpaca_price):
                        prices[pos.symbol] = alpaca_price
                        continue
                    else:
                        print(f"[POSITIONS] {pos.symbol} Alpaca price {alpaca_price:.4f} FAILED range check", flush=True)

                # Fallback: TradingView chart quote (for forex, commodities, indices)
                target_sym = pos.symbol.split(":")[-1]
                result = self.tv_client.set_symbol(pos.symbol, require_ready=True)
                if not result.get("chart_ready", False):
                    print(f"[POSITIONS] Chart not ready for {pos.symbol} — skipping this cycle", flush=True)
                    continue

                quote = self.tv_client.get_quote()
                chart_sym = quote.get("symbol", "").split(":")[-1]
                if chart_sym != target_sym:
                    print(f"[POSITIONS] Symbol mismatch: expected {target_sym}, got {chart_sym}", flush=True)
                    continue

                p = float(quote.get("last") or quote.get("lp") or quote.get("close") or 0)
                if p <= 0:
                    print(f"[POSITIONS] Zero price for {pos.symbol}", flush=True)
                    continue

                if not price_in_range(pos.symbol, p):
                    print(f"[POSITIONS] {pos.symbol} price {p:.4f} FAILED range check", flush=True)
                    continue

                prices[pos.symbol] = p

            except TVClientError as e:
                print(f"[POSITIONS] TVClient error for {pos.symbol}: {e}", flush=True)

        if prices:
            return self.executor.check_positions(prices)
        return []

    def _sync_mt5_closed_positions(self) -> None:
        """Check MT5 for positions that were closed broker-side (SL/TP hit on server).

        MT5 manages SL/TP natively — if the broker closed a position, we need to
        sync our local state to reflect that.
        """
        if not isinstance(self.executor, LiveExecutorAdapter):
            return
        try:
            import MetaTrader5 as mt5
            if not mt5.terminal_info():
                return

            to_close = []
            for ticket, pos in self.executor.open_positions.items():
                # Check if MT5 still has this position open
                mt5_pos = mt5.positions_get(ticket=ticket)
                if mt5_pos is None or len(mt5_pos) == 0:
                    # Position no longer exists on MT5 — broker closed it
                    # Get the deal history to find the close price and P&L
                    from datetime import datetime, timezone, timedelta
                    now = datetime.now(timezone.utc)
                    deals = mt5.history_deals_get(
                        now - timedelta(days=3), now,
                        position=ticket
                    )
                    exit_price = pos.current_price
                    pnl = 0.0
                    reason = "BROKER_CLOSE"
                    if deals:
                        # Find the closing deal for THIS symbol (entry=1 means closing deal)
                        mt5_sym = pos.symbol.split(":")[-1]
                        close_deals = [d for d in deals if d.entry == 1 and d.symbol == mt5_sym]
                        if close_deals:
                            last_deal = close_deals[-1]
                            exit_price = last_deal.price
                            pnl = last_deal.profit
                            if last_deal.comment and "sl" in last_deal.comment.lower():
                                reason = "SL"
                            elif last_deal.comment and "tp" in last_deal.comment.lower():
                                reason = "TP"

                    to_close.append((ticket, reason, exit_price, pnl))

            for ticket, reason, exit_price, mt5_pnl in to_close:
                pos = self.executor.open_positions[ticket]
                if pos.direction == "BUY":
                    local_pnl = (exit_price - pos.entry_price) * pos.lot_size
                else:
                    local_pnl = (pos.entry_price - exit_price) * pos.lot_size
                sl_dist = abs(pos.entry_price - pos.sl_price)
                r_multiple = round(local_pnl / (sl_dist * pos.lot_size), 2) if sl_dist > 0 else 0.0

                print(
                    f"  [MT5_SYNC] #{ticket} {pos.symbol} closed by broker ({reason}) "
                    f"Entry={pos.entry_price:.2f} Exit={exit_price:.2f} "
                    f"PnL={local_pnl:+.2f} ({r_multiple:+.1f}R)",
                    flush=True,
                )

                # Update balance and remove from open positions
                self.executor.balance += local_pnl
                if local_pnl >= 0:
                    self.executor.wins += 1
                else:
                    self.executor.losses += 1
                del self.executor.open_positions[ticket]

                # Log the close event
                self.session.log_trade({
                    "event": "CLOSE",
                    "ticket": ticket,
                    "symbol": pos.symbol,
                    "direction": pos.direction,
                    "entry": pos.entry_price,
                    "exit_price": exit_price,
                    "pnl": round(local_pnl, 2),
                    "r_multiple": r_multiple,
                    "reason": reason,
                    "balance": round(self.executor.balance, 2),
                    "mt5_pnl": round(mt5_pnl, 2) if mt5_pnl else None,
                })
                self.state_store.save(self.executor, self.mode)

        except ImportError:
            pass  # MetaTrader5 not installed
        except Exception as e:
            print(f"[MT5_SYNC] Error checking MT5 positions: {e}", flush=True)

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

            # Paper shadow stats
            paper_info = ""
            if self.paper_shadow is not self.executor:
                ps = self.paper_shadow
                paper_info = (
                    f" | Paper: ${ps.balance:,.2f} "
                    f"Open={len(ps.open_positions)} "
                    f"W/L={ps.wins}/{ps.losses}"
                )

            print(
                f"[HEALTH {now.strftime('%H:%M')}] "
                f"Balance=${summary['balance']:,.2f} "
                f"PnL={summary['daily_pnl_pct']} "
                f"DD={summary['total_drawdown_pct']} "
                f"Open={open_pos} "
                f"W/L={summary['wins']}/{summary['losses']}"
                f"{positions_info}{paper_info}",
                flush=True,
            )

            # Save periodic snapshot + persist state for restart recovery
            self.session.save_snapshot(summary)
            self.state_store.save(self.executor, self.mode)

            # TradingView connectivity check
            try:
                tv_ok = self.tv_client.health_check()
                if tv_ok:
                    if not self._tv_healthy:
                        print("[HEALTH] TradingView reconnected!", flush=True)
                        try:
                            asyncio.ensure_future(self.alerts.send_raw(
                                "TradingView RECONNECTED — resuming analysis"
                            ))
                        except Exception:
                            pass
                    self._tv_consecutive_failures = 0
                    self._tv_healthy = True
                else:
                    self._tv_consecutive_failures += 1
                    if self._tv_consecutive_failures >= 3 and self._tv_healthy:
                        self._tv_healthy = False
                        print(
                            f"[HEALTH] WARNING: TradingView unresponsive "
                            f"({self._tv_consecutive_failures} consecutive failures) "
                            f"— pausing new analysis until reconnected",
                            flush=True,
                        )
                        try:
                            asyncio.ensure_future(self.alerts.send_raw(
                                f"WARNING: TradingView DISCONNECTED "
                                f"({self._tv_consecutive_failures} failures). "
                                f"Open positions still monitored via MT5/Alpaca. "
                                f"New analysis paused."
                            ))
                        except Exception:
                            pass
            except Exception:
                self._tv_consecutive_failures += 1

            # Daily summary at 5 PM ET — fires once per day, does NOT stop the loop
            # (system runs 24/7 for crypto; summary is informational only)
            ny_h = _ny_hour(now)
            from zoneinfo import ZoneInfo
            et_date = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            if ny_h == 17 and now.minute < 2 and self._last_eod_date != et_date:
                self._last_eod_date = et_date
                self._save_end_of_day()
                print("[HEALTH] 5pm ET — daily summary sent.", flush=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_todays_trades(self) -> list[dict]:
        """Load today's trade events from the session store for the startup banner."""
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            session_file = Path.home() / ".tradingview-mcp" / "sessions" / f"{today}.json"
            if not session_file.exists():
                return []
            data = json.loads(session_file.read_text(encoding="utf-8"))
            return [t for t in data.get("trades", [])
                    if t.get("event") in ("OPEN", "CLOSE")]
        except Exception:
            return []

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
            loop = asyncio.get_running_loop()
            asyncio.ensure_future(self.alerts.send_daily_summary(summary, self.mode))
        except RuntimeError:
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

    async def _run_with_shutdown():
        loop = asyncio.get_running_loop()

        # Cancel all tasks on SIGINT/SIGTERM — works on Windows too
        def _request_shutdown():
            print("\n[SIGNAL] Ctrl+C received — shutting down cleanly...", flush=True)
            orch.stop()
            for task in asyncio.all_tasks(loop):
                task.cancel()

        # Windows: signal module only supports SIGINT in main thread via add_signal_handler on Unix.
        # Use signal.signal() which works on Windows for SIGINT.
        import signal as _signal
        _signal.signal(_signal.SIGINT, lambda s, f: loop.call_soon_threadsafe(_request_shutdown))
        if hasattr(_signal, "SIGTERM"):
            _signal.signal(_signal.SIGTERM, lambda s, f: loop.call_soon_threadsafe(_request_shutdown))

        try:
            await orch.run()
        except asyncio.CancelledError:
            pass
        finally:
            print("[ORCH] Saving session and exiting...", flush=True)
            orch._save_end_of_day()
            # Print final account summary
            summary = orch.executor.get_account_summary()
            print(
                f"\n[SESSION END]\n"
                f"  Balance : ${summary['balance']:,.2f}\n"
                f"  Daily P&L: {summary['daily_pnl_pct']}\n"
                f"  Trades  : W={summary['wins']} L={summary['losses']}\n"
                f"  Cycles  : {orch._cycle_count}\n",
                flush=True,
            )

    asyncio.run(_run_with_shutdown())


if __name__ == "__main__":
    main()
