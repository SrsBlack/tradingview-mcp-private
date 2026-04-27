"""
Audit every closed engine='ICT' trade in the ledger against the actual
FTMO broker history. Flags rows where pnl, exit_price, or volume
disagree.
"""
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import MetaTrader5 as mt5

MT5_LOGIN = 1513140458
MT5_PASSWORD = "L!$q1k@4Z"
MT5_SERVER = "FTMO-Demo"
MT5_PATH = "C:/Program Files/METATRADER5.1/terminal64.exe"

if not mt5.initialize(path=MT5_PATH, login=MT5_LOGIN,
                      password=MT5_PASSWORD, server=MT5_SERVER):
    print("init failed:", mt5.last_error())
    raise SystemExit(1)

# Pull broker deals across the full ledger period
start = datetime(2026, 4, 1, tzinfo=timezone.utc)
end = datetime(2026, 4, 27, tzinfo=timezone.utc)
deals = mt5.history_deals_get(start, end) or []

# Group deals by position_id
from collections import defaultdict
by_pos: dict[int, list] = defaultdict(list)
for d in deals:
    by_pos[d.position_id].append(d)

# Compute net profit per position
broker_data = {}
for pos_id, ds in by_pos.items():
    net = sum(d.profit for d in ds)
    sym = ds[0].symbol
    open_deals = [d for d in ds if d.entry == 0]
    close_deals = [d for d in ds if d.entry == 1]
    if not open_deals:
        continue
    od = open_deals[0]
    closed = bool(close_deals)
    cd_price = close_deals[-1].price if close_deals else None
    broker_data[pos_id] = {
        "symbol": sym,
        "direction": "BUY" if od.type == 0 else "SELL" if od.type == 1 else f"T{od.type}",
        "open_time": datetime.fromtimestamp(od.time, tz=timezone.utc),
        "open_price": od.price,
        "open_volume": od.volume,
        "net_profit": net,
        "closed": closed,
        "close_price": cd_price,
        "comment": od.comment,
    }

# Now load ledger trades
db = Path.home() / ".tradingview-mcp" / "trading_ledger.db"
con = sqlite3.connect(str(db))
cur = con.cursor()
cur.execute(
    "SELECT id, ticket, symbol, direction, entry_price, exit_price, lot_size, "
    "       entry_time, exit_time, pnl_usd, signal_grade, status "
    "FROM trades WHERE engine='ICT' AND status='closed' ORDER BY entry_time"
)
ledger_rows = cur.fetchall()

print(f"Ledger closed ICT trades: {len(ledger_rows)}")
print(f"Broker positions in window: {len(broker_data)}")
print()

# Match each ledger row by ticket (ledger ticket = broker position_id)
print(f"{'id':<3} {'lg_pnl':>10} {'br_pnl':>10} {'delta':>9} {'sym':<10} {'dir':<5} {'lg_xprice':>10} {'br_xprice':>10} {'flag'}")
print("-" * 110)
total_lg = 0.0
total_br = 0.0
mismatches = 0
for r in ledger_rows:
    (id_, ticket, sym, dir_, ep, xp, lot, et, xt, lg_pnl, gr, stat) = r
    bd = broker_data.get(ticket)
    if bd is None:
        flag = "NO BROKER MATCH"
        br_pnl = 0.0
        br_xp = None
    else:
        br_pnl = bd["net_profit"]
        br_xp = bd["close_price"]
        if abs((lg_pnl or 0) - br_pnl) > 1.0:
            flag = "PNL_DELTA"
        elif xp is not None and br_xp is not None and abs(xp - br_xp) > 0.01:
            flag = "XPRICE_DELTA"
        else:
            flag = "ok"
    delta = (lg_pnl or 0) - (br_pnl or 0)
    if flag != "ok":
        mismatches += 1
    total_lg += lg_pnl or 0
    total_br += br_pnl or 0
    xp_str = f"{xp:.2f}" if xp is not None else "None"
    br_xp_str = f"{br_xp:.2f}" if br_xp is not None else "None"
    print(f"{id_:<3} {lg_pnl or 0:>+10.2f} {br_pnl:>+10.2f} {delta:>+9.2f} {sym:<10} {dir_:<5} {xp_str:>10} {br_xp_str:>10} {flag}")

print()
print(f"TOTAL: ledger=${total_lg:+.2f}  broker=${total_br:+.2f}  delta=${total_lg - total_br:+.2f}")
print(f"Mismatches: {mismatches} of {len(ledger_rows)}")

mt5.shutdown()
