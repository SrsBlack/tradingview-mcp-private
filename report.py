"""Unified trading system report.

Reads from three canonical sources and joins them:
  1. ~/.tradingview-mcp/trading_ledger.db        — authoritative live trade ledger
  2. ~/.tradingview-mcp/sessions/YYYY-MM-DD.json — analyses, decisions, session trade events
  3. logs/paper_trades_*.jsonl                   — paper shadow events

Symbols are normalized at read time to dedupe BITSTAMP:BTCUSD vs BTCUSD etc.
Date range is dynamic (auto-detected from available session files).

Usage:
    python report.py               # full report, all dates
    python report.py --days 3      # last 3 days only
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Force UTF-8 stdout on Windows so box-drawing chars don't crash cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, Exception):  # pragma: no cover
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge.symbol_utils import normalize_symbol  # noqa: E402

HOME = Path.home()
LEDGER_PATH = HOME / ".tradingview-mcp" / "trading_ledger.db"
SESSIONS_DIR = HOME / ".tradingview-mcp" / "sessions"
PAPER_LOG_DIR = Path(__file__).resolve().parent / "logs"

# Bridge identifies its MT5 trades via this comment substring.
# All other EAs on the same account use different comments (TFPR_*, ME_*, etc.).
BRIDGE_COMMENT_TAG = "ICT_Bridge"

W = 65
LINE = chr(0x2500) * W


def header(title: str) -> None:
    print(f"\n{LINE}\n  {title}\n{LINE}")


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------

def load_sessions(since: datetime | None) -> tuple[list, list, list]:
    """Return (analyses, decisions, session_trades) across all dates >= since."""
    analyses, decisions, trades = [], [], []
    files = sorted(SESSIONS_DIR.glob("2026-*.json"))
    for f in files:
        if ".backup" in f.name or ".corrupt" in f.name:
            continue
        try:
            date = datetime.strptime(f.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if since and date < since:
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        for a in data.get("analyses", []):
            a["_date"] = f.stem
            analyses.append(a)
        for d in data.get("decisions", []):
            d["_date"] = f.stem
            decisions.append(d)
        for t in data.get("trades", []):
            t["_date"] = f.stem
            trades.append(t)
    return analyses, decisions, trades


def load_mt5_bridge_trades(days: int = 30) -> dict | None:
    """Pull bridge-only trades from MT5 directly (source of truth).

    Filters by comment containing BRIDGE_COMMENT_TAG. Returns None if
    MT5 module/terminal is unavailable.
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return None
    if not mt5.initialize():
        return None
    try:
        acct = mt5.account_info()
        account = {
            "login": acct.login,
            "balance": acct.balance,
            "equity": acct.equity,
            "floating_pnl": acct.profit,
            "currency": acct.currency,
        } if acct else {}

        # Open bridge positions
        open_all = mt5.positions_get() or []
        open_bridge = [
            {
                "ticket": p.ticket,
                "symbol": normalize_symbol(p.symbol),
                "direction": "BUY" if p.type == 0 else "SELL",
                "volume": p.volume,
                "entry_price": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "current_price": p.price_current,
                "profit": p.profit,
                "swap": p.swap,
                "comment": p.comment,
                "time": datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
            }
            for p in open_all
            if BRIDGE_COMMENT_TAG in (p.comment or "")
        ]

        # Closed round-trips from deal history.
        # Pad the upper bound by 12h — MT5 broker server times can run ahead
        # of UTC (common brokers: UTC+2/+3), and deals timestamped "in the future"
        # relative to our local clock would otherwise be silently dropped.
        now = datetime.now(timezone.utc) + timedelta(hours=12)
        deals = mt5.history_deals_get(now - timedelta(days=days + 1), now) or []

        by_position: dict[int, list] = defaultdict(list)
        for d in deals:
            by_position[d.position_id].append(d)

        closed: list[dict] = []
        for pid, ds in by_position.items():
            entry_deals = [d for d in ds if d.entry == 0]
            exit_deals = [d for d in ds if d.entry == 1]
            if not entry_deals or not exit_deals:
                continue
            entry = entry_deals[0]
            # Identify as bridge trade by the opening deal's comment
            if BRIDGE_COMMENT_TAG not in (entry.comment or ""):
                continue
            last_exit = exit_deals[-1]
            total_profit = sum(d.profit for d in exit_deals)
            total_comm = sum(d.commission for d in ds)
            total_swap = sum(d.swap for d in ds)
            net = total_profit + total_comm + total_swap
            reason = "TP" if "tp" in (last_exit.comment or "").lower() else \
                     "SL" if "sl" in (last_exit.comment or "").lower() else "CLOSE"
            closed.append({
                "position_id": pid,
                "symbol": normalize_symbol(entry.symbol),
                "direction": "BUY" if entry.type == 0 else "SELL",
                "volume": entry.volume,
                "entry_price": entry.price,
                "exit_price": last_exit.price,
                "entry_time": datetime.fromtimestamp(entry.time, tz=timezone.utc).isoformat(),
                "exit_time": datetime.fromtimestamp(last_exit.time, tz=timezone.utc).isoformat(),
                "gross_pnl": total_profit,
                "commission": total_comm,
                "swap": total_swap,
                "net_pnl": net,
                "reason": reason,
            })
        closed.sort(key=lambda x: x["exit_time"])
        return {"account": account, "open": open_bridge, "closed": closed}
    finally:
        mt5.shutdown()


