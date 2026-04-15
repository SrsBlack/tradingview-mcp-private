"""
Health monitoring — periodic status logging, TV connectivity, end-of-day summaries.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bridge.trading_hours import now_utc, ny_hour
from bridge.tv_client import TVClient
from bridge.session_store import SessionStore
from bridge.state_store import StateStore
from bridge.alerts import BridgeAlerts


class HealthMonitor:
    """Periodic health checks, TV connectivity, and end-of-day summaries."""

    def __init__(
        self,
        executor: Any,
        paper_shadow: Any,
        tv_client: TVClient,
        session: SessionStore,
        state_store: StateStore,
        alerts: BridgeAlerts,
        mode: str,
        paper_state_store: StateStore | None = None,
    ):
        self.executor = executor
        self.paper_shadow = paper_shadow
        self.tv_client = tv_client
        self.session = session
        self.state_store = state_store
        self.paper_state_store = paper_state_store
        self.alerts = alerts
        self.mode = mode

        self.tv_consecutive_failures: int = 0
        self.tv_healthy: bool = True
        self.last_eod_date: str = ""

    def run_check(self, cycle_count: int = 0, symbols: list[str] | None = None) -> None:
        """Run a single health check cycle: log status, save state, check TV."""
        summary = self.executor.get_account_summary()
        now = now_utc()

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
        if self.paper_state_store and self.paper_shadow is not self.executor:
            self.paper_state_store.save(self.paper_shadow, "paper_shadow")

        # TradingView connectivity check
        self._check_tv_health()

        # Daily summary at 5 PM ET
        self._check_end_of_day(now, cycle_count, symbols)

    def _check_tv_health(self) -> None:
        """Check TradingView CDP connectivity and alert on failures."""
        try:
            tv_ok = self.tv_client.health_check()
            if tv_ok:
                if not self.tv_healthy:
                    print("[HEALTH] TradingView reconnected!", flush=True)
                    try:
                        asyncio.ensure_future(self.alerts.send_raw(
                            "TradingView RECONNECTED — resuming analysis"
                        ))
                    except Exception:
                        pass
                self.tv_consecutive_failures = 0
                self.tv_healthy = True
            else:
                self.tv_consecutive_failures += 1
                if self.tv_consecutive_failures >= 3 and self.tv_healthy:
                    self.tv_healthy = False
                    print(
                        f"[HEALTH] WARNING: TradingView unresponsive "
                        f"({self.tv_consecutive_failures} consecutive failures) "
                        f"— pausing new analysis until reconnected",
                        flush=True,
                    )
                    try:
                        asyncio.ensure_future(self.alerts.send_raw(
                            f"WARNING: TradingView DISCONNECTED "
                            f"({self.tv_consecutive_failures} failures). "
                            f"Open positions still monitored via MT5/Alpaca. "
                            f"New analysis paused."
                        ))
                    except Exception:
                        pass
        except Exception:
            self.tv_consecutive_failures += 1

    def _check_end_of_day(self, now: datetime, cycle_count: int, symbols: list[str] | None) -> None:
        """Fire daily summary at 5 PM ET (once per day)."""
        ny_h = ny_hour(now)
        from zoneinfo import ZoneInfo
        et_date = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        if ny_h == 17 and now.minute < 2 and self.last_eod_date != et_date:
            self.last_eod_date = et_date
            self.save_end_of_day(cycle_count, symbols)
            print("[HEALTH] 5pm ET — daily summary sent.", flush=True)

    def save_end_of_day(self, cycle_count: int = 0, symbols: list[str] | None = None) -> None:
        """Save end-of-day summary to session store and send alert."""
        summary = self.executor.get_account_summary()
        summary["cycles_run"] = cycle_count
        summary["mode"] = self.mode
        summary["symbols"] = symbols or []
        self.session.set_summary(summary)
        print(f"[SESSION] Saved to {self.session.session_file}", flush=True)

        try:
            asyncio.ensure_future(self.alerts.send_daily_summary(summary, self.mode))
        except RuntimeError:
            pass

    @staticmethod
    def load_todays_trades() -> list[dict]:
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
