"""Backfill ledger rows with close data from session JSON + paper JSONL logs.

The live bridge previously logged broker-side closes only to the session
store, never calling LedgerStore.record_close(). This left ~13 rows stuck
as status='open' with exit_price=0. This script scans every known close
event in session files and applies them to the ledger by ticket.

Run once, then let the fixed orchestrator handle new closes.

Usage:
    python scripts/backfill_ledger.py          # dry run
    python scripts/backfill_ledger.py --apply  # write changes
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from pathlib import Path

# Make bridge imports work
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.symbol_utils import normalize_symbol  # noqa: E402


LEDGER_PATH = Path.home() / ".tradingview-mcp" / "trading_ledger.db"
SESSIONS_DIR = Path.home() / ".tradingview-mcp" / "sessions"


def collect_close_events() -> dict[int, dict]:
    """Return latest CLOSE event per ticket across all session files."""
    by_ticket: dict[int, dict] = {}
    for f in sorted(SESSIONS_DIR.glob("2026-*.json")):
        if ".backup" in f.name or ".corrupt" in f.name:
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            print(f"[skip] {f.name}: {e}")
            continue
        for t in data.get("trades", []):
            if t.get("event") != "CLOSE":
                continue
            ticket = t.get("ticket")
            if not ticket:
                continue
            by_ticket[int(ticket)] = t
    return by_ticket


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write to ledger DB")
    args = ap.parse_args()

    if not LEDGER_PATH.exists():
        print(f"No ledger at {LEDGER_PATH}")
        return 1

    closes = collect_close_events()
    print(f"Collected {len(closes)} CLOSE events from session files")

    con = sqlite3.connect(str(LEDGER_PATH))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        "SELECT ticket, symbol, direction, entry_price, status "
        "FROM trades WHERE status = 'open'"
    )
    open_rows = cur.fetchall()
    print(f"Ledger has {len(open_rows)} open rows")

    updated = 0
    unmatched: list[int] = []
    for row in open_rows:
        ticket = row["ticket"]
        ev = closes.get(ticket)
        if not ev:
            unmatched.append(ticket)
            continue

        exit_price = float(ev.get("exit_price") or ev.get("exit") or 0)
        pnl = float(ev.get("pnl") or ev.get("mt5_pnl") or 0)
        r = float(ev.get("r_multiple") or 0)
        exit_time = ev.get("timestamp", "")
        reason = ev.get("reason", "CLOSE")
        sym_norm = normalize_symbol(ev.get("symbol") or row["symbol"])

        print(
            f"  #{ticket:<12} {sym_norm:10} exit={exit_price:<10.4f} "
            f"pnl={pnl:+8.2f} R={r:+.1f} {reason}"
        )

        if args.apply:
            cur.execute(
                "UPDATE trades SET exit_price = ?, exit_time = ?, "
                "pnl_usd = ?, r_multiple = ?, status = 'closed', "
                "symbol = ? WHERE ticket = ?",
                (exit_price, exit_time, pnl, r, sym_norm, ticket),
            )
            updated += 1

    # Also normalize symbols on all rows (open or closed)
    if args.apply:
        cur.execute("SELECT id, symbol FROM trades")
        for r in cur.fetchall():
            norm = normalize_symbol(r["symbol"])
            if norm and norm != r["symbol"]:
                cur.execute("UPDATE trades SET symbol = ? WHERE id = ?",
                            (norm, r["id"]))
        con.commit()

    print(f"\nMatched: {updated}  Unmatched: {len(unmatched)}")
    if unmatched:
        print(f"  Unmatched tickets (no CLOSE event found): {unmatched}")
    if not args.apply:
        print("\n[DRY RUN] re-run with --apply to write changes")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