def load_ledger() -> list[dict]:
    """Load all ledger rows."""
    if not LEDGER_PATH.exists():
        return []
    con = sqlite3.connect(str(LEDGER_PATH))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        "SELECT ticket, symbol, direction, entry_price, exit_price, "
        "lot_size, sl_price, tp_price, entry_time, exit_time, "
        "pnl_usd, r_multiple, status, signal_grade, signal_score "
        "FROM trades ORDER BY entry_time"
    )
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    for r in rows:
        r["symbol"] = normalize_symbol(r.get("symbol") or "")
    return rows


def load_paper_trades(since: datetime | None) -> list[dict]:
    """Load all events from paper_trades_*.jsonl files."""
    events: list[dict] = []
    for f in sorted(PAPER_LOG_DIR.glob("paper_trades_*.jsonl")):
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if since:
                        ts = ev.get("timestamp", "")
                        try:
                            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if t < since:
                                continue
                        except ValueError:
                            pass
                    ev["symbol"] = normalize_symbol(ev.get("symbol") or "")
                    events.append(ev)
        except Exception:
            continue
    return events


# --------------------------------------------------------------------------
# Report sections
# --------------------------------------------------------------------------

def section_analysis(analyses: list[dict]) -> None:
    header("ANALYSIS ENGINE")
    print(f"  Total analyses: {len(analyses)}")
    grades: dict[str, int] = defaultdict(int)
    dirs: dict[str, int] = defaultdict(int)
    for a in analyses:
        grades[a.get("grade", "?")] += 1
        dirs[a.get("direction", "?")] += 1
    for g in ["A", "B", "C", "D", "INVALID"]:
        if g in grades:
            pct = grades[g] / max(len(analyses), 1) * 100
            print(f"    Grade {g:8}: {grades[g]:5} ({pct:.1f}%)")
    print("\n  Direction calls:")
    for d, c in sorted(dirs.items(), key=lambda x: -x[1]):
        print(f"    {d}: {c}")


def section_decisions(decisions: list[dict]) -> None:
    header("DECISION ENGINE")
    print(f"  Total decisions: {len(decisions)}")
    actions: dict[str, int] = defaultdict(int)
    models: dict[str, int] = defaultdict(int)
    for d in decisions:
        actions[d.get("action", "?")] += 1
        m = d.get("model_used", "?")
        if "haiku" in m:
            m = "haiku"
        elif "sonnet" in m:
            m = "sonnet"
        models[m] += 1
    for a, c in sorted(actions.items(), key=lambda x: -x[1]):
        print(f"    {a}: {c}")
    print("\n  Model routing:")
    for m, c in sorted(models.items(), key=lambda x: -x[1]):
        print(f"    {m}: {c}")


