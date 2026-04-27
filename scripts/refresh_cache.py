"""
Refresh trading-ai-v2 M15/H1/H4 caches up to current time.

Why: bench_winners_not_blocked replay was missing 8 trades because caches
end at 2026-04-20 and most recent trades happened 2026-04-23/24/25. This
pulls fresh bars from the same MT5 terminal the live bridge uses and
overwrites the parquet caches.

Uses MetaTrader5 module directly — does NOT go through trading-ai-v2's
data.mt5_connector because that module's import side-effect opens
trading-ai-v2's log file, which conflicts with the live bridge's open
file handle on logs/trading.log on Windows.

Safe to run while the live bridge is running: MT5 terminal allows
multiple Python clients to connect simultaneously.

Usage:
    PYTHONUTF8=1 python scripts/refresh_cache.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

# These match the live bridge's FTMO-Demo credentials (see
# trading-ai-v2/config.yaml + trading-ai-v2/.env). Hard-coded here
# because we deliberately avoid importing the trading-ai-v2 logging /
# config stack (see module docstring).
MT5_LOGIN = 1513140458
MT5_PASSWORD = "L!$q1k@4Z"
MT5_SERVER = "FTMO-Demo"
MT5_PATH = "C:/Program Files/METATRADER5.1/terminal64.exe"

CACHE_ROOT = Path("C:/Users/User/Desktop/trading-ai-v2/data/cache")

# cache-dir-name -> MT5 broker symbol on FTMO-Demo
CACHE_TO_MT5 = {
    "BTCUSD":     "BTCUSD",
    "ETHUSD":     "ETHUSD",
    "SOLUSD":     "SOLUSD",
    "EURUSD":     "EURUSD",
    "GBPUSD":     "GBPUSD",
    "XAUUSD":     "XAUUSD",
    "US500.cash": "US500.cash",
}

# tf-dir-name -> MT5 enum + bar count to fetch
TIMEFRAMES = {
    "M15": (mt5.TIMEFRAME_M15, 35_000),
    "H1":  (mt5.TIMEFRAME_H1,  10_000),
    "H4":  (mt5.TIMEFRAME_H4,   5_000),
}


def fetch_bars(symbol: str, tf: int, count: int) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(np.float64)
    return df


def refresh_one(cache_name: str, mt5_sym: str, tf_label: str,
                tf_int: int, count: int) -> dict:
    cache_path = CACHE_ROOT / cache_name / tf_label / "data.parquet"
    if not cache_path.parent.exists():
        return {"skipped": True, "reason": "cache dir missing"}

    existing = None
    if cache_path.exists():
        existing = pd.read_parquet(cache_path)
        if existing.index.tz is None:
            existing.index = existing.index.tz_localize("UTC")

    fresh = fetch_bars(mt5_sym, tf_int, count)
    if fresh is None:
        err = mt5.last_error()
        return {"skipped": True, "reason": f"MT5 returned no bars (err={err})"}

    if existing is not None and len(existing) > 0:
        # fresh wins on duplicate timestamps so any closed-bar revisions land
        # but preserve existing column set (cache may have spread/real_volume
        # that fresh doesn't, or vice versa)
        common_cols = sorted(set(existing.columns) & set(fresh.columns))
        existing = existing[common_cols]
        fresh = fresh[common_cols]
        merged = pd.concat([existing, fresh])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        added = len(merged) - len(existing)
        old_latest = existing.index[-1]
    else:
        merged = fresh.sort_index()
        added = len(merged)
        old_latest = None

    merged.to_parquet(cache_path, compression="snappy")
    return {
        "skipped": False,
        "old_latest": old_latest,
        "new_latest": merged.index[-1],
        "added_bars": added,
        "total_bars": len(merged),
        "cols": list(merged.columns),
    }


def main() -> int:
    print("=" * 78)
    print(f"Cache refresh @ {datetime.now(timezone.utc).isoformat()}")
    print("=" * 78)

    if not mt5.initialize(
        path=MT5_PATH, login=MT5_LOGIN,
        password=MT5_PASSWORD, server=MT5_SERVER,
    ):
        err = mt5.last_error()
        print(f"ERROR: mt5.initialize failed: {err}")
        return 1
    info = mt5.account_info()
    print(f"MT5 connected: login={info.login} server={info.server} balance=${info.balance:.2f}")
    print()

    try:
        for cache_name, mt5_sym in CACHE_TO_MT5.items():
            for tf_label, (tf_int, count) in TIMEFRAMES.items():
                if not (CACHE_ROOT / cache_name / tf_label).exists():
                    continue
                r = refresh_one(cache_name, mt5_sym, tf_label, tf_int, count)
                tag = "SKIP" if r.get("skipped") else "OK  "
                if r.get("skipped"):
                    print(f"  [{tag}] {cache_name:<12} {tf_label:<4} ({r['reason']})")
                else:
                    old = r["old_latest"].isoformat() if r["old_latest"] else "(new)"
                    new = r["new_latest"].isoformat()
                    print(f"  [{tag}] {cache_name:<12} {tf_label:<4} "
                          f"old_latest={old}  new_latest={new}  +{r['added_bars']:>4} bars  total={r['total_bars']}")
    finally:
        mt5.shutdown()
        print("\nMT5 disconnected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
