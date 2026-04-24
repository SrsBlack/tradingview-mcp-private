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
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator


MCP_ROOT = Path(__file__).resolve().parent.parent
_SWITCH_DELAY = 2.0  # seconds between symbol/timeframe switches

# GLOBAL exclusive lock for ALL TV access across the process.
# There is only one TradingView chart in the Electron app. Every TVClient
# instance (analysis loop, position loop, strategy engine, etc.) must
# serialize through this lock so one caller's set_symbol() can't race
# another caller's get_ohlcv(). RLock so the same thread can nest calls
# (e.g. switch_chart() → set_symbol() + set_timeframe()).
_TV_LOCK: threading.RLock = threading.RLock()

# SESSION lock — held for the entire switch+collect sequence.
# The _TV_LOCK only serializes individual subprocess calls. But a pipeline
# operation (switch symbol → sleep → switch TF → sleep → read OHLCV)
# spans multiple _run() calls with sleeps in between. Without a session
# lock, another thread grabs _TV_LOCK between calls and switches the chart
# to a different symbol, causing cross-symbol contamination.
#
# Callers use `with client.chart_session():` to hold exclusive chart access
# for their entire multi-step operation. Individual _run() calls inside
# still acquire _TV_LOCK (harmless since it's an RLock), ensuring the
# existing delay logic still works.
_CHART_SESSION_LOCK: threading.Lock = threading.Lock()

# Shared last-call timestamp, also protected by _TV_LOCK.
_TV_LAST_CALL_TIME: float = 0.0


class TVClientError(Exception):
    """Raised when a TradingView MCP CLI command fails."""