def section_live_mt5(data: dict) -> None:
    header("LIVE TRADES — MT5 (bridge only, filtered by comment)")
    acct = data.get("account", {})
    if acct:
        print(f"  Account #{acct.get('login')}  Currency: {acct.get('currency')}")
        print(f"  Balance: ${acct.get('balance', 0):,.2f}   "
              f"Equity: ${acct.get('equity', 0):,.2f}")
        print(f"  (Account-wide floating ${acct.get('floating_pnl', 0):+,.2f} "
              f"includes ALL EAs — see bridge-only floating below.)")

    opens = data.get("open", [])
    closed = data.get("closed", [])
    bridge_floating = sum(p["profit"] + p["swap"] for p in opens)
    print(f"\n  Bridge open positions:  {len(opens)}  (bridge-only floating: ${bridge_floating:+,.2f})")
    print(f"  Bridge closed trades:   {len(closed)}")

    if closed:
        net_sum = sum(c["net_pnl"] for c in closed)
        gross = sum(c["gross_pnl"] for c in closed)
        comm = sum(c["commission"] for c in closed)
        swap = sum(c["swap"] for c in closed)
        wins = [c for c in closed if c["net_pnl"] > 0]
        losses = [c for c in closed if c["net_pnl"] <= 0]
        wr = len(wins) / len(closed) * 100
        avg_win = sum(c["net_pnl"] for c in wins) / max(len(wins), 1)
        avg_loss = sum(c["net_pnl"] for c in losses) / max(len(losses), 1)
        pf = abs(avg_win * len(wins) / (avg_loss * len(losses))) if avg_loss and losses else 0
        print(f"  Win rate:     {wr:.0f}% ({len(wins)}W / {len(losses)}L)")
        print(f"  Gross P&L:    ${gross:+,.2f}  Comm: ${comm:+,.2f}  Swap: ${swap:+,.2f}")
        print(f"  NET P&L:      ${net_sum:+,.2f}")
        print(f"  Avg win: ${avg_win:+,.2f}  Avg loss: ${avg_loss:+,.2f}  PF: {pf:.2f}")

        by_sym: dict[str, dict] = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
        for c in closed:
            s = c["symbol"]
            by_sym[s]["pnl"] += c["net_pnl"]
            if c["net_pnl"] > 0:
                by_sym[s]["w"] += 1
            else:
                by_sym[s]["l"] += 1
        print("\n  By symbol:")
        for s, d in sorted(by_sym.items(), key=lambda x: -x[1]["pnl"]):
            print(f"    {s:10} {d['w']}W/{d['l']}L  Net=${d['pnl']:+,.2f}")

        print("\n  Trade log:")
        for c in closed:
            ts = c["exit_time"][:16]
            print(
                f"    {ts} {c['symbol']:10} {c['direction']:4} "
                f"entry={c['entry_price']:.4f} exit={c['exit_price']:.4f} "
                f"${c['net_pnl']:+,.2f} {c['reason']}"
            )

    if opens:
        floating = sum(p["profit"] + p["swap"] for p in opens)
        print(f"\n  OPEN POSITIONS  (floating net: ${floating:+,.2f}):")
        for p in opens:
            ts = p["time"][:16]
            print(
                f"    {ts} {p['symbol']:10} {p['direction']:4} "
                f"vol={p['volume']:.2f} entry={p['entry_price']:.4f} "
                f"current={p['current_price']:.4f} "
                f"P&L=${p['profit']+p['swap']:+,.2f}  #{p['ticket']}"
            )


