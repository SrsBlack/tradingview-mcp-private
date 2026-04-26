"""Backtest: would the new MTF invalidation logic have closed any of our 19 trades?

For each closed trade in the session-store:
1. Walk forward from (open + 2h) at 15-min intervals (= bridge cycle interval)
2. Skip if position would be at or above breakeven (matches new logic exempt)
3. At each tick, query MT5 H4 + D1 bias as it would have looked at that moment
4. If both H4 AND D1 oppose direction, mark "MTF would have closed at X"
5. Compare: hypothetical close vs actual close

Verdict per trade:
- SAVED: MTF closed the trade earlier than SL → realized smaller loss
- NO_HELP: MTF fired but at a price worse/same as actual close
- NO_FIRE: MTF never fired across the trade's lifetime
- NO_INTERFERENCE: trade closed before 2h age window
- KILLED_WINNER: MTF closed a trade that ended profitable
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

sys.path.insert(0, r"C:\Users\User\tradingview-mcp-jackson")
sys.stdout.reconfigure(encoding="utf-8")

from bridge.config import ensure_trading_ai_path, tv_to_ftmo_symbol

ensure_trading_ai_path()

from analysis.structure import detect_swings, classify_structure, get_current_bias
from core.types import Direction


CHECK_INTERVAL_MIN = 15
HTF_AGE_SKIP_HOURS = 2

mt5.initialize()


def get_bias_at(ftmo_sym: str, end_dt: datetime, timeframe_name: str) -> str:
    tf_config = {
        "H4": (mt5.TIMEFRAME_H4, 100, 5),
        "D1": (mt5.TIMEFRAME_D1, 60, 3),
        "W1": (mt5.TIMEFRAME_W1, 30, 2),
    }
    tf, n_bars, lookback = tf_config[timeframe_name]
    rates = mt5.copy_rates_from(ftmo_sym, tf, end_dt, n_bars)
    if rates is None or len(rates) < 15:
        return "NEUTRAL"
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time")
    df = df.rename(columns={"tick_volume": "volume"})
    if len(df) > 15:
        df = df.iloc[:-1]
    swings = detect_swings(df, lookback=lookback)
    _, events = classify_structure(swings, df=df)
    return get_current_bias(events).name


def get_price_at(ftmo_sym: str, dt: datetime) -> float | None:
    rates = mt5.copy_rates_from(ftmo_sym, mt5.TIMEFRAME_M5, dt, 1)
    if rates is None or len(rates) == 0:
        return None
    return float(rates[0]["close"])


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def load_trades() -> list[tuple[dict, dict]]:
    session_dir = Path.home() / ".tradingview-mcp" / "sessions"
    dates = ["2026-04-19","2026-04-20","2026-04-21","2026-04-22","2026-04-23","2026-04-24","2026-04-25"]
    all_trades: list[dict] = []
    for d in dates:
        p = session_dir / f"{d}.json"
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        all_trades.extend(s.get("trades", []))

    opens = {t["ticket"]: t for t in all_trades if t.get("event") == "OPEN" and t.get("mode") != "paper_shadow"}
    closes = [t for t in all_trades if t.get("event") == "CLOSE" and t.get("reason") != "TP (while offline)"]

    pairs = []
    for c in closes:
        ticket = c.get("ticket")
        if ticket in opens:
            pairs.append((opens[ticket], c))
    return pairs


def main() -> None:
    pairs = load_trades()
    print(f"Loaded {len(pairs)} closed trades\n")

    rows = []
    total_saved = 0.0
    total_cost = 0.0

    for o, c in pairs:
        sym_tv = o.get("symbol", "")
        sym_base = sym_tv.split(":")[-1]
        ftmo_sym = tv_to_ftmo_symbol(sym_base)
        direction = o.get("direction", "")
        if direction not in ("BUY", "SELL"):
            continue

        open_t = parse_iso(o.get("opened_at") or o.get("timestamp"))
        close_t = parse_iso(c.get("timestamp"))
        if not open_t or not close_t:
            continue

        actual_pnl = c.get("pnl", 0)
        actual_exit = c.get("exit_price", 0)
        entry = o.get("entry_price", 0)
        sl = o.get("sl_price", 0)
        sl_dist = abs(entry - sl)
        actual_outcome = "WIN" if actual_pnl > 0 else ("LOSS" if actual_pnl < 0 else "BE")

        check_start = open_t + timedelta(hours=HTF_AGE_SKIP_HOURS)
        if check_start >= close_t:
            rows.append({
                "ticket": o["ticket"], "symbol": sym_base, "direction": direction,
                "actual_pnl": actual_pnl, "actual_outcome": actual_outcome,
                "fired": False, "verdict": f"NO_INTERFERENCE (closed in {(close_t-open_t).total_seconds()/3600:.1f}h)",
            })
            continue

        cur = check_start
        mtf_fired = None
        while cur < close_t:
            cur_price = get_price_at(ftmo_sym, cur)
            if cur_price is None:
                cur += timedelta(minutes=CHECK_INTERVAL_MIN)
                continue

            unreal = (cur_price - entry) if direction == "BUY" else (entry - cur_price)
            r_now = unreal / sl_dist if sl_dist > 0 else 0

            # Exempt: above breakeven
            if r_now >= 0:
                cur += timedelta(minutes=CHECK_INTERVAL_MIN)
                continue

            h4 = get_bias_at(ftmo_sym, cur, "H4")
            d1 = get_bias_at(ftmo_sym, cur, "D1")

            opposes_h4 = (direction == "BUY" and h4 == "BEARISH") or (direction == "SELL" and h4 == "BULLISH")
            opposes_d1 = (direction == "BUY" and d1 == "BEARISH") or (direction == "SELL" and d1 == "BULLISH")

            if opposes_h4 and opposes_d1:
                mtf_fired = {"t": cur, "price": cur_price, "h4": h4, "d1": d1, "r": r_now}
                break

            cur += timedelta(minutes=CHECK_INTERVAL_MIN)

        if not mtf_fired:
            rows.append({
                "ticket": o["ticket"], "symbol": sym_base, "direction": direction,
                "actual_pnl": actual_pnl, "actual_outcome": actual_outcome,
                "fired": False, "verdict": "NO_FIRE",
            })
            continue

        # Hypothetical pnl at the close — scale by ratio of actual unrealized
        actual_unrealized = (actual_exit - entry) if direction == "BUY" else (entry - actual_exit)
        mtf_unrealized = (mtf_fired["price"] - entry) if direction == "BUY" else (entry - mtf_fired["price"])
        if actual_unrealized != 0:
            hypothetical_pnl = actual_pnl * (mtf_unrealized / actual_unrealized)
        else:
            hypothetical_pnl = 0

        if actual_outcome == "LOSS":
            saved = abs(actual_pnl) - abs(hypothetical_pnl) if hypothetical_pnl < 0 else abs(actual_pnl)
            if saved > 0:
                verdict = f"SAVED ${saved:+.2f} (mtf=${hypothetical_pnl:+.2f})"
                total_saved += saved
            elif saved < 0:
                verdict = f"WORSE_EXIT ${saved:+.2f} (mtf=${hypothetical_pnl:+.2f})"
                total_cost += -saved
            else:
                verdict = f"NO_HELP (mtf=${hypothetical_pnl:+.2f})"
        elif actual_outcome == "WIN":
            cost = actual_pnl - hypothetical_pnl
            verdict = f"KILLED_WINNER cost=${cost:+.2f} (mtf=${hypothetical_pnl:+.2f})"
            total_cost += cost
        else:
            verdict = f"BE_TRADE mtf=${hypothetical_pnl:+.2f}"

        rows.append({
            "ticket": o["ticket"], "symbol": sym_base, "direction": direction,
            "actual_pnl": actual_pnl, "actual_outcome": actual_outcome,
            "fired": True,
            "fire_t": mtf_fired["t"], "h4": mtf_fired["h4"], "d1": mtf_fired["d1"],
            "r_at_fire": mtf_fired["r"],
            "hypothetical_pnl": hypothetical_pnl, "verdict": verdict,
        })

    # Print
    print(f"{'Symbol':<10} {'Dir':<5} {'Outcome':<7} {'Actual':>10} {'Fired':<6} {'When (h after open)':<22} {'Verdict'}")
    print("-" * 130)
    for r in rows:
        f_str = "YES" if r["fired"] else "no"
        when = ""
        if r["fired"]:
            o_t = next(p[0]["opened_at"] for p in pairs if p[0]["ticket"] == r["ticket"])
            o_dt = parse_iso(o_t)
            hours = (r["fire_t"] - o_dt).total_seconds() / 3600
            when = f"{hours:.1f}h H4={r['h4']} D1={r['d1']}"
        print(f"{r['symbol']:<10} {r['direction']:<5} {r['actual_outcome']:<7} ${r['actual_pnl']:>+9.2f} {f_str:<6} {when:<22} {r['verdict']}")

    fired = sum(1 for r in rows if r["fired"])
    print(f"\n=== SUMMARY ===")
    print(f"Trades tested: {len(rows)}")
    print(f"MTF fired: {fired}/{len(rows)}")
    print(f"Money saved (closed losers earlier than SL): ${total_saved:+.2f}")
    print(f"Money cost (closed winners early or made losses worse): ${total_cost:+.2f}")
    print(f"Net deployment value: ${total_saved - total_cost:+.2f}")

    # Per-outcome breakdown
    losers_fired = sum(1 for r in rows if r["fired"] and r["actual_outcome"] == "LOSS")
    losers_total = sum(1 for r in rows if r["actual_outcome"] == "LOSS")
    winners_fired = sum(1 for r in rows if r["fired"] and r["actual_outcome"] == "WIN")
    winners_total = sum(1 for r in rows if r["actual_outcome"] == "WIN")
    print(f"\nLosers caught: {losers_fired}/{losers_total}")
    print(f"Winners hit: {winners_fired}/{winners_total}")


if __name__ == "__main__":
    main()
    mt5.shutdown()
