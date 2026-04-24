"""
StateStore — persistent bridge state across restarts.

Saves to ~/.tradingview-mcp/bridge_state.json on every change.
On startup, the orchestrator loads this file to restore:
  - Open positions (so they can still be tracked/closed)
  - Running balance and P&L accumulators
  - Win/loss counters

This is separate from SessionStore (daily log) — StateStore is the
"hot" state that must survive a crash or Ctrl+C restart.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_PATH = Path.home() / ".tradingview-mcp" / "bridge_state.json"


class StateStore:
    """
    Persists open positions and account state between restarts.

    Schema:
    {
        "saved_at": "ISO timestamp",
        "mode": "paper" | "live",
        "balance": float,
        "initial_balance": float,
        "peak_balance": float,
        "wins": int,
        "losses": int,
        "grade_a_wins": int,
        "grade_a_losses": int,
        "open_positions": [
            {
                "ticket": int,
                "symbol": str,
                "direction": str,
                "entry_price": float,
                "sl_price": float,
                "tp_price": float,
                "tp2_price": float,
                "lot_size": float,
                "risk_pct": float,
                "opened_at": str,
                "ict_grade": str,
                "ict_score": float,
                "reasoning": str,
                "trade_type": str,
                "trailing_sl": float,
                "tp1_hit": bool,
            },
            ...
        ]
    }
    """

    def __init__(self, path: Path | None = None):
        self._path = path or STATE_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, executor: Any, mode: str) -> None:
        """Snapshot executor state to disk with atomic write."""
        # Acquire positions lock to avoid "dictionary changed size" during iteration
        lock = getattr(executor, '_positions_lock', None)
        positions = []
        if lock:
            with lock:
                for pos in executor.open_positions.values():
                    positions.append(pos.to_dict())
        else:
            for pos in executor.open_positions.values():
                positions.append(pos.to_dict())

        state = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "balance": executor.balance,
            "initial_balance": executor.initial_balance,
            "peak_balance": executor.peak_balance,
            "wins": executor.wins,
            "losses": executor.losses,
            "grade_a_wins": getattr(executor, "grade_a_wins", 0),
            "grade_a_losses": getattr(executor, "grade_a_losses", 0),
            "day_start_balance": getattr(executor, "_day_start_balance", executor.balance),
            "day_start_date": getattr(executor, "_day_start_date", ""),
            "open_positions": positions,
        }
        tmp_path = self._path.with_suffix(".tmp")
        backup_path = self._path.with_suffix(".backup.json")
        try:
            tmp_path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
            if self._path.exists():
                shutil.copy2(self._path, backup_path)
            os.replace(tmp_path, self._path)
        except Exception as e:
            print(f"[StateStore] WARNING: save failed: {e}", flush=True)
            # Clean up partial tmp file
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def load(self) -> dict | None:
        """Load saved state. Returns None if no state file exists."""
        if not self._path.exists():
            return None
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            backup_path = self._path.with_suffix(".backup.json")
            if backup_path.exists():
                try:
                    print(f"[StateStore] WARNING: primary state file corrupt, falling back to backup: {backup_path}")
                    return json.loads(backup_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return None

    def clear(self) -> None:
        """Delete state file (call on clean session start if desired)."""
        if self._path.exists():
            self._path.unlink()

    def restore_into(self, executor: Any, mode: str) -> list[dict]:
        """
        Restore saved state into an executor.

        Returns list of restored positions (for display at startup).
        Only restores if mode matches and state is from today.
        """
        state = self.load()
        if not state:
            return []

        # Only restore same mode
        if state.get("mode") != mode:
            return []

        # Restore balance and stats — but for live mode, prefer the MT5-synced
        # balance already set on the executor over stale persisted values.
        if mode == "live" and hasattr(executor, '_get_mt5_balance'):
            mt5_bal = executor._get_mt5_balance()
            if mt5_bal is not None:
                executor.balance = mt5_bal
                # Infer correct initial_balance from MT5 account size.
                # FTMO accounts are 10k/25k/50k/100k/200k — pick the nearest.
                stored_initial = state.get("initial_balance", executor.initial_balance)
                if mt5_bal > stored_initial * 2:
                    for tier in [200_000, 100_000, 50_000, 25_000, 10_000]:
                        if mt5_bal >= tier * 0.85:
                            stored_initial = float(tier)
                            break
                    print(f"  [STATE] Corrected initial_balance to ${stored_initial:,.0f} (was ${state.get('initial_balance', 0):,.0f})", flush=True)
                executor.initial_balance = stored_initial
                executor.peak_balance = max(state.get("peak_balance", mt5_bal), mt5_bal)
                print(f"  [STATE] Live balance synced from MT5: ${mt5_bal:,.2f}", flush=True)
            else:
                executor.balance = state.get("balance", executor.balance)
                executor.initial_balance = state.get("initial_balance", executor.initial_balance)
                executor.peak_balance = state.get("peak_balance", executor.peak_balance)
        else:
            executor.balance = state.get("balance", executor.balance)
            executor.initial_balance = state.get("initial_balance", executor.initial_balance)
            executor.peak_balance = state.get("peak_balance", executor.peak_balance)
        executor.wins = state.get("wins", 0)
        executor.losses = state.get("losses", 0)
        if hasattr(executor, "grade_a_wins"):
            executor.grade_a_wins = state.get("grade_a_wins", 0)
            executor.grade_a_losses = state.get("grade_a_losses", 0)

        # Restore day-start balance for daily P&L tracking.
        # If the saved date is today, use the persisted day-start balance.
        # If it's a new day, use the current MT5 balance as the new baseline.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if hasattr(executor, "_day_start_balance"):
            saved_day = state.get("day_start_date", "")
            if saved_day == today:
                executor._day_start_balance = state.get("day_start_balance", executor.balance)
                executor._day_start_date = saved_day
            else:
                # New day — current balance IS the day-start baseline
                executor._day_start_balance = executor.balance
                executor._day_start_date = today
                print(f"  [STATE] New day — day-start balance: ${executor._day_start_balance:,.2f}", flush=True)

        # Restore positions even across days — swing trades can span multiple days.
        # The position manager will reconcile against MT5 on the next check cycle.
        saved_date = state.get("saved_at", "")[:10]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        positions_to_restore = state.get("open_positions", [])
        if saved_date != today:
            n_pos = len(positions_to_restore)
            if n_pos > 0:
                print(f"  [STATE] Balance restored from {saved_date}: ${executor.balance:,.2f} ({n_pos} positions carried over)", flush=True)
            else:
                print(f"  [STATE] Balance restored from {saved_date}: ${executor.balance:,.2f} (no open positions)", flush=True)

        # Restore open positions
        from bridge.decision_types import PaperPosition
        restored = []
        for p in state.get("open_positions", []):
            try:
                pos = PaperPosition(
                    ticket=p["ticket"],
                    symbol=p["symbol"],
                    direction=p["direction"],
                    entry_price=p["entry_price"],
                    sl_price=p["sl_price"],
                    tp_price=p["tp_price"],
                    tp2_price=p.get("tp2_price", 0.0),
                    lot_size=p["lot_size"],
                    risk_pct=p.get("risk_pct", 0.01),
                    opened_at=p.get("opened_at", ""),
                    ict_grade=p.get("ict_grade", ""),
                    ict_score=p.get("ict_score", 0.0),
                    reasoning=p.get("reasoning", ""),
                    trade_type=p.get("trade_type", "intraday"),
                    trailing_sl=p.get("trailing_sl", p["sl_price"]),
                    tp1_hit=p.get("tp1_hit", False),
                    current_price=p["entry_price"],
                )
                executor.open_positions[pos.ticket] = pos

                # CRITICAL: For live mode, also populate the underlying
                # LiveExecutor's open_tickets dict. Without this, modify_sl()
                # and close_position() silently return False for state-restored
                # positions (they check open_tickets, not open_positions).
                if mode == "live" and hasattr(executor, "_live") and hasattr(executor._live, "open_tickets"):
                    from bridge.config import tv_to_ftmo_symbol
                    ftmo_sym = tv_to_ftmo_symbol(p["symbol"])
                    executor._live.open_tickets[pos.ticket] = {
                        "symbol": ftmo_sym,
                        "tv_symbol": p["symbol"],
                        "direction": p["direction"],
                        "entry_price": p["entry_price"],
                        "sl_price": p["sl_price"],
                        "tp_price": p["tp_price"],
                        "tp2_price": p.get("tp2_price", 0.0),
                        "tp1_hit": p.get("tp1_hit", False),
                        "lot_size": p["lot_size"],
                        "opened_at": p.get("opened_at", ""),
                    }

                # Advance ticket counter past restored tickets
                if hasattr(executor, "_next_ticket"):
                    executor._next_ticket = max(executor._next_ticket, pos.ticket + 1)
                restored.append(p)
            except Exception as e:
                print(f"  [STATE] Failed to restore position: {e} — data: {p}", flush=True)

        return restored