def section_live(ledger: list[dict]) -> None:
    header("LIVE TRADES (legacy ledger DB — fallback)")
    closed = [r for r in ledger if r["status"] == "closed"]
    opens = [r for r in ledger if r["status"] == "open"]

    print(f"  Open positions:  {len(opens)}")
    print(f"  Closed trades:   {len(closed)}")

    if closed:
        wins = [r for r in closed if (r.get("pnl_usd") or 0) > 0]
        losses = [r for r in closed if (r.get("pnl_usd") or 0) <= 0]
        pnl = sum(r.get("pnl_usd") or 0 for r in closed)
        rmult = sum(r.get("r_multiple") or 0 for r in closed)
        wr = len(wins) / len(closed) * 100
        avg_win = sum(r["pnl_usd"] for r in wins) / max(len(wins), 1)
        avg_loss = sum(r["pnl_usd"] for r in losses) / max(len(losses), 1)
        pf = abs(avg_win * len(wins) / (avg_loss * len(losses))) if avg_loss and losses else 0
        print(f"  Win rate:        {wr:.0f}% ({len(wins)}W / {len(losses)}L)")
        print(f"  Total P&L:       ${pnl:+,.2f}")
        print(f"  Total R:         {rmult:+.1f}R")
        print(f"  Avg win: ${avg_win:+,.2f}  Avg loss: ${avg_loss:+,.2f}  PF: {pf:.2f}")

        by_sym: dict[str, dict] = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0, "r": 0.0})
        for r in closed:
            s = r["symbol"]
            pnl_r = r.get("pnl_usd") or 0
            r_r = r.get("r_multiple") or 0
            by_sym[s]["pnl"] += pnl_r
            by_sym[s]["r"] += r_r
            if pnl_r > 0:
                by_sym[s]["w"] += 1
            else:
                by_sym[s]["l"] += 1
        print("\n  By symbol:")
        for s, d in sorted(by_sym.items(), key=lambda x: -x[1]["pnl"]):
            print(f"    {s:10} {d['w']}W/{d['l']}L  P&L=${d['pnl']:+,.2f}  R={d['r']:+.1f}R")

        print("\n  Trade log:")
        for r in sorted(closed, key=lambda x: x.get("exit_time") or ""):
            ts = (r.get("exit_time") or r.get("entry_time") or "")[:16]
            print(
                f"    {ts} {r['symbol']:10} {r['direction']:4} "
                f"entry={r['entry_price']:.4f} exit={r['exit_price']:.4f} "
                f"${r.get('pnl_usd') or 0:+,.2f} ({r.get('r_multiple') or 0:+.1f}R)"
            )

    if opens:
        print("\n  OPEN POSITIONS:")
        for r in opens:
            ts = (r.get("entry_time") or "")[:16]
            print(
                f"    {ts} {r['symbol']:10} {r['direction']:4} "
                f"entry={r['entry_price']:.4f} SL={r.get('sl_price') or 0:.4f} "
                f"TP={r.get('tp_price') or 0:.4f}  #{r['ticket']}"
            )


def section_paper(paper_events: list[dict]) -> None:
    header("PAPER SHADOW — bridge-only (paper_trades_*.jsonl)")
    opens_by_ticket: dict[int, dict] = {}
    closes: list[dict] = []
    for ev in paper_events:
        e = ev.get("event", "")
        if e == "OPEN":
            opens_by_ticket[ev.get("ticket")] = ev
        elif e == "CLOSE":
            closes.append(ev)

    # Still-open paper positions = opens without matching close
    closed_tickets = {c.get("ticket") for c in closes}
    still_open = [o for tk, o in opens_by_ticket.items() if tk not in closed_tickets]

    print(f"  Opens:       {len(opens_by_ticket)}")
    print(f"  Closes:      {len(closes)}")
    print(f"  Still open:  {len(still_open)}")

    if closes:
        wins = [c for c in closes if (c.get("pnl_usd") or c.get("pnl") or 0) > 0]
        losses = [c for c in closes if (c.get("pnl_usd") or c.get("pnl") or 0) <= 0]
        pnl = sum((c.get("pnl_usd") or c.get("pnl") or 0) for c in closes)
        wr = len(wins) / len(closes) * 100
        avg_win = (sum(c.get("pnl_usd") or c.get("pnl") or 0 for c in wins)
                   / max(len(wins), 1))
        avg_loss = (sum(c.get("pnl_usd") or c.get("pnl") or 0 for c in losses)
                    / max(len(losses), 1))
        pf = abs(avg_win * len(wins) / (avg_loss * len(losses))) if avg_loss and losses else 0
        print(f"  Win rate:    {wr:.0f}% ({len(wins)}W / {len(losses)}L)")
        print(f"  Total P&L:   ${pnl:+,.2f}")
        print(f"  Avg win: ${avg_win:+,.2f}  Avg loss: ${avg_loss:+,.2f}  PF: {pf:.2f}")

        by_sym: dict[str, dict] = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
        for c in closes:
            s = c.get("symbol", "?")
            p = c.get("pnl_usd") or c.get("pnl") or 0
            by_sym[s]["pnl"] += p
            if p > 0:
                by_sym[s]["w"] += 1
            else:
                by_sym[s]["l"] += 1
        print("\n  By symbol:")
        for s, d in sorted(by_sym.items(), key=lambda x: -x[1]["pnl"]):
            print(f"    {s:10} {d['w']}W/{d['l']}L  P&L=${d['pnl']:+,.2f}")

        print("\n  Trade log:")
        for c in sorted(closes, key=lambda x: x.get("closed_at") or x.get("timestamp") or ""):
            ts = (c.get("closed_at") or c.get("timestamp") or "")[:16]
            sym = c.get("symbol", "?")
            direction = c.get("direction", "?")
            entry = c.get("entry") or c.get("entry_price") or 0
            exit_p = c.get("exit") or c.get("exit_price") or 0
            pnl_v = c.get("pnl_usd") or c.get("pnl") or 0
            r = c.get("r_multiple") or 0
            reason = c.get("reason", "?")
            grade = c.get("ict_grade", "-")
            print(
                f"    {ts} {sym:12} {direction:4} "
                f"entry={entry:<10.4f} exit={exit_p:<10.4f} "
                f"${pnl_v:+,.2f} ({r:+.1f}R) {reason:4} [{grade}]"
            )

    if still_open:
        print("\n  STILL OPEN (paper):")
        for o in still_open:
            ts = (o.get("opened_at") or o.get("timestamp") or "")[:16]
            print(
                f"    {ts} {o.get('symbol', '?'):12} {o.get('direction', '?'):4} "
                f"entry={o.get('entry_price', 0):<10.4f} "
                f"SL={o.get('sl_price', 0):<10.4f} TP={o.get('tp_price', 0):<10.4f} "
                f"[{o.get('ict_grade', '-')}] #{o.get('ticket', '?')}"
            )


