"""
CLI entry point for the auto-trading bridge.

Usage:
    python -m bridge.cli                      # Paper mode, default watchlist
    python -m bridge.cli --single             # Single cycle then exit
    python -m bridge.cli --symbols BTCUSD ETHUSD
    python -m bridge.cli --mode live          # Live MT5 execution
    python -m bridge.cli --interval 0         # Single cycle (alias for --single)
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import os
import signal as _signal
import sys
from pathlib import Path

LOCK_FILE = Path.home() / ".tradingview-mcp" / "bridge.lock"


def _acquire_lock() -> None:
    """Prevent multiple bridge instances from running simultaneously."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
            # Check if that PID is still alive (Windows-compatible)
            try:
                os.kill(old_pid, 0)
                print(f"[LOCK] Another bridge is already running (PID {old_pid}).", flush=True)
                print(f"[LOCK] If this is stale, delete: {LOCK_FILE}", flush=True)
                sys.exit(1)
            except OSError:
                # Process is dead — stale lock, safe to overwrite
                print(f"[LOCK] Stale lock from PID {old_pid} — overwriting.", flush=True)
        except (ValueError, OSError):
            pass  # Corrupt lock file, overwrite
    LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(_release_lock)
    print(f"[LOCK] Acquired (PID {os.getpid()})", flush=True)


def _release_lock() -> None:
    """Release the process lock on exit."""
    try:
        if LOCK_FILE.exists():
            stored_pid = int(LOCK_FILE.read_text().strip())
            if stored_pid == os.getpid():
                LOCK_FILE.unlink()
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-Trading Bridge Orchestrator")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper",
                        help="Execution mode (default: paper)")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Override watchlist symbols")
    parser.add_argument("--balance", type=float, default=100_000.0,
                        help="Initial paper balance (default: 100000 — matches FTMO live)")
    parser.add_argument("--interval", type=int, default=60,
                        help="Analysis interval in seconds (default: 60, 0=single cycle)")
    parser.add_argument("--single", action="store_true",
                        help="Run a single analysis cycle and exit")

    args = parser.parse_args()

    # Load .env for API keys (ANTHROPIC_API_KEY, ALPACA, etc.)
    # override=True so the on-disk .env always wins over any stale/corrupted
    # value inherited from the parent shell (the bridge auto-restarts via
    # start_bridge_live.bat's :loop, and a bad inherited env var was causing
    # ~80% of Claude calls to fail with 401).
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=True)
        print(f"[ENV] Loaded {env_path} (override=True)", flush=True)
    else:
        print(f"[ENV] WARNING: No .env file at {env_path}", flush=True)

    # Verify critical API key — fail fast rather than silently falling back.
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("[ENV] CRITICAL: ANTHROPIC_API_KEY not set! Claude decisions will be unavailable.", flush=True)
        print("[ENV] Add it to .env or set as environment variable.", flush=True)
        sys.exit(2)
    if not key.startswith("sk-ant-"):
        print(f"[ENV] CRITICAL: ANTHROPIC_API_KEY has unexpected prefix '{key[:10]}...' — refusing to start.", flush=True)
        sys.exit(2)
    print(f"[ENV] ANTHROPIC_API_KEY loaded (len={len(key)}, prefix={key[:12]}...)", flush=True)

    # Acquire process lock BEFORE importing heavy modules
    _acquire_lock()

    from bridge.orchestrator import Orchestrator

    orch = Orchestrator(
        mode=args.mode,
        symbols=args.symbols,
        initial_balance=args.balance,
        analysis_interval=max(args.interval, 10) if args.interval > 0 else 60,
        single_cycle=args.single or args.interval == 0,
    )

    async def _run_with_shutdown():
        loop = asyncio.get_running_loop()

        def _request_shutdown():
            print("\n[SIGNAL] Ctrl+C received — shutting down cleanly...", flush=True)
            orch.stop()
            for task in asyncio.all_tasks(loop):
                task.cancel()

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
