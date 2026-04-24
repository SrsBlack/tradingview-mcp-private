"""
LiveExecutorAdapter — wraps LiveExecutor with PaperExecutor-compatible interface.

Thin adapter that gives LiveExecutor the same synchronous interface
as PaperExecutor so the Orchestrator can use either without branching.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from bridge.config import get_bridge_config, BridgeConfig
from bridge.decision_types import TradeDecision, PaperPosition
from bridge.live_executor import LiveExecutor

# Expected slippage per asset class (conservative estimates)
_EXPECTED_SLIPPAGE: dict[str, float] = {
    "BTCUSD": 0.0005,   # 0.05% for crypto
    "ETHUSD": 0.0005,
    "SOLUSD": 0.001,    # 0.1% for smaller crypto
    "DOGEUSD": 0.001,
    "EURUSD": 0.0001,   # 0.01% for major forex
    "GBPUSD": 0.0001,
    "USDJPY": 0.0001,
    "AUDUSD": 0.00015,
    "NZDUSD": 0.00015,
    "XAUUSD": 0.0002,   # 0.02% for gold
    "XAGUSD": 0.0003,
    "US500": 0.0001,
    "US100": 0.0001,
    "US30": 0.0001,
    "GER40": 0.00015,
}


class LiveExecutorAdapter:
    """
    Thin adapter that gives LiveExecutor the same synchronous interface
    as PaperExecutor so the Orchestrator can use either without branching.
    """

    def __init__(self, initial_balance: float = 100_000.0):
        self._live = LiveExecutor(max_positions=3)
        self._live.confirm_session()  # auto-confirm for fully autonomous mode
        self._config = get_bridge_config()  # for TV->MT5 symbol name translation
        self._mt5_connector = None
        self._connect_mt5()  # initialize + login to MT5 before any trades
        self._positions_lock = threading.Lock()
        self.open_positions: dict[int, Any] = {}  # mirrors PaperExecutor interface
        self.closed_positions: list = []
        self.wins = 0
        self.losses = 0
        self.consecutive_losses = 0
        self.grade_a_wins = 0
        self.grade_a_losses = 0
        # Account heat: reduce size after winning streak to prevent overconfidence
        self._heat_level: float = 1.0  # 1.0 = normal, 0.75 = warm, 0.5 = hot
        self._consecutive_wins: int = 0
        # Sync balance from MT5 at startup instead of using CLI default
        mt5_balance = self._get_mt5_balance()
        if mt5_balance is not None:
            self.balance = mt5_balance
            self.initial_balance = initial_balance
            self.peak_balance = max(initial_balance, mt5_balance)
            print(f"[LIVE] MT5 account balance: ${mt5_balance:,.2f}", flush=True)
        else:
            self.balance = initial_balance
            self.initial_balance = initial_balance
            self.peak_balance = initial_balance

        # Daily P&L tracking — resets at midnight UTC.
        # Uses current MT5 balance as day-start baseline so the kill switch
        # reflects TODAY's P&L, not cumulative loss from initial_balance.
        self._day_start_balance: float = self.balance
        self._day_start_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Per-symbol loss cooldown: symbol -> UTC ISO timestamp when cooldown expires
        self._symbol_loss_cooldowns: dict[str, str] = {}
        self._symbol_loss_cooldown_hours: int = 4

        # Daily trade limit — cap how many new trades per day
        self._daily_trade_count: int = 0
        self._daily_trade_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._max_daily_trades: int = 5  # hard cap: 5 trades per day

        # Dynamic daily trade limit — based on signal quality seen today
        self._grade_a_signals_today: int = 0  # How many Grade A signals seen today

        # Per-symbol loss count — drives dynamic cooldown duration
        self._symbol_loss_counts: dict[str, int] = {}  # symbol -> number of losses today

        # Global loss cooldown — after ANY loss, pause all trading for N minutes
        self._global_loss_cooldown_until: str | None = None
        self._global_loss_cooldown_minutes: int = 60  # 1 hour cooldown after a loss

        # Persisted per-ticket trailing SL state (populated by _load_safety_state;
        # consumed by position_manager.reconcile_mt5_on_startup).
        self._persisted_trail_state: dict[str, dict[str, Any]] = {}

        # Restore safety state from previous run
        self._load_safety_state()

    def _save_safety_state(self) -> None:
        """Persist safety state to disk so it survives bridge restarts."""
        # Track the date the kill switch fired so restore logic can scope it to today
        if self._live._kill_switch and not getattr(self, "_kill_switch_date", ""):
            self._kill_switch_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state = {
            "daily_trade_count": self._daily_trade_count,
            "daily_trade_date": self._daily_trade_date,
            "global_loss_cooldown_until": self._global_loss_cooldown_until,
            "symbol_loss_cooldowns": self._symbol_loss_cooldowns,
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self._consecutive_wins,
            "grade_a_signals_today": self._grade_a_signals_today,
            "symbol_loss_counts": self._symbol_loss_counts,
            "kill_switch_triggered": self._live._kill_switch,
            "kill_switch_date": getattr(self, "_kill_switch_date", ""),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            # Per-ticket position state — survives restart so adopted positions
            # retain trail progress AND the original TP/TP2 targets, grade, and
            # reasoning. Without TP/TP2 here the adoption code zeroes them and
            # silently disables TP management on the position (discovered
            # 2026-04-24 after a mid-session restart stripped TPs from 3
            # positions; salvaged by setting MT5 TP directly, then this fix).
            "trailing_sl_by_ticket": {
                str(t): {
                    "trailing_sl": float(p.trailing_sl),
                    "tp1_hit": bool(getattr(p, "tp1_hit", False)),
                    "trail_desync": bool(getattr(p, "_trail_desync", False)),
                    "desired_sl": float(getattr(p, "_desired_sl", p.trailing_sl)),
                    # Preserve the full planned exit ladder across restarts
                    "tp_price": float(getattr(p, "tp_price", 0.0) or 0.0),
                    "tp2_price": float(getattr(p, "tp2_price", 0.0) or 0.0),
                    "entry_price": float(getattr(p, "entry_price", 0.0) or 0.0),
                    "ict_grade": str(getattr(p, "ict_grade", "") or ""),
                    "ict_score": float(getattr(p, "ict_score", 0.0) or 0.0),
                    "trade_type": str(getattr(p, "trade_type", "intraday") or "intraday"),
                    "risk_pct": float(getattr(p, "risk_pct", 0.01) or 0.01),
                    "opened_at": str(getattr(p, "opened_at", "") or ""),
                    "reasoning": str(getattr(p, "reasoning", "") or "")[:500],  # cap to keep file small
                }
                for t, p in self.open_positions.items()
            },
        }
        path = Path(__file__).parent.parent / "bridge_safety_state.json"
        try:
            path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[SAFETY] Failed to save state: {e}", flush=True)

    def _load_safety_state(self) -> None:
        """Restore safety state from disk after a restart."""
        path = Path(__file__).parent.parent / "bridge_safety_state.json"
        try:
            if not path.exists():
                return
            state = json.loads(path.read_text(encoding="utf-8"))
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            # Only restore daily counters if same day
            if state.get("daily_trade_date") == today:
                self._daily_trade_count = state.get("daily_trade_count", 0)
                self._daily_trade_date = today
                print(
                    f"[SAFETY] Restored daily trade count: "
                    f"{self._daily_trade_count}/{self._max_daily_trades}",
                    flush=True,
                )

            # Restore global loss cooldown if not expired
            glc = state.get("global_loss_cooldown_until")
            if glc:
                expiry = datetime.fromisoformat(glc)
                if datetime.now(timezone.utc) < expiry:
                    self._global_loss_cooldown_until = glc
                    remaining = int((expiry - datetime.now(timezone.utc)).total_seconds() / 60)
                    print(
                        f"[SAFETY] Restored global loss cooldown: {remaining}min remaining",
                        flush=True,
                    )

            # Restore symbol cooldowns (prune expired)
            for sym, expiry_iso in state.get("symbol_loss_cooldowns", {}).items():
                try:
                    expiry = datetime.fromisoformat(expiry_iso)
                    if datetime.now(timezone.utc) < expiry:
                        self._symbol_loss_cooldowns[sym] = expiry_iso
                except (ValueError, TypeError):
                    pass
            if self._symbol_loss_cooldowns:
                print(
                    f"[SAFETY] Restored {len(self._symbol_loss_cooldowns)} symbol cooldown(s)",
                    flush=True,
                )

            # Restore consecutive losses (important for kill switch threshold)
            self.consecutive_losses = state.get("consecutive_losses", 0)
            if self.consecutive_losses > 0:
                print(
                    f"[SAFETY] Restored consecutive losses: {self.consecutive_losses}",
                    flush=True,
                )

            # Restore consecutive wins and grade-A signal count (same-day only)
            if state.get("daily_trade_date") == today:
                self._consecutive_wins = state.get("consecutive_wins", 0)
                self._grade_a_signals_today = state.get("grade_a_signals_today", 0)
                self._symbol_loss_counts = state.get("symbol_loss_counts", {})
                if self._consecutive_wins > 0:
                    print(
                        f"[SAFETY] Restored consecutive wins: {self._consecutive_wins} "
                        f"(heat multiplier: {self.get_heat_multiplier():.2f}x)",
                        flush=True,
                    )

            # Restore kill switch if same day
            if state.get("kill_switch_triggered") and state.get("kill_switch_date") == today:
                self._live._kill_switch = True
                print("[SAFETY] Restored KILL SWITCH (still active from earlier today)", flush=True)

            # Stash per-ticket trailing SL state for adopt_position() to consume
            # when MT5 positions are re-adopted after restart. Without this, the
            # bridge resets trailing_sl to the original entry SL and loses trail.
            self._persisted_trail_state = state.get("trailing_sl_by_ticket", {})
            if self._persisted_trail_state:
                print(
                    f"[SAFETY] Cached trailing-SL state for "
                    f"{len(self._persisted_trail_state)} position(s); "
                    f"will restore on MT5 adopt",
                    flush=True,
                )

        except Exception as e:
            print(f"[SAFETY] Failed to load state: {e}", flush=True)

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

    def _get_mt5_balance(self) -> float | None:
        """Read current account balance from MT5."""
        try:
            import MetaTrader5 as mt5
            info = mt5.account_info()
            if info and info.balance > 0:
                self._mt5_login = info.login
                return float(info.balance)
        except Exception:
            pass
        return None

    def get_bridge_floating_pnl(self) -> float:
        """Floating P&L on bridge-only positions (comment contains ICT_Bridge).

        Filters out EA positions that share the account so health/dashboard
        can show a bridge-specific figure separate from the full-account
        balance used for FTMO risk rules.
        """
        try:
            import MetaTrader5 as mt5
            positions = mt5.positions_get() or []
            return float(sum(
                p.profit for p in positions
                if p.comment and "ICT_Bridge" in p.comment
            ))
        except Exception:
            return 0.0

    def get_bridge_open_count(self) -> int:
        """Number of bridge-owned open positions on MT5."""
        try:
            import MetaTrader5 as mt5
            positions = mt5.positions_get() or []
            return sum(
                1 for p in positions
                if p.comment and "ICT_Bridge" in p.comment
            )
        except Exception:
            return 0

    def _check_daily_reset(self) -> None:
        """Reset day-start balance at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day_start_date:
            # Read fresh balance from MT5 for the new day
            mt5_balance = self._get_mt5_balance()
            if mt5_balance is not None:
                self._day_start_balance = mt5_balance
                self.balance = mt5_balance
            else:
                self._day_start_balance = self.balance
            self._day_start_date = today
            # Clear per-symbol loss cooldowns and counts on new day
            self._symbol_loss_cooldowns.clear()
            self._symbol_loss_counts.clear()
            self._grade_a_signals_today = 0
            print(f"[LIVE] Daily reset — day-start balance: ${self._day_start_balance:,.2f}", flush=True)
            self._save_safety_state()

    @property
    def daily_pnl(self) -> float:
        """Daily P&L *for FTMO risk rules* — uses full account balance delta.

        Compares current balance to the balance at the start of today (UTC),
        NOT to the initial $100k. This ensures the 2% daily loss kill switch
        reflects today's actual loss, not cumulative drawdown.

        This intentionally includes EA P&L because the 2%-daily / 10%-total
        FTMO limits apply to the whole account, not just bridge slice.
        """
        self._check_daily_reset()
        return round(self.balance - self._day_start_balance, 2)

    def is_symbol_on_loss_cooldown(self, symbol: str) -> bool:
        """Check if a symbol is in post-loss cooldown."""
        base = symbol.split(":")[-1]
        expiry_iso = self._symbol_loss_cooldowns.get(base)
        if not expiry_iso:
            return False
        now = datetime.now(timezone.utc)
        expiry = datetime.fromisoformat(expiry_iso)
        if now >= expiry:
            del self._symbol_loss_cooldowns[base]
            return False
        remaining = (expiry - now).total_seconds() / 60
        return True

    def set_symbol_loss_cooldown(self, symbol: str) -> None:
        """Set a graduated cooldown on a symbol after a loss.

        1st loss on symbol today: 2h cooldown (half-size allowed after expiry
        until the 4h mark — see get_symbol_cooldown_risk_multiplier).
        2nd+ loss on same symbol today: 4h hard block.
        """
        base = symbol.split(":")[-1]
        self._symbol_loss_counts[base] = self._symbol_loss_counts.get(base, 0) + 1
        count = self._symbol_loss_counts[base]

        if count >= 2:
            hours = 4  # Hard block after 2+ losses
        else:
            hours = 2  # Shorter cooldown after 1 loss

        expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
        self._symbol_loss_cooldowns[base] = expiry.isoformat()
        print(
            f"  [COOLDOWN] {base} loss #{count}: {hours}h cooldown "
            f"until {expiry.strftime('%H:%M')} UTC",
            flush=True,
        )
        self._save_safety_state()

    def is_daily_trade_limit_reached(self) -> bool:
        """Check if we've hit the dynamic daily trade cap."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_trade_date != today:
            self._daily_trade_count = 0
            self._daily_trade_date = today
        return self._daily_trade_count >= self.dynamic_max_trades

    def increment_daily_trade_count(self) -> None:
        """Call after each successful trade execution."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_trade_date != today:
            self._daily_trade_count = 0
            self._daily_trade_date = today
        self._daily_trade_count += 1
        limit = self.dynamic_max_trades
        remaining = limit - self._daily_trade_count
        print(
            f"  [DAILY CAP] Trade {self._daily_trade_count}/{limit} today "
            f"({remaining} remaining, grade_a_signals={self._grade_a_signals_today})",
            flush=True,
        )
        self._save_safety_state()

    def is_on_global_loss_cooldown(self) -> bool:
        """Check if global loss cooldown is active (pause ALL trading after a loss)."""
        if not self._global_loss_cooldown_until:
            return False
        now = datetime.now(timezone.utc)
        expiry = datetime.fromisoformat(self._global_loss_cooldown_until)
        if now >= expiry:
            self._global_loss_cooldown_until = None
            print("[COOLDOWN] Global loss cooldown expired. Trading resumed.", flush=True)
            return False
        remaining = int((expiry - now).total_seconds() / 60)
        return True

    def set_global_loss_cooldown(self) -> None:
        """Trigger global loss cooldown — no new trades for N minutes."""
        expiry = datetime.now(timezone.utc) + timedelta(minutes=self._global_loss_cooldown_minutes)
        self._global_loss_cooldown_until = expiry.isoformat()
        print(
            f"  [COOLDOWN] Global {self._global_loss_cooldown_minutes}min loss cooldown "
            f"until {expiry.strftime('%H:%M')} UTC — NO new trades",
            flush=True,
        )
        self._save_safety_state()

    # ------------------------------------------------------------------ #
    # Phase 4.1 — Account Heat System                                     #
    # ------------------------------------------------------------------ #

    def get_heat_multiplier(self) -> float:
        """Returns position size multiplier based on winning streak.

        After 3 consecutive wins: 0.75x (reduce by 25%)
        After 5 consecutive wins: 0.50x (reduce by 50%)
        After any loss: reset to 1.0x
        """
        if self._consecutive_wins >= 5:
            return 0.5
        elif self._consecutive_wins >= 3:
            return 0.75
        return 1.0

    # ------------------------------------------------------------------ #
    # Phase 3.4 — Dynamic Daily Trade Limit                               #
    # ------------------------------------------------------------------ #

    def record_grade_a_signal(self) -> None:
        """Call when a Grade A signal is detected (even if not traded)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_trade_date != today:
            self._grade_a_signals_today = 0
            self._daily_trade_date = today
        self._grade_a_signals_today += 1

    @property
    def dynamic_max_trades(self) -> int:
        """Dynamic daily trade limit based on signal quality."""
        if self._grade_a_signals_today >= 2:
            return 7  # High conviction day
        elif self._grade_a_signals_today >= 1:
            return 5  # Normal day
        return 3  # Low conviction day

    # ------------------------------------------------------------------ #
    # Phase 4.2 — Dynamic Loss Cooldown                                   #
    # ------------------------------------------------------------------ #

    def get_symbol_cooldown_risk_multiplier(self, symbol: str) -> float:
        """Returns size multiplier for a symbol in the reduced-size window.

        If the symbol has exactly 1 loss today and is past the 2h soft
        cooldown but within the 4h hard-block window: return 0.5 (half size).
        Otherwise: return 1.0.
        """
        base = symbol.split(":")[-1]
        if self._symbol_loss_counts.get(base, 0) != 1:
            return 1.0
        expiry_iso = self._symbol_loss_cooldowns.get(base)
        if not expiry_iso:
            return 1.0
        now = datetime.now(timezone.utc)
        soft_end = now - timedelta(hours=2)   # 2h ago
        hard_end = now + timedelta(hours=2)   # 4h from original set would be 2h from now
        # Approximate: if cooldown expires within next 2h it's in the 2–4h window
        expiry = datetime.fromisoformat(expiry_iso)
        if now < expiry <= hard_end:
            return 0.5
        return 1.0

    def find_opposite_positions(self, symbol: str, new_direction: str) -> list[tuple[int, Any]]:
        """Find open positions on the same symbol with the opposite direction."""
        result = []
        with self._positions_lock:
            for ticket, pos in self.open_positions.items():
                if pos.symbol == symbol and pos.direction != new_direction:
                    result.append((ticket, pos))
        return result

    def close_position_by_ticket(self, ticket: int, reason: str = "SIGNAL_FLIP") -> dict | None:
        """Close a specific position by ticket. Returns close event dict or None."""
        with self._positions_lock:
            pos = self.open_positions.get(ticket)
        if pos is None:
            return None

        pnl = pos.floating_pnl
        r_mult = pos.r_multiple
        closed_at = datetime.now(timezone.utc).isoformat()

        # Close on MT5
        self._mt5_close_position(ticket, pnl, reason)

        # Remove from state
        with self._positions_lock:
            self.open_positions.pop(ticket, None)

        # Update stats
        self.balance += pnl
        self.peak_balance = max(self.peak_balance, self.balance)
        if pnl >= 0:
            self.wins += 1
            self.consecutive_losses = 0
            self._consecutive_wins += 1
        else:
            self.losses += 1
            self.consecutive_losses += 1
            self._consecutive_wins = 0
        self._save_safety_state()

        return {
            "ticket": ticket, "symbol": pos.symbol,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "exit_price": pos.current_price,
            "actual_trigger_price": pos.current_price,
            "pnl": round(pnl, 2), "r_multiple": round(r_mult, 2),
            "reason": reason, "balance": round(self.balance, 2),
            "opened_at": pos.opened_at, "closed_at": closed_at,
            "sl_price": pos.sl_price, "tp_price": pos.tp_price,
            "tp2_price": pos.tp2_price, "lot_size": pos.lot_size,
            "ict_grade": pos.ict_grade, "ict_score": pos.ict_score,
            "trailing_sl": pos.trailing_sl, "tp1_hit": pos.tp1_hit,
        }

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
        with self._positions_lock:
            for pos in self.open_positions.values():
                if pos.symbol == decision.symbol and pos.direction == decision.action:
                    entry_diff = abs(pos.entry_price - decision.entry_price)
                    threshold = pos.entry_price * 0.005  # 0.5% tolerance
                    if entry_diff < threshold:
                        return {"success": False, "ticket": 0,
                                "message": f"Duplicate: already {pos.direction} {decision.symbol} "
                                           f"@ {pos.entry_price:.4f}"}

        # Use pre-calculated lot size from RiskBridge if provided
        if lot_size is None:
            from bridge.risk_bridge import RiskBridge
            _rb = RiskBridge()
            lot_size = _rb.get_lot_size_live(
                decision.symbol, self.balance, decision.risk_pct,
                decision.entry_price, decision.sl_price, decision.action,
            )
            if lot_size <= 0:
                lot_size = 0.01

        # Translate TV symbol (e.g. "CBOT:YM1!") to MT5 symbol (e.g. "US30")
        mt5_decision = decision
        mt5_symbol = self._config.internal_symbol(decision.symbol)
        if mt5_symbol != decision.symbol:
            mt5_decision = copy.copy(decision)
            mt5_decision.symbol = mt5_symbol

        # MT5-level dedup: check broker for existing positions on same symbol
        try:
            import MetaTrader5 as mt5
            from bridge.config import tv_to_ftmo_symbol
            ftmo_sym = tv_to_ftmo_symbol(mt5_symbol)
            existing = mt5.positions_get(symbol=ftmo_sym)
            if existing:
                bridge_positions = [p for p in existing if p.comment and "ICT_Bridge" in p.comment]
                for bp in bridge_positions:
                    mt5_dir = "BUY" if bp.type == 0 else "SELL"
                    if mt5_dir == decision.action:
                        return {"success": False, "ticket": 0,
                                "message": f"MT5 dedup: already have {mt5_dir} on {ftmo_sym} (#{bp.ticket})"}
        except ImportError:
            pass
        except Exception as e:
            print(f"[LIVE] MT5 dedup check warning: {e}", flush=True)

        # Run async submit_trade in a dedicated thread with its own event loop
        result_holder: list[Any] = []
        error_holder: list[Exception] = []

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
            # Thread timed out — but order may have filled on MT5. Check broker.
            try:
                import MetaTrader5 as mt5
                from bridge.config import tv_to_ftmo_symbol
                ftmo_sym = tv_to_ftmo_symbol(self._config.internal_symbol(decision.symbol))
                positions = mt5.positions_get(symbol=ftmo_sym)
                if positions:
                    # Look for a very recent ICT_Bridge position (opened in last 30s)
                    cutoff = datetime.now(timezone.utc) - timedelta(seconds=30)
                    for p in positions:
                        if "ICT_Bridge" in (p.comment or ""):
                            opened = datetime.fromtimestamp(p.time, tz=timezone.utc)
                            if opened >= cutoff:
                                print(
                                    f"  [TIMEOUT_RECOVERY] Thread timed out but found filled position "
                                    f"#{p.ticket} on MT5 — adopting it",
                                    flush=True,
                                )
                                result_holder.append({
                                    "success": True,
                                    "ticket": p.ticket,
                                    "message": f"Timeout-recovered: {decision.action} {decision.symbol}",
                                    "fill_price": p.price_open,
                                })
                                break
            except Exception as e:
                print(f"  [TIMEOUT_RECOVERY] Check failed: {e}", flush=True)
            if not result_holder:
                return {"success": False, "ticket": 0, "message": "MT5 submit timed out"}
        result = result_holder[0]

        if result["success"]:
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
            with self._positions_lock:
                self.open_positions[result["ticket"]] = pos

            # Log expected slippage for performance tracking
            base_sym = self._config.internal_symbol(decision.symbol)
            slip_pct = _EXPECTED_SLIPPAGE.get(base_sym, 0.0001)
            expected_slip = decision.entry_price * slip_pct
            actual_slip = abs((result["fill_price"] or decision.entry_price) - decision.entry_price)
            if actual_slip > 0:
                print(f"  [SLIPPAGE] {base_sym}: expected {expected_slip:.5f}, actual {actual_slip:.5f}", flush=True)

        return result

    def check_positions(self, current_prices: dict[str, float]) -> list[dict]:
        """
        Check MT5 positions against current prices.
        Handles TP1 partial close, trailing SL sync to MT5, and SL/TP detection.

        Lock is held only for in-memory state updates; MT5 I/O runs outside
        the lock to avoid blocking other threads during network calls.
        """
        from bridge.risk_bridge import calculate_pnl

        events = []
        to_remove = []
        # Deferred MT5 operations: run outside lock
        mt5_ops: list[tuple[str, int, Any, Any]] = []  # (op, ticket, arg1, arg2)

        with self._positions_lock:
            for ticket, pos in self.open_positions.items():
                price = current_prices.get(pos.symbol)
                if price is None:
                    continue

                pos.current_price = price
                pos.floating_pnl = calculate_pnl(
                    pos.symbol.split(":")[-1], pos.entry_price, price, pos.lot_size, pos.direction
                )

                # -- TP1 partial close (50% at TP1, move SL to breakeven) --
                if not pos.tp1_hit and pos.tp2_price > 0:
                    tp1_hit = False
                    if pos.direction == "BUY" and price >= pos.tp_price:
                        tp1_hit = True
                    elif pos.direction == "SELL" and price <= pos.tp_price:
                        tp1_hit = True

                    if tp1_hit:
                        pos.tp1_hit = True
                        partial_pnl = calculate_pnl(
                            pos.symbol.split(":")[-1], pos.entry_price, pos.tp_price,
                            pos.lot_size * 0.5, pos.direction
                        )

                        self.balance += partial_pnl
                        pos.lot_size = round(pos.lot_size * 0.5, 4)
                        pos.trailing_sl = pos.entry_price  # breakeven

                        # Defer MT5 I/O: partial close + SL modify
                        mt5_ops.append(("partial_close", ticket, pos, pos.lot_size))
                        mt5_ops.append(("modify_sl", ticket, pos.entry_price, None))

                        print(
                            f"  [LIVE TP1] {pos.symbol} TP1 hit @ {pos.tp_price:.2f} "
                            f"partial PnL={partial_pnl:+.2f} — SL moved to breakeven",
                            flush=True,
                        )

                # -- Position age timeout --
                if pos.opened_at and not pos.tp1_hit:
                    try:
                        opened_dt = datetime.fromisoformat(pos.opened_at)
                        age_hours = (datetime.now(timezone.utc) - opened_dt).total_seconds() / 3600
                        # scalp=8h, intraday=48h, swing=168h (1 week)
                        _age_limits = {"scalp": (4, 8), "intraday": (24, 48), "swing": (96, 168)}
                        warn_hours, max_hours = _age_limits.get(pos.trade_type, (24, 48))
                        if age_hours > max_hours and pos.r_multiple < 1.0:
                            print(
                                f"  [AGE CLOSE] #{ticket} {pos.symbol} open {age_hours:.0f}h "
                                f"({pos.trade_type}) r={pos.r_multiple:.2f} -> STALE_TIMEOUT",
                                flush=True,
                            )
                            pnl = pos.floating_pnl
                            risk_pnl = abs(calculate_pnl(
                                pos.symbol, pos.entry_price, pos.sl_price, pos.lot_size, pos.direction
                            ))
                            r_mult = pnl / risk_pnl if risk_pnl > 0 else 0.0
                            closed_at = datetime.now(timezone.utc).isoformat()
                            mt5_ops.append(("close_position", ticket, pnl, "STALE_TIMEOUT"))
                            to_remove.append(ticket)
                            self.balance += pnl
                            self.peak_balance = max(self.peak_balance, self.balance)
                            if pnl >= 0:
                                self.wins += 1
                                self.consecutive_losses = 0
                                self._consecutive_wins += 1
                                if pos.ict_grade == "A":
                                    self.grade_a_wins += 1
                                self._save_safety_state()
                            else:
                                self.losses += 1
                                self.consecutive_losses += 1
                                self._consecutive_wins = 0
                                if pos.ict_grade == "A":
                                    self.grade_a_losses += 1
                                self.set_symbol_loss_cooldown(pos.symbol)
                                self.set_global_loss_cooldown()
                            events.append({
                                "ticket": ticket, "symbol": pos.symbol,
                                "direction": pos.direction,
                                "entry_price": pos.entry_price,
                                "exit_price": price,
                                "actual_trigger_price": price,
                                "pnl": round(pnl, 2), "r_multiple": round(r_mult, 2),
                                "reason": "STALE_TIMEOUT", "balance": round(self.balance, 2),
                                "opened_at": pos.opened_at, "closed_at": closed_at,
                                "sl_price": pos.sl_price, "tp_price": pos.tp_price,
                                "tp2_price": pos.tp2_price, "lot_size": pos.lot_size,
                                "ict_grade": pos.ict_grade, "ict_score": pos.ict_score,
                                "trailing_sl": pos.trailing_sl, "tp1_hit": pos.tp1_hit,
                            })
                            continue
                        elif age_hours > warn_hours and not getattr(pos, '_age_warned', False):
                            print(
                                f"  [AGE] #{ticket} {pos.symbol} open {age_hours:.0f}h "
                                f"({pos.trade_type}) -- approaching timeout",
                                flush=True,
                            )
                            pos._age_warned = True
                    except (ValueError, TypeError):
                        pass

                # -- Check TP2/final TP hit --
                # CRITICAL: tp_final=0 means no TP is set (e.g. adopted positions
                # from MT5 restart). Skip TP check entirely when tp_final <= 0
                # to avoid closing positions immediately.
                tp_final = pos.tp2_price if (pos.tp2_price > 0 and pos.tp1_hit) else pos.tp_price
                closed_reason = None
                if pos.direction == "BUY":
                    if price <= pos.trailing_sl and pos.trailing_sl != pos.sl_price:
                        closed_reason = "TRAILING_SL"
                    elif price <= pos.sl_price:
                        closed_reason = "SL"
                    elif tp_final > 0 and price >= tp_final:
                        closed_reason = "TP2" if pos.tp1_hit else "TP"
                else:
                    if price >= pos.trailing_sl and pos.trailing_sl != pos.sl_price:
                        closed_reason = "TRAILING_SL"
                    elif price >= pos.sl_price:
                        closed_reason = "SL"
                    elif tp_final > 0 and price <= tp_final:
                        closed_reason = "TP2" if pos.tp1_hit else "TP"

                if closed_reason:
                    pnl = pos.floating_pnl
                    risk_pnl = abs(calculate_pnl(
                        pos.symbol, pos.entry_price, pos.sl_price, pos.lot_size, pos.direction
                    ))
                    r_mult = pnl / risk_pnl if risk_pnl > 0 else 0.0
                    closed_at = datetime.now(timezone.utc).isoformat()
                    exit_level = pos.sl_price if closed_reason == "SL" else (
                        pos.trailing_sl if closed_reason == "TRAILING_SL" else tp_final)
                    # Defer actual MT5 close (runs outside lock) — ensures broker
                    # position matches bridge state. Broker-side SL may not fire
                    # on bridge-cached prices, so we must send an explicit close.
                    mt5_ops.append(("close_position", ticket, pnl, closed_reason))
                    to_remove.append(ticket)
                    self.balance += pnl
                    self.peak_balance = max(self.peak_balance, self.balance)
                    if pnl >= 0:
                        self.wins += 1
                        self.consecutive_losses = 0
                        self._consecutive_wins += 1
                        if pos.ict_grade == "A":
                            self.grade_a_wins += 1
                        self._save_safety_state()
                    else:
                        self.losses += 1
                        self.consecutive_losses += 1
                        self._consecutive_wins = 0
                        if pos.ict_grade == "A":
                            self.grade_a_losses += 1
                        # Set per-symbol loss cooldown
                        self.set_symbol_loss_cooldown(pos.symbol)
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
                        "trailing_sl": pos.trailing_sl, "tp1_hit": pos.tp1_hit,
                    })
                    continue

                # -- Update trailing stop (in-memory only; MT5 sync deferred) --
                old_trailing = pos.trailing_sl
                self._update_trailing_stop(pos)
                if pos.trailing_sl != old_trailing:
                    mt5_ops.append(("trail_sl", ticket, pos.trailing_sl, old_trailing))
                    print(
                        f"  [LIVE TRAIL] {pos.symbol} #{ticket} trailing SL "
                        f"{old_trailing:.2f} -> {pos.trailing_sl:.2f} (syncing to MT5)",
                        flush=True,
                    )
                elif getattr(pos, "_trail_desync", False):
                    # Prior cycle failed to sync trailing SL to MT5. Re-push the
                    # desired value this cycle so broker state catches up.
                    desired = getattr(pos, "_desired_sl", pos.trailing_sl)
                    mt5_ops.append(("trail_sl", ticket, desired, pos.trailing_sl))
                    print(
                        f"  [LIVE TRAIL] {pos.symbol} #{ticket} retry desynced SL "
                        f"(target {desired:.5f}, broker may still hold old value)",
                        flush=True,
                    )

            for t in to_remove:
                self.open_positions.pop(t, None)

        # -- Execute deferred MT5 I/O outside the lock --
        for op, ticket, arg1, arg2 in mt5_ops:
            if op == "partial_close":
                self._mt5_partial_close(ticket, arg1, arg1.lot_size)
            elif op == "modify_sl":
                self._mt5_modify_sl(ticket, arg1)
            elif op == "trail_sl":
                self._mt5_modify_sl_with_revert(ticket, arg1, arg2)
            elif op == "close_position":
                # arg1 = pnl, arg2 = closed_reason (str, for logging)
                self._mt5_close_position(ticket, arg1, arg2)

        # -- HTF invalidation: close non-scalp positions that now oppose H4 bias --
        # Re-checks H4 structure every cycle. Winners (R>=1.0 or tp1_hit) are exempt.
        htf_close_events = self._check_htf_invalidation()
        events.extend(htf_close_events)

        # -- Market-close exit: close non-swing index/forex positions before close --
        # European indices (GER40) gap 200-500 pts on open. US indices gap 50-200 pts.
        # Only swing trades (explicit HTF alignment + strong confluence) should hold overnight.
        mkt_close_events = self._check_market_close_exit()
        events.extend(mkt_close_events)

        # -- Periodic open_tickets / open_positions sync --
        # Ensures any drift between the two dicts is corrected every check cycle
        if hasattr(self, '_live') and hasattr(self._live, 'open_tickets'):
            live_tickets = self._live.open_tickets
            with self._positions_lock:
                for t in list(self.open_positions.keys()):
                    if t not in live_tickets:
                        pos = self.open_positions[t]
                        from bridge.config import tv_to_ftmo_symbol
                        ftmo_sym = tv_to_ftmo_symbol(pos.symbol.split(":")[-1])
                        live_tickets[t] = {
                            "symbol": ftmo_sym,
                            "tv_symbol": pos.symbol,
                            "direction": pos.direction,
                            "entry_price": pos.entry_price,
                            "sl_price": pos.sl_price,
                            "tp_price": pos.tp_price,
                            "tp2_price": pos.tp2_price,
                            "tp1_hit": pos.tp1_hit,
                            "lot_size": pos.lot_size,
                            "opened_at": pos.opened_at,
                        }

        return events

    def _update_trailing_stop(self, pos: PaperPosition) -> None:
        """Trail SL using ICT swing structure — behind successive higher-lows (BUY)
        or lower-highs (SELL). Falls back to R-multiple if no swing data available.

        ICT method: Trail behind swing structure, NOT tight R-multiple.
        Give the trade room to breathe through normal retracements.

        Trailing stages:
        - 1.0R: Move SL to breakeven (entry price)
        - 2.0R: Move SL to 1.0R (lock in 1R profit)
        - 3.0R: Move SL to 2.0R (lock in 2R profit)
        - Each stage only trails 1R behind current R-multiple

        Swing trail (when available) takes priority but must also be
        at least breakeven after 1R.
        """
        r = pos.r_multiple
        risk = abs(pos.entry_price - pos.sl_price)
        if risk == 0 or r < 1.0:
            return

        # Minimum: breakeven after 1R
        min_trail = pos.entry_price

        # Try swing-based trailing first (ICT preferred method)
        swing_trail = self._get_swing_trail_level(pos)

        if swing_trail is not None:
            new_sl = swing_trail
        else:
            # Fallback: conservative R-multiple trailing
            # Trail 1R behind current R — gives room to breathe
            # At 1.0R -> breakeven, at 2.0R -> 1R locked, at 3.0R -> 2R locked
            trail_r = max(0, r - 1.0)  # Always 1R behind
            if pos.direction == "BUY":
                new_sl = pos.entry_price + trail_r * risk
            else:
                new_sl = pos.entry_price - trail_r * risk

        # Ensure minimum breakeven after 1R
        if pos.direction == "BUY":
            new_sl = max(new_sl, min_trail)
            if new_sl > pos.trailing_sl:
                pos.trailing_sl = round(new_sl, 5)
        else:
            new_sl = min(new_sl, min_trail)
            if new_sl < pos.trailing_sl:
                pos.trailing_sl = round(new_sl, 5)

    def _get_swing_trail_level(self, pos: PaperPosition) -> float | None:
        """Get the trailing stop level based on M15 swing structure.

        For BUY: find the most recent swing low that is above entry price.
        For SELL: find the most recent swing high that is below entry price.

        Uses MT5 M15 bars to detect swings in real-time.
        """
        try:
            import MetaTrader5 as mt5
            from bridge.config import tv_to_ftmo_symbol

            ftmo_sym = tv_to_ftmo_symbol(pos.symbol.split(":")[-1])
            rates = mt5.copy_rates_from_pos(ftmo_sym, mt5.TIMEFRAME_M15, 0, 30)
            if rates is None or len(rates) < 10:
                return None

            # Simple swing detection: a swing low is a bar where low < low of both neighbors
            swing_lows = []
            swing_highs = []
            for i in range(2, len(rates) - 2):
                low = float(rates[i]['low'])
                high = float(rates[i]['high'])
                if low < float(rates[i-1]['low']) and low < float(rates[i-2]['low']) and \
                   low < float(rates[i+1]['low']) and low < float(rates[i+2]['low']):
                    swing_lows.append(low)
                if high > float(rates[i-1]['high']) and high > float(rates[i-2]['high']) and \
                   high > float(rates[i+1]['high']) and high > float(rates[i+2]['high']):
                    swing_highs.append(high)

            atr_buffer = abs(pos.entry_price - pos.sl_price) * 0.1  # 10% of original risk as buffer

            if pos.direction == "BUY" and swing_lows:
                # For buys: trail behind the most recent swing low above entry
                valid_swings = [s for s in swing_lows if s > pos.entry_price]
                if valid_swings:
                    return valid_swings[-1] - atr_buffer  # Below the swing low
            elif pos.direction == "SELL" and swing_highs:
                # For sells: trail above the most recent swing high below entry
                valid_swings = [s for s in swing_highs if s < pos.entry_price]
                if valid_swings:
                    return valid_swings[-1] + atr_buffer  # Above the swing high

            return None
        except Exception:
            return None

    def _get_h4_bias(self, ftmo_symbol: str) -> str:
        """Query MT5 for H4 candles and determine bias. Returns BULLISH/BEARISH/NEUTRAL."""
        try:
            import MetaTrader5 as mt5
            rates = mt5.copy_rates_from_pos(ftmo_symbol, mt5.TIMEFRAME_H4, 0, 20)
            if rates is None or len(rates) < 10:
                return "NEUTRAL"

            highs = [float(r[2]) for r in rates[-10:]]
            lows = [float(r[3]) for r in rates[-10:]]

            recent_high = max(highs[-5:])
            prev_high = max(highs[:5])
            recent_low = min(lows[-5:])
            prev_low = min(lows[:5])

            if recent_high > prev_high and recent_low > prev_low:
                return "BULLISH"
            elif recent_high < prev_high and recent_low < prev_low:
                return "BEARISH"
            return "NEUTRAL"
        except Exception:
            return "NEUTRAL"

    def _check_htf_invalidation(self) -> list[dict]:
        """Close non-scalp positions that now oppose H4 bias.

        Winners (R>=1.0 or tp1_hit) and scalps are exempt.
        Only runs the MT5 H4 query once per symbol per cycle.
        """
        from bridge.config import tv_to_ftmo_symbol
        from bridge.risk_bridge import calculate_pnl

        events = []
        to_close: list[tuple[int, "PaperPosition", str]] = []
        htf_cache: dict[str, str] = {}

        if not self.open_positions:
            return events

        with self._positions_lock:
            for ticket, pos in self.open_positions.items():
                # Skip recently adopted positions — give them time to stabilize
                # after restart before running HTF checks
                if pos.opened_at:
                    try:
                        opened_dt = datetime.fromisoformat(pos.opened_at)
                        age_hours = (datetime.now(timezone.utc) - opened_dt).total_seconds() / 3600
                        # Give positions at least 2 hours before HTF invalidation.
                        # H4 candles take 4 hours to form — a crude high/low bias
                        # check flip-flops during consolidation and kills valid trades
                        # within minutes of opening. (SOL -$35 bug, 2026-04-23)
                        if age_hours < 2.0:
                            continue
                    except (ValueError, TypeError):
                        pass
                # Scalps are exempt — they don't need HTF alignment
                if getattr(pos, 'trade_type', 'intraday') == "scalp":
                    continue
                # Winners are exempt
                if pos.r_multiple >= 1.0 or pos.tp1_hit:
                    continue

                ftmo_sym = tv_to_ftmo_symbol(pos.symbol.split(":")[-1])
                if ftmo_sym not in htf_cache:
                    htf_cache[ftmo_sym] = self._get_h4_bias(ftmo_sym)
                h4_bias = htf_cache[ftmo_sym]

                if h4_bias == "NEUTRAL" or h4_bias == "MIXED":
                    continue

                opposes = (
                    (pos.direction == "BUY" and h4_bias == "BEARISH") or
                    (pos.direction == "SELL" and h4_bias == "BULLISH")
                )
                if opposes:
                    to_close.append((ticket, pos, h4_bias))

        # Execute closes outside lock
        for ticket, pos, h4_bias in to_close:
            print(
                f"  [HTF INVALIDATION] #{ticket} {pos.symbol} {pos.direction} opposes H4 {h4_bias} "
                f"(r={pos.r_multiple:.2f}, trade_type={pos.trade_type}) — closing",
                flush=True,
            )
            pnl = pos.floating_pnl
            risk_pnl = abs(calculate_pnl(
                pos.symbol.split(":")[-1], pos.entry_price, pos.sl_price, pos.lot_size, pos.direction
            ))
            r_mult = pnl / risk_pnl if risk_pnl > 0 else 0.0
            closed_at = datetime.now(timezone.utc).isoformat()

            self._mt5_close_position(ticket, pnl, "HTF_INVALIDATION")

            with self._positions_lock:
                self.open_positions.pop(ticket, None)
                self.balance += pnl
                self.peak_balance = max(self.peak_balance, self.balance)
                if pnl >= 0:
                    self.wins += 1
                    self.consecutive_losses = 0
                    self._consecutive_wins += 1
                    self._save_safety_state()
                else:
                    self.losses += 1
                    self.consecutive_losses += 1
                    self._consecutive_wins = 0
                    self.set_symbol_loss_cooldown(pos.symbol)

            events.append({
                "ticket": ticket, "symbol": pos.symbol,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "exit_price": pos.current_price,
                "actual_trigger_price": pos.current_price,
                "pnl": round(pnl, 2), "r_multiple": round(r_mult, 2),
                "reason": "HTF_INVALIDATION", "balance": round(self.balance, 2),
                "opened_at": pos.opened_at, "closed_at": closed_at,
                "sl_price": pos.sl_price, "tp_price": pos.tp_price,
                "tp2_price": pos.tp2_price, "lot_size": pos.lot_size,
                "ict_grade": pos.ict_grade, "ict_score": pos.ict_score,
                "trailing_sl": pos.trailing_sl, "tp1_hit": pos.tp1_hit,
            })

        return events

    def _check_market_close_exit(self) -> list[dict]:
        """Close non-swing intraday/scalp positions before market close.

        European indices (GER40/DAX) close at ~16:30 ET and gap 200-500 pts.
        US indices close at ~17:00 ET and gap 50-200 pts.
        Forex closes Friday ~17:00 ET (weekend gap).
        Crypto trades 24/7 — exempt.

        Only *swing* trades with explicit HTF alignment hold through close.
        Intraday/scalp trades are closed 30 min before market close.
        """
        from bridge.risk_bridge import calculate_pnl
        from zoneinfo import ZoneInfo

        events = []
        now_et = datetime.now(ZoneInfo("America/New_York"))
        current_min = now_et.hour * 60 + now_et.minute
        weekday = now_et.weekday()  # 0=Mon, 4=Fri

        # Market close windows (ET minutes): close positions 30 min before
        _CLOSE_WINDOWS = {
            # European indices: close at 16:30 ET → exit by 16:00
            "GER40": (16 * 60, 16 * 60 + 30),
            "DAX": (16 * 60, 16 * 60 + 30),
            # US indices: close at 17:00 ET → exit by 16:30
            "US30": (16 * 60 + 30, 17 * 60),
            "US500": (16 * 60 + 30, 17 * 60),
            "US100": (16 * 60 + 30, 17 * 60),
        }

        # Crypto is exempt
        _CRYPTO = {"BTCUSD", "ETHUSD", "SOLUSD", "DOGEUSD"}

        to_close: list[tuple[int, "PaperPosition", str]] = []

        with self._positions_lock:
            for ticket, pos in self.open_positions.items():
                base_sym = pos.symbol.split(":")[-1]

                # Skip crypto — 24/7 market
                if base_sym in _CRYPTO:
                    continue

                # Skip swing trades — they're designed to hold overnight
                if getattr(pos, 'trade_type', 'intraday') == "swing":
                    continue

                # Skip if already TP1 hit and profitable (let it ride)
                if pos.tp1_hit and pos.r_multiple >= 1.0:
                    continue

                # Check if this symbol has a close window
                from bridge.config import tv_to_ftmo_symbol
                ftmo_sym = tv_to_ftmo_symbol(base_sym)
                # Strip .cash suffix for lookup
                lookup_sym = ftmo_sym.replace(".cash", "")

                close_window = _CLOSE_WINDOWS.get(lookup_sym)

                # Friday: ALL non-crypto, non-swing positions close before 16:30 ET
                if weekday == 4 and not close_window:
                    close_window = (16 * 60, 16 * 60 + 30)

                if close_window:
                    window_start, window_end = close_window
                    if window_start <= current_min <= window_end:
                        to_close.append((ticket, pos, f"MARKET_CLOSE_{lookup_sym}"))

        # Execute closes outside lock
        for ticket, pos, reason in to_close:
            pnl = pos.floating_pnl
            risk_pnl = abs(calculate_pnl(
                pos.symbol.split(":")[-1], pos.entry_price, pos.sl_price,
                pos.lot_size, pos.direction
            ))
            r_mult = pnl / risk_pnl if risk_pnl > 0 else 0.0
            closed_at = datetime.now(timezone.utc).isoformat()

            print(
                f"  [MARKET CLOSE] #{ticket} {pos.symbol} {pos.direction} "
                f"({pos.trade_type}) r={r_mult:.2f} PnL={pnl:+.2f} — closing before gap",
                flush=True,
            )

            self._mt5_close_position(ticket, pnl, reason)

            with self._positions_lock:
                self.open_positions.pop(ticket, None)
                self.balance += pnl
                self.peak_balance = max(self.peak_balance, self.balance)
                if pnl >= 0:
                    self.wins += 1
                    self.consecutive_losses = 0
                    self._consecutive_wins += 1
                else:
                    self.losses += 1
                    self.consecutive_losses += 1
                    self._consecutive_wins = 0
                    self.set_symbol_loss_cooldown(pos.symbol)

            events.append({
                "ticket": ticket, "symbol": pos.symbol,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "exit_price": pos.current_price,
                "actual_trigger_price": pos.current_price,
                "pnl": round(pnl, 2), "r_multiple": round(r_mult, 2),
                "reason": reason, "balance": round(self.balance, 2),
                "opened_at": pos.opened_at, "closed_at": closed_at,
                "sl_price": pos.sl_price, "tp_price": pos.tp_price,
                "tp2_price": pos.tp2_price, "lot_size": pos.lot_size,
                "ict_grade": pos.ict_grade, "ict_score": pos.ict_score,
                "trailing_sl": pos.trailing_sl, "tp1_hit": pos.tp1_hit,
            })

        return events

    def _mt5_modify_sl(self, ticket: int, new_sl: float, attempts: int = 3) -> bool:
        """Update SL on MT5 position with retry + backoff. Returns True on success.

        Retries up to `attempts` times with 0.5s / 1.0s backoff. Each individual
        attempt has an 8s timeout. Between attempts the broker often recovers
        from transient retcodes (10004 REQUOTE, 10016 INVALID_STOPS races).

        If all attempts fail, the caller is expected to set pos._trail_desync=True
        via _mt5_modify_sl_with_revert so the next cycle re-attempts.
        """
        last_err: str = ""
        for attempt in range(1, attempts + 1):
            success = False
            def _run():
                nonlocal success, last_err
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    result = loop.run_until_complete(self._live.modify_sl(ticket, new_sl))
                    if result:
                        success = True
                    else:
                        last_err = "modify_sl returned False (see [MT5_SL] line above)"
                except Exception as e:
                    last_err = f"exception: {e}"
                finally:
                    loop.close()

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=8)

            if success:
                return True

            if t.is_alive():
                last_err = "thread timed out after 8s"

            if attempt < attempts:
                backoff = 0.5 * attempt  # 0.5s, 1.0s
                print(
                    f"  [MT5_SL] #{ticket} attempt {attempt}/{attempts} failed ({last_err}); "
                    f"retrying in {backoff}s",
                    flush=True,
                )
                time.sleep(backoff)

        print(
            f"  [MT5_SL] #{ticket} ALL {attempts} attempts failed; "
            f"position flagged for trail_desync recovery next cycle",
            flush=True,
        )
        return False

    def _mt5_modify_sl_with_revert(self, ticket: int, new_sl: float, old_sl: float) -> None:
        """Update SL on MT5. On failure, flag position as desynced so next cycle
        re-attempts, and also revert the in-memory trailing_sl to keep bridge
        state consistent with broker reality.
        """
        if self._mt5_modify_sl(ticket, new_sl):
            # Clear any previous desync flag on success
            with self._positions_lock:
                pos = self.open_positions.get(ticket)
                if pos is not None and getattr(pos, "_trail_desync", False):
                    pos._trail_desync = False
            return

        with self._positions_lock:
            pos = self.open_positions.get(ticket)
            if pos:
                # Mark for re-attempt next cycle — the bridge will see this flag
                # and re-push the trailing SL even if the stored value hasn't advanced.
                pos._trail_desync = True
                pos._desired_sl = new_sl
                pos.trailing_sl = old_sl
                if hasattr(self, '_live') and hasattr(self._live, 'open_tickets'):
                    info = self._live.open_tickets.get(ticket)
                    if info:
                        info["sl_price"] = old_sl
                print(
                    f"  [MT5_SL] Reverted trailing SL for #{ticket} to {old_sl:.5f}; "
                    f"desired {new_sl:.5f} queued for retry (trail_desync=True)",
                    flush=True,
                )

    def _mt5_partial_close(self, ticket: int, pos: PaperPosition, close_lots: float) -> None:
        """Partial close on MT5 — close specified lot size."""
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                half_lots = round(close_lots, 2)
                result = loop.run_until_complete(
                    self._live.partial_close_tp1(ticket, pos.tp_price)
                )
                if not result:
                    print(f"  [MT5_TP1] Failed partial close for #{ticket}", flush=True)
            except Exception as e:
                print(f"  [MT5_TP1] Error partial close for #{ticket}: {e}", flush=True)
            finally:
                loop.close()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=15)

    def _mt5_close_position(self, ticket: int, pnl: float, reason: str) -> None:
        """Full close on MT5 when bridge logic decides a position should exit.

        Critical for TRAILING_SL and TP closes: the bridge's cached price may
        cross the trigger level even if MT5's server-side tick does not, so we
        must send an explicit close order to keep broker state in sync with
        bridge state. If MT5 already closed it (e.g. server-side SL fired),
        close_position returns False and we log it — position is still out of
        our internal open_positions, matching reality.
        """
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(self._live.close_position(ticket, pnl))
                if result:
                    print(f"  [MT5_CLOSE] {reason} close OK for #{ticket}", flush=True)
                else:
                    # Already closed broker-side, or order rejected — either way,
                    # bridge state already removed it. Log for diagnosis only.
                    print(
                        f"  [MT5_CLOSE] {reason} close returned False for #{ticket} "
                        f"(likely already closed broker-side)",
                        flush=True,
                    )
            except Exception as e:
                print(f"  [MT5_CLOSE] Error closing #{ticket} ({reason}): {e}", flush=True)
            finally:
                loop.close()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=15)

    def get_account_summary(self) -> dict:
        daily_pnl_pct = self.daily_pnl / self._day_start_balance if self._day_start_balance else 0
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
