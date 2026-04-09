"""
TradingView MCP CLI client — subprocess wrapper for chart data access.

Calls `npm run tv -- <command>` and parses JSON output.
Handles retries, JSON extraction from mixed stdout, and delay management.

Usage:
    from bridge.tv_client import TVClient
    client = TVClient()
    bars = client.get_ohlcv("BTCUSD", "240", count=200)
    quote = client.get_quote()
    values = client.get_study_values()
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any


MCP_ROOT = Path(__file__).resolve().parent.parent
_SWITCH_DELAY = 2.0  # seconds between symbol/timeframe switches


class TVClientError(Exception):
    """Raised when a TradingView MCP CLI command fails."""


class TVClient:
    """Subprocess-based client for the TradingView MCP CLI."""

    def __init__(self, mcp_root: Path | None = None):
        self.mcp_root = mcp_root or MCP_ROOT
        self._last_call_time: float = 0.0

    # ------------------------------------------------------------------
    # Low-level runner
    # ------------------------------------------------------------------

    def _run(self, args: list[str], timeout: int = 30) -> dict[str, Any]:
        """
        Run `npm run tv -- <args>` and return parsed JSON.

        Handles npm stderr noise and extracts JSON from stdout.
        """
        # Rate limiting: ensure minimum delay between calls
        elapsed = time.time() - self._last_call_time
        if elapsed < _SWITCH_DELAY:
            time.sleep(_SWITCH_DELAY - elapsed)

        cmd = ["npm", "run", "tv", "--"] + args
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.mcp_root),
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=True,  # Required on Windows for npm
            )
        except subprocess.TimeoutExpired:
            raise TVClientError(f"Timeout ({timeout}s) for: {' '.join(args)}")
        except FileNotFoundError:
            raise TVClientError("npm not found. Is Node.js installed?")

        self._last_call_time = time.time()

        # Parse JSON from stdout (may contain npm lifecycle noise before the JSON)
        stdout = result.stdout.strip()
        if not stdout:
            raise TVClientError(
                f"Empty output from: {' '.join(args)}\nstderr: {result.stderr[:500]}"
            )

        # Find the first '{' or '[' — skip npm noise lines
        json_start = -1
        for i, ch in enumerate(stdout):
            if ch in ('{', '['):
                json_start = i
                break

        if json_start < 0:
            raise TVClientError(
                f"No JSON found in output: {stdout[:200]}\nstderr: {result.stderr[:300]}"
            )

        try:
            data = json.loads(stdout[json_start:])
        except json.JSONDecodeError:
            # Multiple JSON objects in output (e.g. draw returns overrides echo + result)
            # Take the last complete JSON object
            last_start = stdout.rfind("{")
            if last_start >= 0 and last_start != json_start:
                try:
                    data = json.loads(stdout[last_start:])
                except json.JSONDecodeError as e2:
                    raise TVClientError(
                        f"JSON parse error: {e2}\nRaw output: {stdout[json_start:json_start+300]}"
                    )
            else:
                raise TVClientError(
                    f"JSON parse error\nRaw output: {stdout[json_start:json_start+300]}"
                )

        # Check for tool-level failure
        if isinstance(data, dict) and data.get("success") is False:
            raise TVClientError(f"Tool error: {data.get('error', 'unknown')}")

        return data

    # ------------------------------------------------------------------
    # Chart control
    # ------------------------------------------------------------------

    def set_symbol(self, symbol: str) -> dict:
        """Switch chart to a different symbol."""
        return self._run(["symbol", symbol])

    def set_timeframe(self, tf: str) -> dict:
        """Switch chart timeframe. tf is TradingView resolution string (e.g., '240', '15', 'D')."""
        return self._run(["timeframe", tf])

    def switch_chart(self, symbol: str, timeframe: str) -> None:
        """Switch both symbol and timeframe, with delay."""
        self.set_symbol(symbol)
        time.sleep(1.0)
        self.set_timeframe(timeframe)
        time.sleep(2.0)  # Allow chart to load new data

    # ------------------------------------------------------------------
    # Data retrieval
    # ------------------------------------------------------------------

    def get_ohlcv(self, symbol: str | None = None, timeframe: str | None = None,
                  count: int = 200, switch: bool = True) -> dict:
        """
        Get OHLCV bar data from the active chart.

        If symbol/timeframe are provided and switch=True, switches chart first.

        Returns: {"success": true, "bar_count": N, "bars": [{time, open, high, low, close, volume}, ...]}
        """
        if switch and (symbol or timeframe):
            if symbol:
                self.set_symbol(symbol)
                time.sleep(2.0)  # Wait for symbol + data to load
            if timeframe:
                self.set_timeframe(timeframe)
                time.sleep(1.0)  # Timeframe switch is faster

        return self._run(["ohlcv", "-n", str(min(count, 500))])

    def get_quote(self) -> dict:
        """Get real-time quote for the current chart symbol."""
        return self._run(["quote"])

    def get_study_values(self) -> dict:
        """Get current indicator values from the data window."""
        return self._run(["values"])

    def get_chart_state(self) -> dict:
        """Get current chart state (symbol, timeframe, all indicators)."""
        return self._run(["state"])

    def get_pine_lines(self, study_filter: str | None = None) -> dict:
        """Get horizontal price levels drawn by Pine indicators."""
        args = ["data", "lines"]
        if study_filter:
            args += ["--study-filter", study_filter]
        return self._run(args)

    def get_pine_labels(self, study_filter: str | None = None) -> dict:
        """Get text annotations with prices from Pine indicators."""
        args = ["data", "labels"]
        if study_filter:
            args += ["--study-filter", study_filter]
        return self._run(args)

    def get_pine_boxes(self, study_filter: str | None = None) -> dict:
        """Get price zone boxes {high, low} from Pine indicators."""
        args = ["data", "boxes"]
        if study_filter:
            args += ["--study-filter", study_filter]
        return self._run(args)

    def draw_shape(self, shape_type: str, price: float, text: str = "",
                   overrides: dict | None = None) -> dict:
        """Draw a shape on the current chart. Returns the entity_id of the new shape."""
        args = ["draw", "shape", "-t", shape_type, "-p", str(price)]
        if text:
            args += ["--text", text]
        if overrides:
            import json as _json
            args += ["--overrides", _json.dumps(overrides)]
        return self._run(args)

    def draw_remove(self, entity_id: str) -> dict:
        """Remove a single drawing by its entity_id. Never touches other drawings."""
        # entity_id is a positional argument, NOT --id flag
        return self._run(["draw", "remove", entity_id])

    def draw_trade(self, symbol: str, direction: str, entry: float,
                   sl: float, tp1: float, tp2: float, grade: str, ticket: int) -> list[str]:
        """
        Draw entry, SL, TP1, TP2 lines on the chart for a trade.
        Switches to the symbol first.
        Returns list of entity_ids so they can be removed on close.
        NEVER calls draw_clear — only adds new shapes.
        """
        entity_ids: list[str] = []
        try:
            color_entry = "#2196F3"   # blue
            color_sl = "#F44336"      # red
            color_tp = "#4CAF50"      # green

            # Entry arrow — points toward price action (up for BUY, down for SELL)
            arrow = "▲" if direction == "BUY" else "▼"
            arrow_color = "#00E676" if direction == "BUY" else "#FF1744"
            r = self.draw_shape("text", entry,
                                text=f"{arrow} #{ticket} {grade} {direction}",
                                overrides={"color": arrow_color, "fontsize": 14, "bold": True})
            if r.get("entity_id"):
                entity_ids.append(r["entity_id"])

            r = self.draw_shape("horizontal_line", entry,
                                text=f"#{ticket} {symbol} ENTRY {direction} {grade}",
                                overrides={"linecolor": color_entry, "linewidth": 2})
            if r.get("entity_id"):
                entity_ids.append(r["entity_id"])

            r = self.draw_shape("horizontal_line", sl,
                                text=f"#{ticket} SL",
                                overrides={"linecolor": color_sl, "linewidth": 1, "linestyle": 2})
            if r.get("entity_id"):
                entity_ids.append(r["entity_id"])

            r = self.draw_shape("horizontal_line", tp1,
                                text=f"#{ticket} TP1 (50%)",
                                overrides={"linecolor": color_tp, "linewidth": 1})
            if r.get("entity_id"):
                entity_ids.append(r["entity_id"])

            if tp2 and tp2 != tp1:
                r = self.draw_shape("horizontal_line", tp2,
                                    text=f"#{ticket} TP2",
                                    overrides={"linecolor": color_tp, "linewidth": 2})
                if r.get("entity_id"):
                    entity_ids.append(r["entity_id"])

        except Exception as e:
            print(f"  [DRAW] Warning: could not draw trade on chart: {e}", flush=True)

        return entity_ids

    def draw_remove_trade(self, entity_ids: list[str]) -> None:
        """Remove only the lines drawn for a specific trade, by their saved entity_ids."""
        for eid in entity_ids:
            try:
                self.draw_remove(eid)
            except Exception:
                pass

    def run_brief(self) -> dict:
        """Run the morning brief scan across the watchlist."""
        return self._run(["brief"], timeout=60)

    # ------------------------------------------------------------------
    # Multi-symbol data collection
    # ------------------------------------------------------------------

    def collect_multi_tf(
        self,
        symbol: str,
        timeframes: list[str],
        counts: dict[str, int] | None = None,
    ) -> dict[str, dict]:
        """
        Collect OHLCV data for a symbol across multiple timeframes.

        Args:
            symbol: TradingView symbol name
            timeframes: List of TF strings (e.g., ["240", "60", "15"])
            counts: Optional per-TF bar counts

        Returns:
            {tf: ohlcv_response, ...}
        """
        counts = counts or {}
        results: dict[str, dict] = {}

        self.set_symbol(symbol)
        time.sleep(0.5)

        for tf in timeframes:
            count = counts.get(tf, 200)
            self.set_timeframe(tf)
            time.sleep(0.7)  # Wait for chart to update
            data = self._run(["ohlcv", "-n", str(min(count, 500))])
            results[tf] = data

        return results


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    client = TVClient()
    print("Testing TVClient...")

    # Test quote
    try:
        q = client.get_quote()
        print(f"Quote: {q.get('symbol')} @ {q.get('last')}")
    except TVClientError as e:
        print(f"Quote failed: {e}")

    # Test OHLCV (5 bars, no switch)
    try:
        bars = client.get_ohlcv(count=5, switch=False)
        print(f"OHLCV: {bars.get('bar_count')} bars from {bars.get('source')}")
        if bars.get("bars"):
            last = bars["bars"][-1]
            print(f"  Last bar: O={last['open']} H={last['high']} L={last['low']} C={last['close']}")
    except TVClientError as e:
        print(f"OHLCV failed: {e}")
