"""One-shot trade verification script — run once, then delete."""
import json, os, requests, sys, time, random
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
headers = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
TV_TO_ALPACA = {"BTCUSD": "BTC/USD", "ETHUSD": "ETH/USD", "SOLUSD": "SOL/USD"}

PRICE_RANGES = {
    "XAUUSD": (3000, 8000),
    "UKOIL": (30, 200),
    "EURUSD": (0.5, 2.0),
    "US100": (10000, 50000),
    "US500": (3000, 10000),
}


def get_alpaca_bars(sym_base, start_dt):
    alpaca_sym = TV_TO_ALPACA.get(sym_base)
    if not alpaca_sym:
        return None
    end_dt = start_dt + timedelta(hours=72)
    resp = requests.get(
        "https://data.alpaca.markets/v1beta3/crypto/us/bars",
        headers=headers,
        params={"symbols": alpaca_sym, "timeframe": "1Hour",
                "start": start_dt.isoformat(), "end": end_dt.isoformat(), "limit": 500},
        timeout=10,
    )
    if resp.status_code != 200:
        return None
    return resp.json().get("bars", {}).get(alpaca_sym, [])


def get_tv_bars(full_sym, expected_base):
    from bridge.tv_client import TVClient
    tv = TVClient()
    lo, hi = PRICE_RANGES.get(expected_base, (0, 1e12))

    for attempt in range(5):
        tv.set_symbol(full_sym, require_ready=True)
        time.sleep(4)
        quote = tv.get_quote()
        chart_sym = quote.get("symbol", "").split(":")[-1]
        price = float(quote.get("last", 0))

        if chart_sym != expected_base or not (lo <= price <= hi):
            print(f"    attempt {attempt+1}: chart={chart_sym} price={price:.2f}, retrying...", flush=True)
            time.sleep(3)
            continue

        bars_data = tv.get_ohlcv(count=300, timeframe="60")
        bars = bars_data.get("bars", [])
        if not bars:
            continue

        checks = random.sample(bars, min(3, len(bars)))
        all_ok = all(lo <= float(b.get("high", 0)) <= hi * 1.1 for b in checks)
        if not all_ok:
            print(f"    attempt {attempt+1}: bar sanity fail, retrying...", flush=True)
            time.sleep(3)
            continue

        return bars
    return None


def check_trade(bars, action, sl, tp1, tp2, trade_ts, kh, kl, kt):
    sl_hit = tp1_hit = tp2_hit = None
    for bar in bars:
        bt = bar.get(kt, bar.get("time", 0))
        if isinstance(bt, str):
            bt_epoch = datetime.fromisoformat(bt.replace("Z", "+00:00")).timestamp()
            bt_str = datetime.fromisoformat(bt.replace("Z", "+00:00")).strftime("%m-%d %H:%M")
        else:
            bt_epoch = bt
            bt_str = datetime.fromtimestamp(bt, tz=timezone.utc).strftime("%m-%d %H:%M")

        if bt_epoch < trade_ts:
            continue

        h = float(bar.get(kh, bar.get("high", 0)))
        l = float(bar.get(kl, bar.get("low", 0)))

        if action == "BUY":
            if sl_hit is None and l <= sl: sl_hit = bt_str
            if tp1_hit is None and h >= tp1: tp1_hit = bt_str
            if tp2 > 0 and tp2_hit is None and h >= tp2: tp2_hit = bt_str
        else:
            if sl_hit is None and h >= sl: sl_hit = bt_str
            if tp1_hit is None and l <= tp1: tp1_hit = bt_str
            if tp2 > 0 and tp2_hit is None and l <= tp2: tp2_hit = bt_str

    return sl_hit, tp1_hit, tp2_hit


TRADES = [
    (1,  "SOLUSD",  "COINBASE:SOLUSD", "BUY",  82.28,   81.15,   84.92,   87.56,   "2026-04-09T08:23", "C", "alpaca"),
    (2,  "UKOIL",   "TVC:UKOIL",       "SELL", 98.29,   98.95,   97.15,   95.58,   "2026-04-09T08:30", "C", "tv"),
    (3,  "EURUSD",  "OANDA:EURUSD",    "SELL", 1.17,    1.175,   1.162,   1.149,   "2026-04-09T08:44", "B", "tv"),
    (4,  "XAUUSD",  "OANDA:XAUUSD",    "BUY",  4731.93, 4720.15, 4748.6,  4762.85, "2026-04-09T08:47", "C", "tv"),
    (5,  "ETHUSD",  "COINBASE:ETHUSD", "BUY",  2183.38, 2175.20, 2198.5,  2213.75, "2026-04-09T08:53", "B", "alpaca"),
    (6,  "BTCUSD",  "BITSTAMP:BTCUSD", "BUY",  71313.0, 70847.0, 72156.0, 73289.0, "2026-04-09T11:07", "B", "alpaca"),
    (7,  "SOLUSD",  "COINBASE:SOLUSD", "BUY",  81.58,   80.42,   83.87,   85.91,   "2026-04-09T14:33", "B", "alpaca"),
    (8,  "XAUUSD",  "OANDA:XAUUSD",    "BUY",  4758.34, 4748.2,  4775.5,  4799.8,  "2026-04-09T14:47", "B", "tv"),
    (9,  "BTCUSD",  "BITSTAMP:BTCUSD", "BUY",  71917.0, 71650.0, 72850.0, 73500.0, "2026-04-09T18:39", "B", "alpaca"),
    (10, "SOLUSD",  "COINBASE:SOLUSD", "BUY",  83.08,   82.21,   84.50,   86.75,   "2026-04-10T01:01", "B", "alpaca"),
    (11, "ETHUSD",  "COINBASE:ETHUSD", "BUY",  2196.60, 2180.87, 2220.0,  2250.0,  "2026-04-10T01:13", "B", "alpaca"),
    (12, "US100",   "CAPITALCOM:US100", "BUY",  25144.8, 25059.22,25289.5, 25434.2, "2026-04-10T12:48", "B", "tv"),
    (13, "US500",   "CAPITALCOM:US500", "BUY",  6834.5,  6818.54, 6858.25, 6897.38, "2026-04-10T13:10", "B", "tv"),
]


