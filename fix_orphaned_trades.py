"""
Fix orphaned trades in the trading ledger.
- Real MT5 tickets: query deal history for actual exit data
- Paper tickets (100002, 100003): mark as closed_orphan
"""
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = r"C:\Users\User\.tradingview-mcp\trading_ledger.db"

PAPER_TICKETS = [100002, 100003]
MT5_TICKETS = [425251661, 425257411, 425415101, 426189879, 427148826, 427173314, 427173316]

def get_trade_info(conn, ticket):
    cur = conn.cursor()
    cur.execute("SELECT * FROM trades WHERE ticket=?", (ticket,))
    row = cur.fetchone()
    return dict(zip([d[0] for d in cur.description], row)) if row else None

def compute_r_multiple(trade, exit_price):
    """R = PnL-direction / risk-per-unit. Positive = good."""
    entry = trade['entry_price']
    sl = trade['sl_price']
    direction = trade['direction']
    risk = abs(entry - sl)
    if risk == 0:
        return 0.0
    if direction == 'BUY':
        r = (exit_price - entry) / risk
    else:
        r = (entry - exit_price) / risk
    return round(r, 3)

def close_paper_tickets(conn):
    print("=" * 60)
    print("PAPER TICKETS - marking as closed_orphan")
    print("=" * 60)
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    for ticket in PAPER_TICKETS:
        trade = get_trade_info(conn, ticket)
        if not trade:
            print(f"  Ticket #{ticket}: NOT FOUND in ledger, skipping")
            continue
        if trade['status'] != 'open':
            print(f"  Ticket #{ticket}: already status={trade['status']}, skipping")
            continue
        cur.execute("""
            UPDATE trades SET status='closed_orphan', exit_time=?, pnl_usd=0, r_multiple=0
            WHERE ticket=?
        """, (now, ticket))
        print(f"  Ticket #{ticket} {trade['direction']} {trade['symbol']} @ {trade['entry_price']} "
              f"({trade['lot_size']} lots) -> closed_orphan")
    conn.commit()

def close_mt5_tickets(conn):
    print()
    print("=" * 60)
    print("MT5 TICKETS - querying deal history")
    print("=" * 60)

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("ERROR: MetaTrader5 package not installed. Cannot query MT5.")
        return False

    if not mt5.initialize():
        print(f"ERROR: mt5.initialize() failed: {mt5.last_error()}")
        return False

    print(f"  MT5 connected: {mt5.terminal_info().company}")
    print()

    cur = conn.cursor()
    updated = 0
    failed = 0

    for ticket in MT5_TICKETS:
        trade = get_trade_info(conn, ticket)
        if not trade:
            print(f"  Ticket #{ticket}: NOT FOUND in ledger, skipping")
            continue
        if trade['status'] != 'open':
            print(f"  Ticket #{ticket}: already status={trade['status']}, skipping")
            continue

        # Query deal history for this position
        deals = mt5.history_deals_get(position=ticket)
        if deals is None or len(deals) == 0:
            print(f"  Ticket #{ticket} {trade['direction']} {trade['symbol']}: "
                  f"NO DEALS FOUND in MT5 history")
            # Mark as closed_orphan since position doesn't exist broker-side
            now = datetime.now(timezone.utc).isoformat()
            cur.execute("""
                UPDATE trades SET status='closed_orphan', exit_time=?
                WHERE ticket=?
            """, (now, ticket))
            print(f"    -> marked closed_orphan (no MT5 history)")
            failed += 1
            continue

        # Find the close deal (DEAL_ENTRY_OUT = 1)
        close_deal = None
        total_pnl = 0.0
        total_commission = 0.0
        total_swap = 0.0
        for d in deals:
            # entry: 0=IN, 1=OUT, 2=INOUT, 3=OUT_BY
            if d.entry == 1 or d.entry == 3:  # OUT or OUT_BY
                close_deal = d
            total_commission += d.commission
            total_swap += d.swap

        if close_deal is None:
            print(f"  Ticket #{ticket} {trade['direction']} {trade['symbol']}: "
                  f"found {len(deals)} deals but no close deal")
            now = datetime.now(timezone.utc).isoformat()
            cur.execute("""
                UPDATE trades SET status='closed_orphan', exit_time=?
                WHERE ticket=?
            """, (now, ticket))
            print(f"    -> marked closed_orphan (no close deal found)")
            failed += 1
            continue

        exit_price = close_deal.price
        pnl = close_deal.profit  # This is the realized P&L from MT5
        exit_time_dt = datetime.fromtimestamp(close_deal.time, tz=timezone.utc)
        exit_time = exit_time_dt.isoformat()
        r_multiple = compute_r_multiple(trade, exit_price)

        cur.execute("""
            UPDATE trades
            SET status='closed', exit_price=?, pnl_usd=?, exit_time=?, r_multiple=?,
                commission=?, swap=?
            WHERE ticket=?
        """, (exit_price, pnl, exit_time, r_multiple, total_commission, total_swap, ticket))

        arrow = "+" if pnl >= 0 else ""
        print(f"  Ticket #{ticket} {trade['direction']} {trade['symbol']}:")
        print(f"    Entry: {trade['entry_price']} -> Exit: {exit_price}")
        print(f"    P&L: {arrow}{pnl:.2f} USD | R: {r_multiple:.2f}R")
        print(f"    Commission: {total_commission:.2f} | Swap: {total_swap:.2f}")
        print(f"    Closed at: {exit_time}")
        updated += 1

    conn.commit()
    mt5.shutdown()
    return updated, failed

def main():
    conn = sqlite3.connect(DB_PATH)

    # Step 1: Paper tickets
    close_paper_tickets(conn)

    # Step 2: MT5 tickets
    result = close_mt5_tickets(conn)

    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    cur = conn.cursor()
    cur.execute("SELECT status, COUNT(*) FROM trades GROUP BY status")
    for row in cur.fetchall():
        print(f"  {row[0]}: {row[1]} trades")

    cur.execute("SELECT COUNT(*) FROM trades WHERE status='open'")
    remaining = cur.fetchone()[0]
    print(f"\n  Remaining open positions: {remaining}")

    conn.close()
    print("\nDone.")

if __name__ == "__main__":
    main()
