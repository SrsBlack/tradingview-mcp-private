"""Win-rate by signal class — what kinds of trade signals actually win?

Two-tier report:

Tier 1 (broad — 50 trades): joins broker-truth P&L with the ledger
        (trading_ledger.db) which has signal_score + signal_grade for
        every ICT_Bridge trade. Reports WR by Grade, score band, side,
        and symbol class. Big-N stats.

Tier 2 (deep — 5+ trades): adds log-derived features (MTF conflict,
        HTF rejection, IPDA position, kill-zone, sweep, displacement)
        for trades whose entry context is still in the current
        trading.log. Smaller N but richer features.

Usage:
  PYTHONUTF8=1 python scripts/bench_wr_by_class.py
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5

LEDGER = Path("C:/Users/User/.tradingview-mcp/trading_ledger.db")
LOG = Path(__file__).resolve().parent.parent / "logs" / "trading.log"

MT5_LOGIN = 1513140458
MT5_PASSWORD = "L!$q1k@4Z"
MT5_SERVER = "FTMO-Demo"
MT5_PATH = "C:/Program Files/METATRADER5.1/terminal64.exe"

SYMBOL_CLASS_MAP = {
    "BTCUSD": "crypto", "ETHUSD": "crypto", "SOLUSD": "crypto", "DOGEUSD": "crypto",
    "EURUSD": "fx", "GBPUSD": "fx", "USDJPY": "fx", "EURJPY": "fx",
    "GBPJPY": "fx", "USDCAD": "fx", "AUDUSD": "fx", "NZDUSD": "fx",
    "XAUUSD": "metal", "XAGUSD": "metal", "XCUUSD": "metal",
    "US500": "index", "US500.cash": "index", "US100": "index",
    "US100.cash": "index", "GER40": "index", "GER40.cash": "index", "YM1!": "index",
    "UKOIL": "oil", "UKOIL.cash": "oil",
}


def symbol_class(sym: str) -> str:
    base = sym.split(":")[-1]
    return SYMBOL_CLASS_MAP.get(base, "other")


def score_band(score: float | None) -> str:
    if score is None:
        return "?"
    if score < 70:
        return "<70"
    if score < 85:
        return "70-84"
    if score < 95:
        return "85-94"
    return "95+"


# ---------------------------------------------------------------------------
# Tier 1 sources — ledger + broker
# ---------------------------------------------------------------------------

def load_ledger() -> dict[int, dict]:
    """ticket -> ledger row dict."""
    conn = sqlite3.connect(LEDGER)
    cur = conn.cursor()
    cur.execute("""
        SELECT ticket, symbol, direction, strategy_name, entry_time,
               signal_score, signal_grade, session, regime
        FROM trades
        WHERE strategy_name = 'ICT_Bridge'
    """)
    out: dict[int, dict] = {}
    for r in cur.fetchall():
        ticket = int(r[0])
        out[ticket] = {
            "ticket": ticket, "symbol": r[1], "direction": r[2],
            "entry_time_str": r[4],
            "signal_score": r[5], "signal_grade": r[6],
            "session": r[7], "regime": r[8],
        }
    conn.close()
    return out


def load_broker_trades(start: datetime, end: datetime) -> list[dict]:
    if not mt5.initialize(path=MT5_PATH, login=MT5_LOGIN,
                          password=MT5_PASSWORD, server=MT5_SERVER):
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    try:
        deals = mt5.history_deals_get(start, end) or []
        positions = mt5.positions_get() or []
    finally:
        mt5.shutdown()
    by_pos: dict[int, list] = defaultdict(list)
    for d in deals:
        by_pos[d.position_id].append(d)
    out: list[dict] = []
    for pid, ds in by_pos.items():
        opens = [d for d in ds if d.entry == 0]
        closes = [d for d in ds if d.entry == 1]
        if not opens:
            continue
        od = opens[0]
        if "ICT_Bridge" not in (od.comment or ""):
            continue
        side = "BUY" if od.type == 0 else "SELL"
        if closes:
            net_pnl = sum(d.profit for d in ds)
            status = "closed"
        else:
            net_pnl = 0.0
            status = "open"
        out.append({
            "position_id": pid,
            "symbol": od.symbol,
            "side": side,
            "entry_price": od.price,
            "entry_time": datetime.fromtimestamp(od.time, tz=timezone.utc),
            "exit_price": closes[-1].price if closes else None,
            "exit_time": datetime.fromtimestamp(closes[-1].time, tz=timezone.utc)
                          if closes else None,
            "pnl_usd": net_pnl,
            "status": status,
        })
    # Add still-open positions (broker truth not in deals yet)
    seen = {t["position_id"] for t in out}
    for p in positions:
        if p.ticket in seen:
            continue
        if "ICT_Bridge" not in (p.comment or ""):
            continue
        out.append({
            "position_id": p.ticket,
            "symbol": p.symbol,
            "side": "BUY" if p.type == 0 else "SELL",
            "entry_price": p.price_open,
            "entry_time": datetime.fromtimestamp(p.time, tz=timezone.utc),
            "exit_price": None, "exit_time": None,
            "pnl_usd": p.profit, "status": "live_floating",
        })
    out.sort(key=lambda t: t["entry_time"])
    return out


# ---------------------------------------------------------------------------
# Tier 2 source — log entry context
# ---------------------------------------------------------------------------

GRADE_RX = re.compile(
    r"\[([A-Z0-9_:.\!]+)\]\s+Grade\s+([A-D])\s+\((\d+)/100\)\s+(BULLISH|BEARISH|NEUTRAL)"
    r"\s+\|\s+(\d+)\s+confluence\s+\|\s+struct=(\d+)\s+ob=(\d+)\s+fvg=(\d+)"
    r"\s+sess=(\d+)\s+ote=(\d+)\s+smt=(\d+)\s+sweep=([YN])\s+disp=([YN])"
    r"\s+pd=(\w+)(?:\(([^)]+)\))?\s+kz=([YN])"
)
DECISION_RX = re.compile(
    r"\[([A-Z0-9_:.\!]+)\]\s+Decision:\s+(BUY|SELL|SKIP)\s+\(confidence=(\d+),\s+model=([^)]+)\)"
)
REASON_RX = re.compile(r"\[([A-Z0-9_:.\!]+)\]\s+Reason:\s+(.*)")
OPENED_RX = re.compile(
    r"\[([A-Z0-9_:.\!]+)\]\s+OPENED:\s+(BUY|SELL)\s+(\S+)\s+@\s+([\d.]+)\s+Lots=([\d.]+)"
)


def parse_log_entries(path: Path) -> list[dict]:
    """List of dicts, one per OPENED line, containing the most recent
    Grade + Decision + Reason for that symbol."""
    if not path.exists():
        return []
    out: list[dict] = []
    state: dict[str, dict] = {}
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = GRADE_RX.search(line)
            if m:
                sym = m.group(1)
                state[sym] = {
                    "symbol": sym,
                    "original_grade": m.group(2),
                    "score": int(m.group(3)),
                    "direction_analysis": m.group(4),
                    "confluence": int(m.group(5)),
                    "struct": int(m.group(6)),
                    "ob": int(m.group(7)),
                    "fvg": int(m.group(8)),
                    "sess_score": int(m.group(9)),
                    "ote": int(m.group(10)),
                    "smt": int(m.group(11)),
                    "sweep": m.group(12),
                    "disp": m.group(13),
                    "pd_state": m.group(14),
                    "pd_alignment": m.group(15) or "",
                    "kill_zone": m.group(16),
                    "decision": None, "confidence": None, "model": None, "reason": "",
                }
                continue
            m = DECISION_RX.search(line)
            if m and m.group(1) in state:
                state[m.group(1)]["decision"] = m.group(2)
                state[m.group(1)]["confidence"] = int(m.group(3))
                state[m.group(1)]["model"] = m.group(4).strip()
                continue
            m = REASON_RX.search(line)
            if m and m.group(1) in state:
                state[m.group(1)]["reason"] = m.group(2)
                continue
            m = OPENED_RX.search(line)
            if m:
                sym = m.group(1)
                if sym not in state:
                    continue
                d = dict(state[sym])
                d["entry_price"] = float(m.group(4))
                d["lots"] = float(m.group(5))
                d["side"] = m.group(2)
                out.append(d)
    return out


def htf_rej_class(reason: str, side: str) -> str:
    r = reason.lower()
    if "htf_rej" not in r and "htf rejection" not in r:
        return "NONE"
    bull_rej = bool(re.search(r"htf_rej[_a-z0-9]*bullish|htf rejection.*bullish", r))
    bear_rej = bool(re.search(r"htf_rej[_a-z0-9]*bearish|htf rejection.*bearish", r))
    trade_dir = "bullish" if side == "BUY" else "bearish"
    if bull_rej and bear_rej:
        return "MIXED"
    if (trade_dir == "bullish" and bull_rej) or (trade_dir == "bearish" and bear_rej):
        return "SAME"
    if bull_rej or bear_rej:
        return "OPPOSING"
    return "NONE"


def confidence_band(c: int | None) -> str:
    if c is None:
        return "?"
    if c < 60:
        return "<60"
    if c < 70:
        return "60-69"
    if c < 80:
        return "70-79"
    return "80+"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def report_by_dim(trades: list[dict], dim_key: str, label: str) -> None:
    by: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        v = t.get(dim_key)
        if v is None or v == "?":
            v = "<unknown>"
        by[str(v)].append(t)
    print(f"\n=== WR by {label} ({dim_key}) ===")
    print(f"  {'class':<22} {'N':>4} {'WR%':>6} {'wins':>5} {'losses':>6} "
          f"{'avg_pnl':>9} {'tot_pnl':>10}")
    rows = []
    for k in sorted(by.keys()):
        bucket = by[k]
        # Only count CLOSED trades for WR (open positions don't have a final P&L yet)
        closed = [t for t in bucket if t.get("status") == "closed"]
        n_closed = len(closed)
        n_total = len(bucket)
        if n_closed == 0:
            wins = losses = 0
            wr = 0
            tot = 0
            avg = 0
        else:
            wins = sum(1 for t in closed if t["pnl_usd"] > 0)
            losses = n_closed - wins
            wr = wins / n_closed * 100
            tot = sum(t["pnl_usd"] for t in closed)
            avg = tot / n_closed
        rows.append((k, n_closed, n_total, wr, wins, losses, avg, tot))
    rows.sort(key=lambda r: -r[1])
    for k, nc, nt, wr, w, l, avg, tot in rows:
        marker = " *" if nc >= 5 else "  "
        suffix = f"  ({nt-nc} open)" if nt > nc else ""
        print(f"  {k:<22} {nc:>4} {wr:>5.1f}% {w:>5} {l:>6} "
              f"{avg:>+9.2f} {tot:>+10.2f}{marker}{suffix}")
    print("  (* = N>=5 closed, statistically meaningful)")


def main() -> int:
    print("=" * 80)
    print("WR by signal class — broker-truth + ledger join (Tier 1) + log (Tier 2)")
    print("=" * 80)

    print("\nLoading ledger...")
    ledger = load_ledger()
    print(f"  ICT_Bridge rows in ledger: {len(ledger)}")

    print("\nLoading broker history...")
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    trades = load_broker_trades(start, end)
    print(f"  Broker positions: {len(trades)}  "
          f"(closed: {sum(1 for t in trades if t['status'] == 'closed')}, "
          f"open: {sum(1 for t in trades if t['status'] != 'closed')})")

    # ============== Tier 1: ledger join ==============
    print("\nJoining broker -> ledger by ticket...")
    tier1: list[dict] = []
    for t in trades:
        ticket = t["position_id"]
        led = ledger.get(ticket)
        if led is None:
            continue
        joined = dict(t)
        joined["signal_score"] = led.get("signal_score")
        joined["signal_grade"] = led.get("signal_grade")
        joined["score_band"] = score_band(led.get("signal_score"))
        joined["symbol_class"] = symbol_class(t["symbol"])
        joined["is_winner"] = t["pnl_usd"] > 0
        tier1.append(joined)
    print(f"  Joined: {len(tier1)} (Tier 1)")

    # Closed only for headline
    closed = [t for t in tier1 if t["status"] == "closed"]
    if closed:
        wins = sum(1 for t in closed if t["is_winner"])
        n = len(closed)
        tot = sum(t["pnl_usd"] for t in closed)
        print(f"\n  Tier 1 closed trades: {n}  WR={wins/n*100:.1f}%  total=${tot:+.2f}")

    # Tier 1 reports
    print("\n" + "=" * 80)
    print("TIER 1 — full broker history (50 trades)")
    print("=" * 80)
    report_by_dim(tier1, "side", "trade direction")
    report_by_dim(tier1, "signal_grade", "Grade (ledger)")
    report_by_dim(tier1, "score_band", "Score band")
    report_by_dim(tier1, "symbol_class", "Symbol class")
    report_by_dim(tier1, "symbol", "Symbol")

    # ============== Tier 2: log join (rich features for trades still in current log) ==============
    print("\n" + "=" * 80)
    print("TIER 2 — current log only (smaller N, richer features)")
    print("=" * 80)
    log_entries = parse_log_entries(LOG)
    print(f"\nLog OPENED entries parseable: {len(log_entries)}")

    # Match by (entry_price within 0.001%, side, time within 10 min)
    tier2: list[dict] = []
    for trade in trades:
        for e in log_entries:
            if e["side"] != trade["side"]:
                continue
            if e["symbol"].split(":")[-1] != trade["symbol"].split(".")[0]:
                # Symbol mapping: COINBASE:SOLUSD -> SOLUSD; UKOIL.cash matched as TVC:UKOIL
                if not (
                    trade["symbol"].split(".")[0] in e["symbol"]
                    or e["symbol"].split(":")[-1] in trade["symbol"]
                ):
                    continue
            # Price match within 0.5%
            if abs(e["entry_price"] - trade["entry_price"]) / trade["entry_price"] > 0.005:
                continue
            joined = dict(trade)
            joined.update(e)
            joined["confidence_band"] = confidence_band(e.get("confidence"))
            joined["htf_rej_class"] = htf_rej_class(e.get("reason", ""), trade["side"])
            joined["mtf_conflict"] = "Y" if "mtf conflict" in e.get("reason", "").lower() else "N"
            joined["symbol_class"] = symbol_class(trade["symbol"])
            joined["is_winner"] = trade["pnl_usd"] > 0
            tier2.append(joined)
            break

    print(f"  Joined to log entries: {len(tier2)} (Tier 2)")
    if not tier2:
        print("  No tier-2 matches.")
        return 0

    # Tier 2 reports
    report_by_dim(tier2, "side", "trade direction")
    report_by_dim(tier2, "original_grade", "Grade (raw log)")
    report_by_dim(tier2, "pd_alignment", "PD alignment")
    report_by_dim(tier2, "kill_zone", "Kill zone")
    report_by_dim(tier2, "sweep", "Sweep")
    report_by_dim(tier2, "disp", "Displacement")
    report_by_dim(tier2, "mtf_conflict", "MTF conflict in reason")
    report_by_dim(tier2, "htf_rej_class", "HTF rejection direction")
    report_by_dim(tier2, "confidence_band", "Confidence band")
    report_by_dim(tier2, "model", "Model")

    print("\n" + "=" * 80)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