def main():
    results = []
    hdr = f"{'#':>2} {'Symbol':8} {'Dir':4} {'Gr':2} {'Entry':>12} {'SL':>12} {'TP1':>12} {'TP2':>12} | {'Outcome':14} {'R':>7} | Evidence"
    print(hdr)
    print("=" * len(hdr))

    for num, base, full_sym, action, entry, sl, tp1, tp2, ts, grade, source in TRADES:
        trade_dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        trade_ts = trade_dt.timestamp()
        sl_dist = abs(entry - sl)

        if source == "alpaca":
            bars = get_alpaca_bars(base, trade_dt)
            kh, kl, kt = "h", "l", "t"
        else:
            bars = get_tv_bars(full_sym, base)
            kh, kl, kt = "high", "low", "time"

        if not bars:
            print(f"{num:2} {base:8} {action:4} {grade:2} {entry:>12.4f} {sl:>12.4f} {tp1:>12.4f} {tp2:>12.4f} | {'NO_DATA':14} {'---':>7} | {source} failed")
            results.append(("NO_DATA", 0))
            continue

        sl_hit, tp1_hit, tp2_hit = check_trade(bars, action, sl, tp1, tp2, trade_ts, kh, kl, kt)

        outcome = ""
        r_val = 0.0
        evidence = ""

        if sl_hit and tp1_hit:
            if tp1_hit < sl_hit:
                if tp2_hit and tp2_hit < sl_hit:
                    r_val = round(abs(tp2 - entry) / sl_dist, 2)
                    outcome = "WIN_TP2"
                    evidence = f"TP1={tp1_hit} TP2={tp2_hit} SL_later={sl_hit}"
                else:
                    r_val = round(abs(tp1 - entry) / sl_dist, 2)
                    outcome = "WIN_TP1"
                    evidence = f"TP1={tp1_hit} SL_later={sl_hit}"
            elif sl_hit < tp1_hit:
                r_val = -1.0
                outcome = "LOSS_SL"
                evidence = f"SL={sl_hit} TP1_later={tp1_hit}"
            else:
                outcome = "AMBIGUOUS"
                evidence = f"same_bar={sl_hit}"
        elif tp1_hit:
            if tp2_hit:
                r_val = round(abs(tp2 - entry) / sl_dist, 2)
                outcome = "WIN_TP2"
                evidence = f"TP1={tp1_hit} TP2={tp2_hit} SL=never"
            else:
                r_val = round(abs(tp1 - entry) / sl_dist, 2)
                outcome = "WIN_TP1"
                evidence = f"TP1={tp1_hit} SL=never"
        elif sl_hit:
            r_val = -1.0
            outcome = "LOSS_SL"
            evidence = f"SL={sl_hit} TP1=never"
        else:
            outcome = "OPEN"
            evidence = "neither_hit"

        results.append((outcome, r_val))
        r_str = f"{r_val:+.2f}R" if outcome not in ("OPEN", "AMBIGUOUS") else "---"
        print(f"{num:2} {base:8} {action:4} {grade:2} {entry:>12.4f} {sl:>12.4f} {tp1:>12.4f} {tp2:>12.4f} | {outcome:14} {r_str:>7} | {evidence}")

    print(f"\n{'='*100}")
    wins = [(o, r) for o, r in results if o.startswith("WIN")]
    losses = [(o, r) for o, r in results if o == "LOSS_SL"]
    opens = [(o, r) for o, r in results if o == "OPEN"]
    no_data = [(o, r) for o, r in results if o == "NO_DATA"]

    closed_r = sum(r for _, r in wins) + sum(r for _, r in losses)
    closed_n = len(wins) + len(losses)

    print(f"Wins: {len(wins)}  Losses: {len(losses)}  Open: {len(opens)}  No data: {len(no_data)}")
    if closed_n > 0:
        print(f"Win Rate: {len(wins)}/{closed_n} = {len(wins)/closed_n*100:.0f}%")
        print(f"Total R (closed): {closed_r:+.2f}R")
        if wins:
            print(f"Avg Winner: {sum(r for _,r in wins)/len(wins):+.2f}R")
        if losses:
            print(f"Avg Loser: {sum(r for _,r in losses)/len(losses):+.2f}R")
        print(f"Expectancy: {closed_r/closed_n:+.2f}R per trade")


if __name__ == "__main__":
    main()
