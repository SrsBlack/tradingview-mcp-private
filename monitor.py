"""Live monitoring dashboard for the TradingView bridge.

Tracks what `report.py` doesn't:
  1. Claude SKIP rate + categorized reasons (zone gate, grade gate, session...)
  2. Grade -> outcome map (does Grade A actually win more than B?)
  3. Per-symbol edge in R-multiples + $ P&L
  4. Model routing cost (haiku vs sonnet calls)
  5. Current-trial scorecard (MT5 deals reset every 14-day FTMO trial)
  6. Daily loss watch (FTMO compliance signal)

Data sources by purpose:
  - STRATEGY VALIDATION (full history, survives FTMO trial resets):
      * logs/paper_trades_*.jsonl — paper shadow (mirror of live decisions)
      * ~/.tradingview-mcp/trading_ledger.db — signal_grade + r_multiple
  - CURRENT TRIAL STATUS (MT5 only, resets on new trial):
      * MT5 positions_get / history_deals_get — filtered by ICT_Bridge comment
  - DECISION FLOW (all history):
      * ~/.tradingview-mcp/sessions/YYYY-MM-DD.json — analyses, decisions

Usage:
  python monitor.py                 # Last 30 days, strategy + current-trial
  python monitor.py --days 7        # Shorter lookback for decision flow
  python monitor.py --watch         # Refresh every 60s
  python monitor.py --symbol BTCUSD # Filter to one symbol
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, Exception):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge.symbol_utils import normalize_symbol  # noqa: E402

HOME = Path.home()
SESSIONS_DIR = HOME / ".tradingview-mcp" / "sessions"
LEDGER_PATH = HOME / ".tradingview-mcp" / "trading_ledger.db"
PAPER_LOG_DIR = Path(__file__).resolve().parent / "logs"

BRIDGE_COMMENT_TAG = "ICT_Bridge"

# Strategy overhaul date: zone gate + bias fix + wider crypto SL shipped 2026-04-19.
# Trades before this used a materially different strategy and should not be
# pooled with post-fix data for validation.
STRATEGY_CUTOFF = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)

W = 70
LINE = chr(0x2500) * W


# ---------------------------------------------------------------------------
# Data loaders (shared with report.py, kept minimal here)
# ---------------------------------------------------------------------------

def load_sessions(days: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (analyses, decisions, session_trades) from last N days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    analyses: list[dict] = []
    decisions: list[dict] = []
    trades: list[dict] = []

    for f in sorted(SESSIONS_DIR.glob("2026-*.json")):
        if ".backup" in f.name or ".corrupt" in f.name:
            continue
        try:
            date = datetime.strptime(f.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if date < since - timedelta(days=1):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
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


def load_mt5_closed_trades(days: int) -> tuple[dict | None, list[dict]]:
    """Return (account_info, closed_bridge_trades) from MT5. None if unavailable."""
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError:
        return None, []
    if not mt5.initialize():
        return None, []
    try:
        acct_raw = mt5.account_info()
        account = {
            "login": acct_raw.login,
            "balance": acct_raw.balance,
            "equity": acct_raw.equity,
            "floating": acct_raw.profit,
        } if acct_raw else None

        now = datetime.now(timezone.utc) + timedelta(hours=12)
        deals = mt5.history_deals_get(now - timedelta(days=days + 1), now) or []

        by_position: dict[int, list] = defaultdict(list)
        for d in deals:
            by_position[d.position_id].append(d)

        closed: list[dict] = []
        for pid, ds in by_position.items():
            entries = [d for d in ds if d.entry == 0]
            exits = [d for d in ds if d.entry == 1]
            if not entries or not exits:
                continue
            entry = entries[0]
            if BRIDGE_COMMENT_TAG not in (entry.comment or ""):
                continue
            last_exit = exits[-1]
            net = sum(d.profit for d in exits) + sum(d.commission for d in ds) + sum(d.swap for d in ds)
            reason = "TP" if "tp" in (last_exit.comment or "").lower() else \
                     "SL" if "sl" in (last_exit.comment or "").lower() else "CLOSE"
            closed.append({
                "position_id": pid,
                "symbol": normalize_symbol(entry.symbol),
                "direction": "BUY" if entry.type == 0 else "SELL",
                "entry_price": entry.price,
                "exit_price": last_exit.price,
                "entry_time": datetime.fromtimestamp(entry.time, tz=timezone.utc),
                "exit_time": datetime.fromtimestamp(last_exit.time, tz=timezone.utc),
                "net_pnl": net,
                "reason": reason,
            })
        closed.sort(key=lambda x: x["exit_time"])
        return account, closed
    finally:
        mt5.shutdown()


def load_ledger_r_multiples() -> dict[int, dict]:
    """Map position_id -> {r_multiple, signal_grade, signal_score} from ledger DB."""
    if not LEDGER_PATH.exists():
        return {}
    con = sqlite3.connect(str(LEDGER_PATH))
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT ticket, r_multiple, signal_grade, signal_score, pnl_usd "
            "FROM trades WHERE status='closed'"
        )
        return {r["ticket"]: dict(r) for r in cur.fetchall()}
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def load_ledger_all_closed() -> list[dict]:
    """All closed trades from ledger DB (survives FTMO trial resets).

    Returns a list of dicts with symbol, direction, entry/exit prices,
    pnl_usd, r_multiple, signal_grade. This is the authoritative source
    for strategy validation because it preserves per-trade grade context
    across trial resets.
    """
    if not LEDGER_PATH.exists():
        return []
    con = sqlite3.connect(str(LEDGER_PATH))
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT ticket, symbol, direction, entry_price, exit_price, "
            "entry_time, exit_time, pnl_usd, r_multiple, "
            "signal_grade, signal_score, session "
            "FROM trades WHERE status IN ('closed', 'closed_orphan') "
            "ORDER BY entry_time"
        )
        rows = [dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    for r in rows:
        r["symbol"] = normalize_symbol(r.get("symbol") or "")
    return rows


def load_paper_round_trips() -> list[dict]:
    """Reconstruct OPEN→CLOSE round-trips from paper_trades_*.jsonl.

    Paper trades survive FTMO trial resets (local files). Each round-trip
    carries the ict_grade from the OPEN event, so this is the best source
    for strategy validation when ledger data is sparse.
    """
    opens: dict[int, dict] = {}
    closes: list[dict] = []
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
                    ticket = ev.get("ticket")
                    if ticket is None:
                        continue
                    if ev.get("event") == "OPEN":
                        opens[ticket] = ev
                    elif ev.get("event") == "CLOSE":
                        closes.append(ev)
        except OSError:
            continue

    round_trips: list[dict] = []
    for c in closes:
        o = opens.get(c.get("ticket"))
        if not o:
            continue
        pnl = c.get("pnl_usd") or c.get("pnl") or 0.0
        r_raw = c.get("r_multiple", 0) or 0
        # JSONL r_multiple is unsigned — re-sign from pnl
        r_signed = abs(r_raw) * (1 if pnl > 0 else -1)
        round_trips.append({
            "ticket": c.get("ticket"),
            "symbol": normalize_symbol(c.get("symbol") or o.get("symbol") or ""),
            "direction": c.get("direction") or o.get("direction"),
            "entry_price": o.get("entry_price") or c.get("entry") or 0,
            "exit_price": c.get("exit_price") or c.get("exit") or 0,
            "entry_time": o.get("opened_at") or o.get("timestamp", ""),
            "exit_time": c.get("closed_at") or c.get("timestamp", ""),
            "pnl_usd": pnl,
            "r_multiple": r_signed,
            "signal_grade": o.get("ict_grade") or c.get("ict_grade"),
            "signal_score": o.get("ict_score"),
            "reason": c.get("reason", "?"),
        })
    return round_trips


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def merge_trade_sources(
    ledger_rows: list[dict],
    paper_rt: list[dict],
    cutoff: datetime | None = STRATEGY_CUTOFF,
) -> list[dict]:
    """Combine ledger (authoritative for live) + paper (fills gaps, survives resets).

    Dedup strategy: if ticket appears in both, prefer ledger.
    Cutoff filter: drops trades before cutoff — pre-overhaul trades used a
    materially different strategy and would pollute validation stats.
    """
    ledger_tickets = {r["ticket"] for r in ledger_rows if r.get("ticket")}
    merged = list(ledger_rows)
    for r in paper_rt:
        if r["ticket"] not in ledger_tickets:
            merged.append({**r, "source": "paper"})
    for r in merged:
        r.setdefault("source", "ledger")

    if cutoff is not None:
        before = len(merged)
        merged = [
            r for r in merged
            if (ts := _parse_ts(r.get("entry_time"))) is None or ts >= cutoff
        ]
        dropped = before - len(merged)
        if dropped > 0:
            # Stash count for display via section_grade_vs_outcome
            merged.append({"_dropped_pre_cutoff": dropped})

    merged = [r for r in merged if "_dropped_pre_cutoff" not in r or r]
    # Extract the sentinel (ugly but keeps function pure)
    return merged


def split_cutoff_info(trades: list[dict]) -> tuple[list[dict], int]:
    """Pull the dropped-count sentinel out of the trades list."""
    clean: list[dict] = []
    dropped = 0
    for t in trades:
        if "_dropped_pre_cutoff" in t:
            dropped = t["_dropped_pre_cutoff"]
        else:
            clean.append(t)
    clean.sort(key=lambda x: x.get("entry_time") or "")
    return clean, dropped


def detect_trial_start(closed_mt5: list[dict]) -> datetime | None:
    """Detect current FTMO trial start = earliest MT5 closed trade, or now - 14d.

    MT5 deal history is reset on each new trial, so the earliest visible
    deal is effectively the trial start. Returns None if no MT5 deals.
    """
    if not closed_mt5:
        return None
    return min(c["entry_time"] for c in closed_mt5)


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

# ORDER MATTERS — first match wins. Put specific patterns before generic ones.
SKIP_CATEGORIES: list[tuple[str, re.Pattern[str]]] = [
    ("zone_violation",   re.compile(r"ZONE VIOLATION|premium zone violates|BUYING? IN PREMIUM|SELLING? IN DISCOUNT|premium.*align|discount.*align", re.I)),
    ("ict_gates",        re.compile(r"fails critical ICT gates|critical failures|critical flaws|critical violation", re.I)),
    ("risk_limit",       re.compile(r"R:R.*below|exposure|daily.?loss|drawdown|max.?position", re.I)),
    ("grade_gate",       re.compile(r"below minimum for API call", re.I)),
    ("session_filter",   re.compile(r"outside kill zone|fallback mode|session|kill.?zone", re.I)),
    ("bias_mismatch",    re.compile(r"bias|htf|wrong side", re.I)),
    ("recent_trade",     re.compile(r"recent|cooldown|already open|duplicate", re.I)),
    ("claude_skip",      re.compile(r"^SKIP|conflicting|insufficient|weak|no confluence", re.I)),
]


def categorize_skip(reason: str) -> str:
    """Map a free-text SKIP reason into a bucket."""
    if not reason:
        return "unknown"
    for name, pat in SKIP_CATEGORIES:
        if pat.search(reason):
            return name
    return "other"


def pct(part: int, total: int) -> str:
    if total == 0:
        return "  0.0%"
    return f"{part / total * 100:5.1f}%"


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def header(title: str) -> None:
    print(f"\n{LINE}\n  {title}\n{LINE}")


def section_funnel(analyses: list[dict], decisions: list[dict]) -> None:
    """The selectivity funnel: analyses -> SKIPs -> trades."""
    header("SELECTIVITY FUNNEL — how many signals survive each gate")

    n_analyses = len(analyses)
    grade_counts = Counter(a.get("grade") for a in analyses)

    actions = Counter(d.get("action") for d in decisions)
    n_skips = actions.get("SKIP", 0)
    n_trades = sum(c for a, c in actions.items() if a != "SKIP")

    print(f"  Total analyses:      {n_analyses:6}")
    for g in ("A", "B", "C", "D", "INVALID"):
        if g in grade_counts:
            print(f"    Grade {g:8}:     {grade_counts[g]:6}  ({pct(grade_counts[g], n_analyses)})")

    print(f"\n  Total decisions:     {len(decisions):6}")
    print(f"    SKIPped:           {n_skips:6}  ({pct(n_skips, len(decisions))})")
    print(f"    Trade actions:     {n_trades:6}  ({pct(n_trades, len(decisions))})")

    # Overall conversion: analyses -> executed trades
    if n_analyses > 0:
        conv = n_trades / n_analyses * 100
        print(f"\n  Conversion (analysis → trade): {conv:.2f}%")
        print(f"  SKIP rate:                     {n_skips / max(len(decisions), 1) * 100:.1f}%")


def section_skip_reasons(decisions: list[dict]) -> None:
    """Categorize and rank SKIP reasons."""
    header("SKIP REASONS — categorized")
    skips = [d for d in decisions if d.get("action") == "SKIP"]
    if not skips:
        print("  No SKIPs recorded.")
        return

    buckets: Counter[str] = Counter()
    examples: dict[str, str] = {}
    for d in skips:
        reason = d.get("reasoning", "") or ""
        bucket = categorize_skip(reason)
        buckets[bucket] += 1
        if bucket not in examples:
            examples[bucket] = reason[:90]

    total = len(skips)
    print(f"  {'Category':<18} {'Count':>6}  {'Share':>6}   Example")
    print(f"  {'-'*18} {'-'*6}  {'-'*6}   {'-'*40}")
    for bucket, count in buckets.most_common():
        share = pct(count, total)
        example = examples.get(bucket, "")
        print(f"  {bucket:<18} {count:>6}  {share:>6}   {example}")


def section_model_routing(decisions: list[dict]) -> None:
    """Claude model usage — cost signal."""
    header("MODEL ROUTING — Claude API usage (cost signal)")
    models: Counter[str] = Counter()
    claude_calls = 0
    for d in decisions:
        m = (d.get("model_used") or "").lower()
        if "haiku" in m:
            models["haiku"] += 1
            claude_calls += 1
        elif "sonnet" in m:
            models["sonnet"] += 1
            claude_calls += 1
        elif "opus" in m:
            models["opus"] += 1
            claude_calls += 1
        elif m and m != "rule_based" and m != "-":
            models[m] += 1
            claude_calls += 1
        else:
            models["rule-based"] += 1

    total = sum(models.values())
    for m, c in models.most_common():
        print(f"  {m:<15} {c:>6}  ({pct(c, total)})")

    # Cost estimate — measured from real prompts on 2026-04-20
    # After concept injector added (+329 tokens avg):
    #   Input:  ~1530 tokens (system 128 + user prompt 1070 + concept block 330)
    #   Output: ~125 tokens (avg of 260 real Claude responses)
    # Sonnet 4.6: $3/MTok in, $15/MTok out  -> $0.00646/call
    # Haiku 4.5:  $1/MTok in, $5/MTok out   -> $0.00215/call
    sonnet_cost = models.get("sonnet", 0) * 0.00646
    haiku_cost = models.get("haiku", 0) * 0.00215
    print(f"\n  Estimated API spend: ${sonnet_cost + haiku_cost:.2f}  "
          f"(Sonnet ${sonnet_cost:.2f} + Haiku ${haiku_cost:.2f})")
    print(f"  Claude calls: {models.get('sonnet', 0) + models.get('haiku', 0)}"
          f"   Pre-gated (no API): {models.get('pre-gate', 0)}"
          f"   Rule-based: {models.get('rule-based-fallback', 0) + models.get('rule-based', 0)}")


def section_grade_vs_outcome(all_trades: list[dict]) -> None:
    """Does Grade A actually beat Grade B? Uses full history (ledger + paper).

    FTMO 14-day trials reset MT5 history, so we use the ledger DB + paper
    shadow log as the authoritative source for strategy validation.

    IMPORTANT: paper shadow P&L is not comparable to ledger P&L (paper
    uses simulated position sizing that can produce unrealistic dollar
    amounts). We therefore rely on:
      - R-multiple for edge comparison (scale-invariant — works across both)
      - Win rate (direction-only — works across both)
      - $ P&L split by source (never summed across sources)
    """
    header("GRADE → OUTCOME — strategy validation (post-overhaul)")

    graded = [t for t in all_trades if t.get("signal_grade")]
    if not graded:
        print("  No graded trades in history yet.")
        return

    by_grade: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "wins": 0,
                 "pnl_ledger": 0.0, "pnl_paper": 0.0,
                 "r_sum": 0.0, "r_n": 0,
                 "ledger": 0, "paper": 0}
    )
    for t in graded:
        g = t.get("signal_grade", "?")
        d = by_grade[g]
        d["n"] += 1
        pnl = t.get("pnl_usd") or 0.0
        if pnl > 0:
            d["wins"] += 1
        r = t.get("r_multiple")
        if r is not None:
            d["r_sum"] += r
            d["r_n"] += 1
        if t.get("source") == "paper":
            d["paper"] += 1
            d["pnl_paper"] += pnl
        else:
            d["ledger"] += 1
            d["pnl_ledger"] += pnl

    sources = Counter(t.get("source", "ledger") for t in graded)
    print(f"  Sample: {len(graded)} graded trades "
          f"({sources.get('ledger', 0)} ledger + {sources.get('paper', 0)} paper)")
    print(f"  Edge measured in R-multiples (scale-invariant across live/paper)")
    print()
    print(f"  {'Grade':<6} {'Trades':>7} {'Wins':>5} {'WinRate':>8} "
          f"{'Avg R':>7}  {'Ledger $':>11}  {'L:P':>8}")
    print(f"  {'-'*6} {'-'*7} {'-'*5} {'-'*8} {'-'*7}  {'-'*11}  {'-'*8}")
    for g in sorted(by_grade.keys()):
        d = by_grade[g]
        wr = d["wins"] / d["n"] * 100 if d["n"] else 0
        avg_r = d["r_sum"] / d["r_n"] if d["r_n"] else 0.0
        src = f"{d['ledger']}:{d['paper']}"
        print(f"  {g:<6} {d['n']:>7} {d['wins']:>5} {wr:>7.1f}% "
              f"{avg_r:>+6.2f}R  ${d['pnl_ledger']:>+9.2f}  {src:>8}")

    # Edge summary — requires at least 3 R-multiple samples per grade
    grade_a = by_grade.get("A", {"r_sum": 0, "r_n": 0})
    grade_b = by_grade.get("B", {"r_sum": 0, "r_n": 0})
    if grade_a["r_n"] >= 3 and grade_b["r_n"] >= 3:
        a_r = grade_a["r_sum"] / grade_a["r_n"]
        b_r = grade_b["r_sum"] / grade_b["r_n"]
        print()
        if a_r > b_r:
            print(f"  + Grade A outperforms Grade B by {a_r - b_r:+.2f}R/trade — grading is predictive")
        else:
            print(f"  ! Grade A UNDERPERFORMS Grade B by {b_r - a_r:.2f}R/trade — grading may need recalibration")


def section_symbol_edge(all_trades: list[dict]) -> None:
    """Per-symbol edge: which symbols are profitable? Uses full history.

    Edge is measured in R-multiples (scale-invariant across ledger+paper).
    Paper $ P&L is not summed with ledger $ P&L because paper uses
    simulated position sizing that can produce unrealistic amounts.
    """
    header("PER-SYMBOL EDGE — strategy validation (post-overhaul)")

    if not all_trades:
        print("  No trades in history yet.")
        return

    by_sym: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl_ledger": 0.0,
                 "r_sum": 0.0, "r_n": 0,
                 "ledger": 0, "paper": 0}
    )
    for t in all_trades:
        s = t["symbol"]
        d = by_sym[s]
        d["n"] += 1
        pnl = t.get("pnl_usd") or 0.0
        if pnl > 0:
            d["wins"] += 1
        r = t.get("r_multiple")
        if r is not None:
            d["r_sum"] += r
            d["r_n"] += 1
        if t.get("source") == "paper":
            d["paper"] += 1
        else:
            d["ledger"] += 1
            d["pnl_ledger"] += pnl

    print(f"  {'Symbol':<11} {'N':>4} {'WR':>6} {'AvgR':>7} "
          f"{'Ledger $':>11} {'L:P':>6}  Verdict")
    print(f"  {'-'*11} {'-'*4} {'-'*6} {'-'*7} "
          f"{'-'*11} {'-'*6}  {'-'*12}")
    # Rank by R-multiple (works even when only paper data exists)
    ranked = sorted(by_sym.items(),
                    key=lambda kv: -(kv[1]["r_sum"] / kv[1]["r_n"] if kv[1]["r_n"] else 0))
    for s, d in ranked:
        wr = d["wins"] / d["n"] * 100
        avg_r = d["r_sum"] / d["r_n"] if d["r_n"] else 0.0
        if d["n"] >= 5:
            verdict = "profitable" if avg_r > 0 else "losing"
        else:
            verdict = "too few"
        src = f"{d['ledger']}:{d['paper']}"
        print(f"  {s:<11} {d['n']:>4} {wr:>5.1f}% {avg_r:>+6.2f}R "
              f"${d['pnl_ledger']:>+9.2f} {src:>6}  {verdict}")


def section_current_trial(
    closed: list[dict],
    account: dict | None,
    trial_start: datetime | None,
    open_floating: float,
) -> None:
    """Current FTMO trial scorecard (MT5 deals only, scoped to this trial)."""
    header("CURRENT TRIAL — FTMO scorecard (MT5 only, resets every 14 days)")

    if not account:
        print("  MT5 unavailable — skipping.")
        return

    balance = account.get("balance", 0)
    equity = account.get("equity", 0)

    if trial_start:
        days_in = (datetime.now(timezone.utc) - trial_start).days
        days_left = max(0, 14 - days_in)
        print(f"  Trial started:    {trial_start.strftime('%Y-%m-%d %H:%M UTC')}  "
              f"({days_in}d in, ~{days_left}d left)")
    else:
        print(f"  Trial start:      unknown (no MT5 deals visible)")

    n_closed = len(closed)
    n_wins = sum(1 for c in closed if c["net_pnl"] > 0)
    wr = (n_wins / n_closed * 100) if n_closed else 0
    trial_pnl = sum(c["net_pnl"] for c in closed)

    print(f"\n  Balance:          ${balance:,.2f}")
    print(f"  Equity:           ${equity:,.2f}")
    print(f"  Floating (open):  ${open_floating:+,.2f}")
    print(f"\n  This trial:       {n_closed} closed trades, {n_wins}W "
          f"({wr:.1f}% WR), ${trial_pnl:+,.2f}")
    if n_closed < 5:
        print(f"  (Sample too small for meaningful stats — see strategy validation sections above)")


def section_daily_loss_watch(
    closed: list[dict],
    account: dict | None,
    open_floating: float = 0.0,
) -> None:
    """FTMO compliance signal: daily loss as % of account balance."""
    header("DAILY LOSS WATCH — FTMO compliance")

    if not account:
        print("  MT5 unavailable — skipping.")
        return

    balance = account.get("balance", 0)
    equity = account.get("equity", 0)

    # Today's closed P&L (bridge only)
    today = datetime.now(timezone.utc).date()
    today_pnl = sum(c["net_pnl"] for c in closed if c["exit_time"].date() == today)
    daily_loss_pct = -today_pnl / balance * 100 if balance and today_pnl < 0 else 0.0
    floating_pct = -open_floating / balance * 100 if balance and open_floating < 0 else 0.0

    print(f"  Balance:                  ${balance:,.2f}")
    print(f"  Equity:                   ${equity:,.2f}")
    print(f"  Today closed P&L:         ${today_pnl:+,.2f}")
    print(f"  Floating (open, bridge):  ${open_floating:+,.2f}")
    print(f"\n  Daily realized loss:      {daily_loss_pct:.2f}% of balance")
    print(f"  Floating loss pct:        {floating_pct:.2f}% of balance")
    print(f"  FTMO daily limit:         5.00%")
    if daily_loss_pct >= 4.0:
        print(f"\n  *** WARNING: within 1% of daily FTMO limit ***")
    elif daily_loss_pct + floating_pct >= 4.0:
        print(f"\n  *** CAUTION: realized + floating approaching FTMO limit ***")


def section_skip_by_symbol(decisions: list[dict]) -> None:
    """Which symbols are getting SKIPped most (wasted analysis)?"""
    header("SKIP RATE BY SYMBOL — where's the wasted analysis going?")
    by_sym: dict[str, dict[str, int]] = defaultdict(lambda: {"skip": 0, "trade": 0})
    for d in decisions:
        s = normalize_symbol(d.get("symbol", "") or "")
        if d.get("action") == "SKIP":
            by_sym[s]["skip"] += 1
        else:
            by_sym[s]["trade"] += 1

    print(f"  {'Symbol':<12} {'Total':>6} {'SKIP':>6} {'TRADE':>6} {'Skip%':>7}")
    print(f"  {'-'*12} {'-'*6} {'-'*6} {'-'*6} {'-'*7}")
    ranked = sorted(
        by_sym.items(),
        key=lambda kv: -(kv[1]["skip"] + kv[1]["trade"])
    )
    for s, d in ranked:
        total = d["skip"] + d["trade"]
        skip_pct = d["skip"] / total * 100 if total else 0
        print(f"  {s:<12} {total:>6} {d['skip']:>6} {d['trade']:>6} {skip_pct:>6.1f}%")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_once(days: int, symbol: str | None) -> None:
    analyses, decisions, session_trades = load_sessions(days)
    account, closed_mt5 = load_mt5_closed_trades(days=30)

    # Strategy validation sources (post-overhaul only; see STRATEGY_CUTOFF)
    ledger_all = load_ledger_all_closed()
    paper_rt = load_paper_round_trips()
    merged_raw = merge_trade_sources(ledger_all, paper_rt, cutoff=STRATEGY_CUTOFF)
    all_trades, dropped_count = split_cutoff_info(merged_raw)

    if symbol:
        sym_norm = normalize_symbol(symbol)
        analyses = [a for a in analyses if normalize_symbol(a.get("symbol", "")) == sym_norm]
        decisions = [d for d in decisions if normalize_symbol(d.get("symbol", "")) == sym_norm]
        closed_mt5 = [c for c in closed_mt5 if c["symbol"] == sym_norm]
        all_trades = [t for t in all_trades if t.get("symbol") == sym_norm]

    # Compute floating separately via positions_get (cheap re-open)
    open_floating = 0.0
    try:
        import MetaTrader5 as mt5  # type: ignore
        if mt5.initialize():
            try:
                for p in (mt5.positions_get() or []):
                    if BRIDGE_COMMENT_TAG in (p.comment or ""):
                        open_floating += p.profit + p.swap
            finally:
                mt5.shutdown()
    except Exception:
        pass

    trial_start = detect_trial_start(closed_mt5)

    # Header
    print("=" * W)
    print(f"  BRIDGE MONITOR — decision flow last {days}d"
          + (f"  (filter: {symbol})" if symbol else ""))
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Strategy validation: post-overhaul only "
          f"(>= {STRATEGY_CUTOFF.strftime('%Y-%m-%d')}"
          f"{', ' + str(dropped_count) + ' pre-overhaul trades excluded' if dropped_count else ''})")
    print("=" * W)

    # Decision flow (all history from session files)
    section_funnel(analyses, decisions)
    section_skip_reasons(decisions)
    section_skip_by_symbol(decisions)
    section_model_routing(decisions)

    # Strategy validation (full history — ledger + paper, survives trial resets)
    section_grade_vs_outcome(all_trades)
    section_symbol_edge(all_trades)

    # Current trial (MT5 only, 14-day scope)
    section_current_trial(closed_mt5, account, trial_start, open_floating)
    section_daily_loss_watch(closed_mt5, account, open_floating)

    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7,
                    help="Decision-flow lookback in days (default 7). "
                         "Strategy-validation sections use full history from ledger+paper.")
    ap.add_argument("--symbol", type=str, default=None, help="Filter to one symbol")
    ap.add_argument("--watch", action="store_true", help="Refresh every 60s")
    ap.add_argument("--interval", type=int, default=60, help="Watch interval seconds")
    args = ap.parse_args()

    if not args.watch:
        run_once(args.days, args.symbol)
        return 0

    try:
        while True:
            os.system("cls" if os.name == "nt" else "clear")
            run_once(args.days, args.symbol)
            print(f"  [refreshing every {args.interval}s — Ctrl+C to stop]")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