def section_side_by_side(mt5_data: dict | None, paper_events: list[dict],
                          session_trades: list[dict]) -> None:
    """Compare LIVE vs PAPER trades in R-multiples (apples-to-apples).

    R-multiple = pnl / planned_risk_at_entry. Normalizes for differing balances:
    paper sims on $10k, live trades $98k FTMO — only R-multiples are comparable.
    """
    header("LIVE vs PAPER — R-MULTIPLE COMPARISON")
    if not mt5_data:
        print("  MT5 unavailable — skipping comparison")
        return

    # Build SL lookup keyed by (symbol, direction). Multiple OPENs per symbol
    # over time → keep list and pick nearest by entry price.
    sl_by_sym: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
    for t in session_trades:
        if t.get("event") == "OPEN":
            sym = normalize_symbol(t.get("symbol", ""))
            sl_by_sym[(sym, t.get("direction"))].append(
                (t.get("entry_price", 0), t.get("sl_price", 0))
            )

    def lookup_sl(sym: str, direction: str, entry: float) -> float:
        candidates = sl_by_sym.get((sym, direction), [])
        if not candidates:
            return 0.0
        # Pick the OPEN whose entry price is closest (handles slippage rounding)
        best = min(candidates, key=lambda ep_sl: abs(ep_sl[0] - entry))
        return best[1] if abs(best[0] - entry) < entry * 0.005 else 0.0

    live_trades = []
    for c in mt5_data.get("closed", []):
        sl = lookup_sl(c["symbol"], c["direction"], c["entry_price"])
        risk_per_unit = abs(c["entry_price"] - sl) if sl else 0
        actual_per_unit = abs(c["exit_price"] - c["entry_price"])
        if risk_per_unit > 0:
            r = (actual_per_unit / risk_per_unit) * (1 if c["net_pnl"] > 0 else -1)
        else:
            r = 0.0
        live_trades.append({
            "sym": c["symbol"], "dir": c["direction"], "pnl": c["net_pnl"], "r": r,
            "reason": c["reason"], "entry_t": c["entry_time"][:16],
        })

    # Paper R-multiples come straight from JSONL (already signed correctly)
    paper_trades = []
    opens: dict[int, dict] = {}
    for ev in paper_events:
        if ev.get("event") == "OPEN":
            opens[ev.get("ticket")] = ev
        elif ev.get("event") == "CLOSE":
            o = opens.get(ev.get("ticket"), {})
            r_raw = ev.get("r_multiple", 0)
            # JSONL r_multiple is unsigned — re-sign from pnl
            r_signed = abs(r_raw) * (1 if ev.get("pnl", 0) > 0 else -1)
            paper_trades.append({
                "sym": ev.get("symbol", "?"), "dir": ev.get("direction"),
                "pnl": ev.get("pnl", 0), "r": r_signed,
                "reason": ev.get("reason", "?"),
                "entry_t": (o.get("opened_at") or ev.get("timestamp", ""))[:16],
                "grade": o.get("ict_grade", "-"),
            })

    # Match live to paper by symbol+direction within 6h
    used = set()
    print(f"  {'STATUS':9} {'LIVE':<48} {'PAPER':<48}")
    print(f"  {'-'*9} {'-'*47} {'-'*47}")
    for l in live_trades:
        cands = [
            (i, p) for i, p in enumerate(paper_trades)
            if i not in used and p["sym"] == l["sym"] and p["dir"] == l["dir"]
            and abs((datetime.fromisoformat(p["entry_t"]) -
                     datetime.fromisoformat(l["entry_t"])).total_seconds()) < 6 * 3600
        ]
        if cands:
            i, p = min(cands, key=lambda ip: abs(
                (datetime.fromisoformat(ip[1]["entry_t"]) -
                 datetime.fromisoformat(l["entry_t"])).total_seconds()))
            used.add(i)
            agree = "MATCH" if (l["r"] > 0) == (p["r"] > 0) else "DIVERGE"
            l_str = f"{l['sym']:8} {l['dir']:4} {l['r']:+5.2f}R ${l['pnl']:+8.2f} {l['reason']}"
            p_str = f"{p['sym']:8} {p['dir']:4} {p['r']:+5.2f}R ${p['pnl']:+8.2f} {p['reason']}"
            print(f"  {agree:9} {l_str:<48} {p_str:<48}")
        else:
            l_str = f"{l['sym']:8} {l['dir']:4} {l['r']:+5.2f}R ${l['pnl']:+8.2f} {l['reason']}"
            print(f"  {'LIVE-ONLY':9} {l_str:<48} {'(no paper match)':<48}")

    print()
    live_avg_r = sum(t["r"] for t in live_trades) / max(len(live_trades), 1)
    paper_avg_r = sum(t["r"] for t in paper_trades) / max(len(paper_trades), 1)
    print(f"  LIVE  : {len(live_trades)} trades  Avg R: {live_avg_r:+.2f}  "
          f"Total: {sum(t['r'] for t in live_trades):+.2f}R")
    print(f"  PAPER : {len(paper_trades)} trades  Avg R: {paper_avg_r:+.2f}  "
          f"Total: {sum(t['r'] for t in paper_trades):+.2f}R")
    print(f"  Edge gap: {paper_avg_r - live_avg_r:+.2f}R per trade "
          f"(positive = paper outperforms live on avg)")


