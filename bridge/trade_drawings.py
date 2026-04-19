"""
Trade drawing persistence — manages chart entity IDs for trade visualizations.

Saves/restores/cleans up TradingView chart drawings (entry, SL, TP lines)
so they survive bridge restarts and get removed when positions close.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bridge.tv_client import TVClient


class TradeDrawingManager:
    """Manages chart entity IDs for trade level drawings."""

    def __init__(self, tv_client: TVClient, path: Path | None = None):
        self._tv_client = tv_client
        self._drawings: dict[int | str, list[str]] = {}
        self._path = path or Path.home() / ".tradingview-mcp" / "trade_drawings.json"
        self.restore()

    def save(self) -> None:
        """Persist trade drawing entity IDs to disk."""
        try:
            data = {str(k): v for k, v in self._drawings.items()}
            self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def restore(self) -> None:
        """Load saved trade drawing entity IDs from disk."""
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for k, v in data.items():
                try:
                    self._drawings[int(k)] = v
                except ValueError:
                    self._drawings[k] = v
        except Exception:
            pass

    def add(self, key: int | str, entity_ids: list[str]) -> None:
        """Record entity IDs for a trade's chart drawings."""
        self._drawings[key] = entity_ids
        self.save()

    def remove(self, key: int | str) -> list[str]:
        """Remove and return entity IDs for a trade. Removes from chart too."""
        entity_ids = self._drawings.pop(key, [])
        if entity_ids:
            try:
                self._tv_client.draw_remove_trade(entity_ids)
            except Exception:
                pass
            self.save()
        return entity_ids

    def get(self, key: int | str) -> list[str]:
        """Get entity IDs for a trade without removing."""
        return self._drawings.get(key, [])

    @property
    def all_drawings(self) -> dict[int | str, list[str]]:
        return self._drawings

    def cleanup_stale(
        self,
        active_tickets: set[str],
        watchlist: list[str] | None = None,
    ) -> int:
        """Remove chart drawings for positions that are no longer open.

        Uses saved entity IDs first (fast, precise). Then falls back to
        scanning chart drawings by text pattern for any orphaned lines.

        If watchlist is provided, cycles through each symbol to scan for
        orphaned drawings on every chart — not just the currently loaded one.

        Returns total number of drawing elements removed.
        """
        # Step 1: Remove drawings for closed positions via saved entity IDs
        stale_keys = []
        for key in self._drawings:
            key_str = str(key)
            ticket_str = key_str.replace("paper_", "")
            if ticket_str not in active_tickets:
                stale_keys.append(key)

        removed_tracked = 0
        for key in stale_keys:
            entity_ids = self._drawings.pop(key, [])
            try:
                self._tv_client.draw_remove_trade(entity_ids)
                removed_tracked += len(entity_ids)
            except Exception:
                pass

        # Step 2: Scan chart(s) for orphaned trade drawings
        removed_orphan = 0
        if watchlist:
            # Cycle through every watchlist symbol to catch orphans on any chart
            for symbol in watchlist:
                try:
                    self._tv_client.set_symbol(symbol, require_ready=True)
                    removed_orphan += self._tv_client.draw_remove_stale_trades(active_tickets)
                except Exception as e:
                    print(f"  [DRAW] Cleanup failed for {symbol}: {e}", flush=True)
        else:
            # Fallback: only scan current chart
            removed_orphan = self._tv_client.draw_remove_stale_trades(active_tickets)

        total = removed_tracked + removed_orphan
        if total > 0:
            print(f"  [DRAW] Cleaned up {total} stale trade drawing(s) "
                  f"({removed_tracked} tracked, {removed_orphan} orphaned)", flush=True)
            self.save()

        return total
