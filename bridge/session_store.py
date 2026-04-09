"""
Session Store — structured JSON session persistence.

Saves daily trading sessions to ~/.tradingview-mcp/sessions/YYYY-MM-DD.json
Tracks analyses, decisions, trades, and account snapshots.

Usage:
    from bridge.session_store import SessionStore
    store = SessionStore()
    store.log_analysis(symbol_analysis)
    store.log_decision(trade_decision)
    store.log_trade(trade_event)
    store.save_snapshot(account_summary)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SessionStore:
    """
    Persists daily session data as structured JSON.

    File: ~/.tradingview-mcp/sessions/YYYY-MM-DD.json
    Schema:
    {
        "date": "2026-04-06",
        "started_at": "...",
        "analyses": [...],
        "decisions": [...],
        "trades": [...],
        "account_snapshots": [...],
        "summary": {...}
    }
    """

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or Path.home() / ".tradingview-mcp" / "sessions"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._session_file = self.base_dir / f"{self._today}.json"
        self._data = self._load_or_create()

    def _load_or_create(self) -> dict:
        if self._session_file.exists():
            with open(self._session_file) as f:
                return json.load(f)
        return {
            "date": self._today,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "analyses": [],
            "decisions": [],
            "trades": [],
            "account_snapshots": [],
            "summary": {},
        }

    def _save(self) -> None:
        with open(self._session_file, "w") as f:
            json.dump(self._data, f, indent=2, default=str)

    def log_analysis(self, analysis_dict: dict) -> None:
        """Log an ICT analysis result."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **analysis_dict,
        }
        self._data["analyses"].append(entry)
        self._save()

    def log_decision(self, decision_dict: dict) -> None:
        """Log a trade decision (BUY/SELL/SKIP)."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **decision_dict,
        }
        self._data["decisions"].append(entry)
        self._save()

    def log_trade(self, trade_event: dict) -> None:
        """Log a trade open/close event."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **trade_event,
        }
        self._data["trades"].append(entry)
        self._save()

    def save_snapshot(self, account_summary: dict) -> None:
        """Save an account state snapshot."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **account_summary,
        }
        self._data["account_snapshots"].append(entry)
        self._save()

    def set_summary(self, summary: dict) -> None:
        """Set the end-of-day summary."""
        self._data["summary"] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **summary,
        }
        self._save()

    @property
    def session_file(self) -> Path:
        return self._session_file

    @property
    def analysis_count(self) -> int:
        return len(self._data["analyses"])

    @property
    def trade_count(self) -> int:
        return len(self._data["trades"])


if __name__ == "__main__":
    store = SessionStore()
    store.log_analysis({"symbol": "BTCUSD", "grade": "B", "score": 76.8})
    store.log_decision({"action": "BUY", "symbol": "BTCUSD", "confidence": 80})
    store.log_trade({"ticket": 100001, "symbol": "BTCUSD", "pnl": 200.0})
    store.save_snapshot({"balance": 10200.0, "open_positions": 0})
    print(f"Session saved to: {store.session_file}")
    print(f"Analyses: {store.analysis_count}, Trades: {store.trade_count}")