class TVClient:
    """Subprocess-based client for the TradingView MCP CLI.

    All instances share a process-wide lock and a process-wide
    last-call timestamp, so concurrent code paths cannot race the
    single TradingView chart.
    """

    def __init__(self, mcp_root: Path | None = None):
        self.mcp_root = mcp_root or MCP_ROOT
        # Kept as a property for API compat, but the real one is global.
        self._last_call_time: float = 0.0

    @staticmethod
    @contextmanager
    def chart_session() -> Generator[None, None, None]:
        """Hold exclusive chart access for a multi-step switch+collect sequence.

        Usage::

            with client.chart_session():
                client.set_symbol("BTCUSD", require_ready=True)
                time.sleep(5.0)
                quote = client.get_quote()
                client.set_timeframe("240")
                time.sleep(2.5)
                bars = client.get_ohlcv_verified("BTCUSD")

        While this context manager is held, no other thread can begin a
        chart session (they block on _CHART_SESSION_LOCK). This prevents
        symbol switching between the set_symbol and get_ohlcv calls.
        """
        _CHART_SESSION_LOCK.acquire()
        try:
            yield
        finally:
            _CHART_SESSION_LOCK.release()

    # ------------------------------------------------------------------
    # Low-level runner
    # ------------------------------------------------------------------

    def _run(self, args: list[str], timeout: int = 30) -> dict[str, Any]:
        """
        Run `npm run tv -- <args>` and return parsed JSON.

        Serialized process-wide via _TV_LOCK to prevent chart contention
        across the analysis loop, position loop, strategy engine, etc.
        """
        global _TV_LAST_CALL_TIME
        with _TV_LOCK:
            elapsed = time.time() - _TV_LAST_CALL_TIME
            if elapsed < _SWITCH_DELAY:
                time.sleep(_SWITCH_DELAY - elapsed)
            try:
                return self._run_locked(args, timeout)
            finally:
                _TV_LAST_CALL_TIME = time.time()
                self._last_call_time = _TV_LAST_CALL_TIME

    def _run_locked(self, args: list[str], timeout: int = 30) -> dict[str, Any]:
        """Actual subprocess call — only invoked while holding _TV_LOCK."""

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

    def set_symbol(self, symbol: str, require_ready: bool = False) -> dict:
        """Switch chart to a different symbol.

        Args:
            symbol: TradingView symbol (e.g. "BITSTAMP:BTCUSD")
            require_ready: If True, retry up to 3 times if chart_ready is False.

        Returns:
            {"success": true, "symbol": "...", "chart_ready": bool}
        """
        result = self._run(["symbol", symbol])
        if require_ready and not result.get("chart_ready", False):
            target_sym = symbol.split(":")[-1]
            for attempt in range(5):
                delay = 2.0 + attempt * 1.0
                time.sleep(delay)
                try:
                    q = self.get_quote()
                    chart_sym = q.get("symbol", "").split(":")[-1]
                    if chart_sym == target_sym and float(q.get("last", 0)) > 0:
                        result["chart_ready"] = True
                        break
                except TVClientError:
                    pass
                if attempt == 2:
                    try:
                        result = self._run(["symbol", symbol])
                        if result.get("chart_ready", False):
                            break
                    except TVClientError:
                        pass
        return result

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

    def get_ohlcv_verified(self, expected_symbol: str, count: int = 200) -> dict | None:
        """Get OHLCV and verify the chart is still on the expected symbol.

        Verifies quote BEFORE reading OHLCV to ensure the chart has fully
        loaded the expected symbol, then verifies again AFTER to catch drift.
        Returns None if either check fails (contamination detected).
        """
        target_sym = expected_symbol.split(":")[-1]

        # Pre-check: ensure chart is on the right symbol before reading bars
        try:
            q = self.get_quote()
            chart_sym = q.get("symbol", "").split(":")[-1]
            if chart_sym != target_sym:
                print(
                    f"  [TV] Symbol drift detected: expected {target_sym}, "
                    f"chart shows {chart_sym}. Discarding OHLCV data.",
                    flush=True,
                )
                return None
        except TVClientError:
            pass

        bars = self._run(["ohlcv", "-n", str(min(count, 500))])

        # Post-check: verify symbol hasn't drifted during OHLCV read
        try:
            q = self.get_quote()
            chart_sym = q.get("symbol", "").split(":")[-1]
            if chart_sym != target_sym:
                print(
                    f"  [TV] Symbol drift detected: expected {target_sym}, "
                    f"chart shows {chart_sym}. Discarding OHLCV data.",
                    flush=True,
                )
                return None
        except TVClientError:
            pass

        return bars

    def health_check(self) -> bool:
        """Quick connectivity check — returns True if TradingView CDP is responsive.

        Uses 'status' instead of 'quote' because quote fails when the chart
        is on a closed-market symbol (e.g. GER40 after European close).
        A failed quote does NOT mean TradingView is disconnected — it means
        the current symbol has no live data. CDP status is the correct check.
        """
        try:
            result = self._run(["status"], timeout=10)
            return isinstance(result, dict) and result.get("cdp_connected", False)
        except Exception:
            return False

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

    def draw_list(self) -> dict:
        """List all drawings on the chart. Returns {shapes: [{id, name}, ...]}."""
        return self._run(["draw", "list"])

    def draw_get_properties(self, entity_id: str) -> dict:
        """Get properties of a drawing by entity_id. Returns text, points, etc."""
        return self._run(["draw", "get", entity_id])

    def draw_remove(self, entity_id: str) -> dict:
        """Remove a single drawing by its entity_id. Never touches other drawings."""
        # entity_id is a positional argument, NOT --id flag
        return self._run(["draw", "remove", entity_id])

    def draw_trade(self, symbol: str, direction: str, entry: float,
                   sl: float, tp1: float, tp2: float, grade: str, ticket: int) -> list[str]:
        """
        Draw entry, SL, TP1, TP2 lines on the chart for a trade.
        Ensures chart is on the correct symbol before drawing.
        Returns list of entity_ids so they can be removed on close.
        NEVER calls draw_clear — only adds new shapes.
        """
        entity_ids: list[str] = []
        try:
            # Ensure chart is on the correct symbol before drawing
            self.set_symbol(symbol, require_ready=True)

            # Get current timestamp for arrow placement
            import time as _time
            now_ts = int(_time.time())

            color_entry = "#2196F3"   # blue
            color_sl = "#F44336"      # red
            color_tp = "#4CAF50"      # green

            # Entry arrow — placed at current time + entry price
            arrow = "▲" if direction == "BUY" else "▼"
            arrow_color = "#00E676" if direction == "BUY" else "#FF1744"
            args = ["draw", "shape", "-t", "text", "-p", str(entry),
                    "--time", str(now_ts),
                    "--text", f"{arrow} #{ticket} {grade} {direction}",
                    "--overrides", json.dumps({"color": arrow_color, "fontsize": 14, "bold": True})]
            r = self._run(args)
            if r.get("entity_id"):
                entity_ids.append(r["entity_id"])

            # Horizontal lines (these span the full chart, no time needed)
            r = self.draw_shape("horizontal_line", entry,
                                text=f"#{ticket} {symbol.split(':')[-1]} ENTRY {direction} {grade}",
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

    def draw_remove_stale_trades(self, active_tickets: set[str] | None = None) -> int:
        """Remove trade drawings that no longer have an open position.

        Identifies trade drawings by checking if their text/title contains '#<number>'
        (our trade drawing convention: '#123 ENTRY', '#123 SL', '#123 TP1', etc.).
        Only removes drawings whose ticket is NOT in active_tickets.

        Uses the enhanced draw_list which returns text/title inline (no N+1 queries).

        Args:
            active_tickets: Set of ticket strings (e.g. {"123", "P-5"}) to keep.
                            If None, removes ALL trade drawings.

        Returns:
            Number of drawings removed.
        """
        import re
        active = active_tickets or set()
        removed = 0
        try:
            result = self.draw_list()
            shapes = result.get("shapes", [])
            for shape in shapes:
                eid = shape.get("id", "")
                if not eid:
                    continue
                # draw_list now returns title/text inline
                text = str(shape.get("title", "") or shape.get("text", "") or "")
                # Match our trade drawing pattern: #<number>
                match = re.search(r"#(\d+)", text)
                if match:
                    ticket_str = match.group(1)
                    # Keep if ticket is in active set
                    if ticket_str in active:
                        continue
                    # Also check paper prefix
                    if f"P-{ticket_str}" in active:
                        continue
                    self.draw_remove(eid)
                    removed += 1
        except Exception as e:
            print(f"  [DRAW] Warning: could not list drawings for cleanup: {e}", flush=True)
        return removed

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

        with self.chart_session():
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