def section_health(mt5_data: dict | None) -> None:
    header("ACCOUNT HEALTH")
    if not mt5_data or not mt5_data.get("account"):
        print("  MT5 unavailable — run with bridge running to see live account state")
        return
    acct = mt5_data["account"]
    print(f"  Balance:          ${acct.get('balance', 0):,.2f}")
    print(f"  Equity:           ${acct.get('equity', 0):,.2f}")
    print(f"  Floating P&L:     ${acct.get('floating_pnl', 0):+,.2f}")
    print(f"  (Account is shared with other EAs — FTMO limits apply to total,")
    print(f"   not bridge-only numbers. Monitor via MT5 terminal for DD.)")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0,
                    help="Limit to last N days (0 = all).")
    args = ap.parse_args()

    since = None
    if args.days > 0:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)

    analyses, decisions, session_trades = load_sessions(since)
    mt5_data = load_mt5_bridge_trades(days=args.days or 30)
    paper = load_paper_trades(since)

    dates = sorted({a.get("_date") for a in analyses if a.get("_date")})
    period = f"{dates[0]} to {dates[-1]}" if dates else "no data"

    print("=" * W)
    print("  TRADING SYSTEM REPORT")
    print(f"  Period: {period}")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * W)

    section_analysis(analyses)
    section_decisions(decisions)
    if mt5_data is not None:
        section_live_mt5(mt5_data)
    else:
        print("\n  [MT5 unavailable — falling back to ledger DB]")
        section_live(load_ledger())
    section_paper(paper)
    section_side_by_side(mt5_data, paper, session_trades)
    section_health(mt5_data)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
