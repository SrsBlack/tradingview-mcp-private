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
import signal as _signal


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-Trading Bridge Orchestrator")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper",
                        help="Execution mode (default: paper)")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Override watchlist symbols")
    parser.add_argument("--balance", type=float, default=10_000.0,
                        help="Initial paper balance (default: 10000)")
    parser.add_argument("--interval", type=int, default=60,
                        help="Analysis interval in seconds (default: 60, 0=single cycle)")
    parser.add_argument("--single", action="store_true",
                        help="Run a single analysis cycle and exit")

    args = parser.parse_args()

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
