"""
Session Store — structured JSON session persistence.

Saves daily trading sessions to ~/.tradingview-mcp/sessions/YYYY-MM-DD.json
Tracks analyses, decisions, trades, and account snapshots.

Resilience:
  - Atomic writes (tmp file + rename) prevent corruption on crash
  - Backup kept before every write
  - Analyses trimmed to last N entries to prevent unbounded growth
  - Trade and decision logs are never trimmed (audit trail)

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
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Analyses can be 100+ per hour across all symbols — cap to prevent bloat
MAX_ANALYSES = 500
MAX_SNAPSHOTS = 200


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
            try:
                with open(self._session_file, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                # Try backup
                backup = self._session_file.with_suffix(".backup.json")
                if backup.exists():
                    try:
                        print(f"[SessionStore] Primary corrupt, loading backup: {e}", flush=True)
                        with open(backup, encoding="utf-8") as f:
                            return json.load(f)
                    except Exception:
                        pass
                # Both corrupt — archive the broken file and start fresh
                corrupt_path = self._session_file.with_suffix(".corrupt.json")
                print(f"[SessionStore] WARNING: session file corrupt, archiving to {corrupt_path.name}", flush=True)
                try:
                    shutil.copy2(self._session_file, corrupt_path)
                except OSError:
                    pass
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
        """Atomic write: write to tmp, backup old, then rename."""
        tmp_path = self._session_file.with_suffix(".tmp")
        backup_path = self._session_file.with_suffix(".backup.json")
        try:
            payload = json.dumps(self._data, indent=2, default=str)
            tmp_path.write_text(payload, encoding="utf-8")
            if self._session_file.exists():
                shutil.copy2(self._session_file, backup_path)
            os.replace(tmp_path, self._session_file)
        except Exception as e:
            print(f"[SessionStore] WARNING: save failed: {e}", flush=True)
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _trim_if_needed(self) -> None:
        """Trim analyses and snapshots to prevent unbounded growth."""
        if len(self._data["analyses"]) > MAX_ANALYSES:
            self._data["analyses"] = self._data["analyses"][-MAX_ANALYSES:]
        if len(self._data["account_snapshots"]) > MAX_SNAPSHOTS:
            self._data["account_snapshots"] = self._data["account_snapshots"][-MAX_SNAPSHOTS:]

    def log_analysis(self, analysis_dict: dict) -> None:
        """Log an ICT analysis result."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **analysis_dict,
        }
        self._data["analyses"].append(entry)
        self._trim_if_needed()
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
        self._trim_if_needed()
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
