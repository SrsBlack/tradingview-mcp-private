"""
Alerts — Telegram notifications and dashboard integration for the bridge.

Reuses trading-ai-v2's AlertManager for Telegram and LedgerStore for dashboard.
Falls back to console-only if Telegram is not configured.

Usage:
    from bridge.alerts import BridgeAlerts
    alerts = BridgeAlerts()
    await alerts.send_trade_open(decision, result)
    await alerts.send_trade_close(close_event)
    await alerts.send_daily_summary(account_summary)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bridge.config import ensure_trading_ai_path
from bridge.symbol_utils import normalize_symbol

ensure_trading_ai_path()


# ---------------------------------------------------------------------------
# Bridge Alerts
# ---------------------------------------------------------------------------

class BridgeAlerts:
    """
    Alert system for the bridge pipeline.

    Uses trading-ai-v2's AlertManager for Telegram if configured,
    and LedgerStore for the Streamlit dashboard.
    Falls back to console logging if dependencies unavailable.
    """

    def __init__(self, ledger_path: str | Path | None = None):
        self._alert_mgr = None
        self._ledger = None
        self._init_telegram()
        self._init_ledger(ledger_path)

    def _init_telegram(self) -> None:
        """Try to initialize Telegram alerts."""
        try:
            from monitoring.alerts import AlertManager
            from core.config import Settings
            settings = Settings()
            self._alert_mgr = AlertManager(settings)
            if self._alert_mgr.enabled:
                print("[ALERTS] Telegram alerts enabled", flush=True)
            else:
                print("[ALERTS] Telegram not configured (console only)", flush=True)
        except Exception as e:
            print(f"[ALERTS] Telegram unavailable: {e}", flush=True)

    def _init_ledger(self, ledger_path: str | Path | None = None) -> None:
        """Try to initialize LedgerStore for dashboard."""
        try:
            from monitoring.dashboard import LedgerStore
            path = ledger_path or Path.home() / ".tradingview-mcp" / "trading_ledger.db"
            self._ledger = LedgerStore(str(path))
            print(f"[ALERTS] Ledger store: {path}", flush=True)
        except Exception as e:
            print(f"[ALERTS] Ledger unavailable: {e}", flush=True)

    @property
    def telegram_enabled(self) -> bool:
        return self._alert_mgr is not None and self._alert_mgr.enabled

    @property
    def ledger_enabled(self) -> bool:
        return self._ledger is not None

    # ------------------------------------------------------------------
    # Trade alerts
    # ------------------------------------------------------------------

    async def send_trade_open(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        tp2_price: float = 0.0,
        lot_size: float = 0.0,
        grade: str = "",
        score: float = 0.0,
        confidence: int = 0,
        reasoning: str = "",
        ticket: int = 0,
        mode: str = "paper",
    ) -> None:
        """Send trade open notification."""
        rr = abs(tp_price - entry_price) / abs(entry_price - sl_price) if abs(entry_price - sl_price) > 0 else 0

        msg = (
            f"{'📄' if mode == 'paper' else '🔴'} *{mode.upper()} TRADE OPEN*\n\n"
            f"*{direction} {symbol}* @ {entry_price:,.2f}\n"
            f"SL: {sl_price:,.2f} | TP1: {tp_price:,.2f} | TP2: {tp2_price:,.2f}\n"
            f"R:R: {rr:.1f}:1 | Size: {lot_size:.4f}\n"
            f"Grade: {grade} ({score:.0f}/100) | Conf: {confidence}%\n"
            f"_{reasoning}_"
        )

        if self.telegram_enabled:
            await self._alert_mgr.send_raw(msg)

        # Record in ledger
        if self.ledger_enabled and ticket > 0:
            self._ledger.record_entry(
                ticket=ticket,
                symbol=normalize_symbol(symbol),
                direction=direction,
                engine="ICT",
                strategy_name="ICT_Bridge",
                cluster="ICT_REVERSAL",
                entry_price=entry_price,
                lot_size=lot_size,
                sl_price=sl_price,
                tp_price=tp_price,
                entry_time=datetime.now(timezone.utc),
                signal_score=score,
                signal_grade=grade,
            )

        print(f"[ALERT] Trade open: {direction} {symbol} @ {entry_price}", flush=True)

    async def send_trade_close(
        self,
        symbol: str,
        direction: str,
        pnl: float,
        r_multiple: float,
        reason: str,
        balance: float,
        ticket: int = 0,
        exit_price: float = 0.0,
        mode: str = "paper",
    ) -> None:
        """Send trade close notification."""
        emoji = "✅" if pnl >= 0 else "❌"
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

        msg = (
            f"{emoji} *{mode.upper()} TRADE CLOSED*\n\n"
            f"*{direction} {symbol}* closed by {reason}\n"
            f"PnL: {pnl_str} ({r_multiple:+.1f}R achieved)\n"
            f"Exit: {exit_price:,.2f}\n"
            f"Balance: ${balance:,.2f}"
        )

        if self.telegram_enabled:
            await self._alert_mgr.send_raw(msg)

        # Record in ledger
        if self.ledger_enabled and ticket > 0:
            self._ledger.record_close(
                ticket=ticket,
                exit_price=exit_price,
                exit_time=datetime.now(timezone.utc),
                pnl_usd=pnl,
                r_multiple=r_multiple,
            )

        print(f"[ALERT] Trade close: {symbol} {reason} PnL={pnl_str}", flush=True)

    # ------------------------------------------------------------------
    # Daily summary
    # ------------------------------------------------------------------

    async def send_daily_summary(
        self,
        account_summary: dict,
        mode: str = "paper",
    ) -> None:
        """Send end-of-day summary."""
        wins = account_summary.get('wins', 0)
        losses = account_summary.get('losses', 0)
        total = wins + losses
        win_rate = f"{wins/total:.0%}" if total > 0 else "N/A"
        grade_a_wr = account_summary.get('grade_a_win_rate', 'N/A')

        msg = (
            f"📊 *{mode.upper()} DAILY SUMMARY*\n\n"
            f"Balance: ${account_summary.get('balance', 0):,.2f}\n"
            f"Daily PnL: {account_summary.get('daily_pnl_pct', '0.00%')}\n"
            f"Drawdown: {account_summary.get('total_drawdown_pct', '0.00%')}\n"
            f"Trades: {account_summary.get('closed_today', 0)} "
            f"(W:{wins} L:{losses} | Win Rate: {win_rate})\n"
            f"Grade A Win Rate: {grade_a_wr}\n"
            f"Open: {account_summary.get('open_positions', 0)}\n"
            f"Cycles: {account_summary.get('cycles_run', 0)}"
        )

        if self.telegram_enabled:
            await self._alert_mgr.send_raw(msg)

        # Record account snapshot in ledger
        if self.ledger_enabled:
            self._ledger.record_account_snapshot(
                timestamp=datetime.now(timezone.utc),
                balance=account_summary.get("balance", 0),
                equity=account_summary.get("balance", 0),
                daily_pnl_usd=account_summary.get("daily_pnl", 0),
                daily_pnl_pct=float(account_summary.get("daily_pnl_pct", "0%").rstrip("%")) / 100
                    if isinstance(account_summary.get("daily_pnl_pct"), str)
                    else account_summary.get("daily_pnl_pct", 0),
                total_drawdown_pct=float(account_summary.get("total_drawdown_pct", "0%").rstrip("%")) / 100
                    if isinstance(account_summary.get("total_drawdown_pct"), str)
                    else account_summary.get("total_drawdown_pct", 0),
                open_positions=account_summary.get("open_positions", 0),
            )

        print(f"[ALERT] Daily summary sent", flush=True)

    # ------------------------------------------------------------------
    # Warning alerts
    # ------------------------------------------------------------------

    async def send_drawdown_warning(
        self,
        severity: str,
        daily_pnl_pct: float,
        total_drawdown_pct: float,
        mode: str = "paper",
    ) -> None:
        """Send drawdown warning."""
        emoji = {"warning": "⚠️", "critical": "🚨", "limit_reached": "🛑"}.get(severity, "⚠️")

        msg = (
            f"{emoji} *DRAWDOWN {severity.upper()}* ({mode})\n\n"
            f"Daily PnL: {daily_pnl_pct:.2%}\n"
            f"Total DD: {total_drawdown_pct:.2%}"
        )

        if self.telegram_enabled:
            await self._alert_mgr.send_raw(msg)

        if self.ledger_enabled:
            self._ledger.record_drawdown_event(
                timestamp=datetime.now(timezone.utc),
                daily_pnl_pct=daily_pnl_pct,
                total_drawdown_pct=total_drawdown_pct,
                severity=severity,
            )

        print(f"[ALERT] Drawdown {severity}: daily={daily_pnl_pct:.2%} total={total_drawdown_pct:.2%}", flush=True)

    async def send_kill_switch_daily(
        self,
        daily_pnl_pct: float,
        balance: float,
        mode: str = "paper",
    ) -> None:
        """Send daily loss kill switch alert."""
        msg = (
            f"🛑 *KILL SWITCH: Daily Loss Limit* ({mode.upper()})\n\n"
            f"Daily loss: {daily_pnl_pct:.2%} — limit: -2.00%\n"
            f"Balance: ${balance:,.2f}\n"
            f"Trading paused until midnight UTC."
        )
        if self.telegram_enabled:
            await self._alert_mgr.send_raw(msg)
        print(f"[ALERT] Kill switch (daily loss): {daily_pnl_pct:.2%}", flush=True)

    async def send_kill_switch(self, consecutive_losses: int, mode: str = "paper") -> None:
        """Send kill switch activation alert."""
        msg = (
            f"🛑 *KILL SWITCH ACTIVATED* ({mode})\n\n"
            f"{consecutive_losses} consecutive losses.\n"
            f"Trading paused. Manual review required."
        )

        if self.telegram_enabled:
            await self._alert_mgr.send_raw(msg)

        print(f"[ALERT] Kill switch: {consecutive_losses} consecutive losses", flush=True)

    # ------------------------------------------------------------------
    # Generic
    # ------------------------------------------------------------------

    async def send_raw(self, text: str) -> None:
        """Send raw message (for custom alerts)."""
        if self.telegram_enabled:
            await self._alert_mgr.send_raw(text)
        print(f"[ALERT] {text[:100]}", flush=True)


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    async def test():
        alerts = BridgeAlerts()

        await alerts.send_trade_open(
            symbol="BTCUSD", direction="BUY",
            entry_price=69000.0, sl_price=68500.0, tp_price=70000.0,
            lot_size=0.20, grade="B", score=76.8,
            confidence=80, reasoning="Test trade for bridge verification",
            ticket=100001, mode="paper",
        )

        await alerts.send_trade_close(
            symbol="BTCUSD", direction="BUY",
            pnl=200.0, r_multiple=2.0, reason="TP",
            balance=10200.0, ticket=100001, exit_price=70000.0,
            mode="paper",
        )

        await alerts.send_daily_summary({
            "balance": 10200.0,
            "daily_pnl_pct": "2.00%",
            "total_drawdown_pct": "0.00%",
            "closed_today": 1,
            "wins": 1,
            "losses": 0,
            "open_positions": 0,
            "cycles_run": 5,
        })

    asyncio.run(test())
