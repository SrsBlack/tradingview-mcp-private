"""
MT5 OHLCV data collection — replaces TradingView chart switching.

MT5 provides reliable, fast OHLCV data without any chart switching issues.
All symbols are available simultaneously — no need to switch between them.

Usage::

    from bridge.mt5_data import MT5DataCollector

    collector = MT5DataCollector()
    dfs = collector.collect_data("BTCUSD")
    # dfs = {"H4": DataFrame, "H1": DataFrame, "M15": DataFrame}
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from bridge.config import get_bridge_config, tv_to_ftmo_symbol, BridgeConfig


# MT5 timeframe mapping
_TF_MAP: dict[str, int] = {}


def _ensure_tf_map() -> None:
    """Lazily populate MT5 timeframe constants."""
    global _TF_MAP
    if _TF_MAP:
        return
    try:
        import MetaTrader5 as mt5
        _TF_MAP.update({
            "1": mt5.TIMEFRAME_M1,
            "5": mt5.TIMEFRAME_M5,
            "15": mt5.TIMEFRAME_M15,
            "30": mt5.TIMEFRAME_M30,
            "60": mt5.TIMEFRAME_H1,
            "240": mt5.TIMEFRAME_H4,
            "D": mt5.TIMEFRAME_D1,
            "W": mt5.TIMEFRAME_W1,
            "M15": mt5.TIMEFRAME_M15,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
            "W1": mt5.TIMEFRAME_W1,
        })
    except ImportError:
        pass


def _mt5_to_dataframe(rates: Any) -> pd.DataFrame:
    """Convert MT5 rates array to OHLCV DataFrame with DatetimeIndex."""
    if rates is None or len(rates) == 0:
        return pd.DataFrame()

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.rename(columns={
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "tick_volume": "volume",
    }, inplace=True)

    # Ensure we have the standard columns
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            if col == "volume" and "real_volume" in df.columns:
                df["volume"] = df["real_volume"]
            else:
                df[col] = 0.0

    # Keep only OHLCV columns
    df = df[["open", "high", "low", "close", "volume"]].copy()

    return df


class MT5DataCollector:
    """Collect OHLCV data directly from MT5 — no TradingView needed.

    MT5 provides all timeframes for all symbols simultaneously.
    No chart switching, no symbol drift, no quote failures.
    """

    def __init__(self, config: BridgeConfig | None = None):
        self.config = config or get_bridge_config()
        self._connected = False
        self._connect()

    def _connect(self) -> None:
        """Ensure MT5 is connected."""
        try:
            import MetaTrader5 as mt5
            if not mt5.terminal_info():
                mt5.initialize()
            self._connected = mt5.terminal_info() is not None
            if self._connected:
                _ensure_tf_map()
        except ImportError:
            print("[MT5_DATA] MetaTrader5 not installed", flush=True)
        except Exception as e:
            print(f"[MT5_DATA] Connection error: {e}", flush=True)

    def collect_data(self, symbol: str) -> dict[str, pd.DataFrame | None]:
        """Collect H4, H1, M15 OHLCV data from MT5.

        Args:
            symbol: TradingView symbol (e.g., "BITSTAMP:BTCUSD", "OANDA:EURUSD")

        Returns:
            {"H4": df, "H1": df, "M15": df} — None for any that fail.
        """
        dfs: dict[str, pd.DataFrame | None] = {
            "W1": None, "D1": None, "H4": None, "H1": None, "M15": None,
        }

        if not self._connected:
            self._connect()
            if not self._connected:
                return dfs

        # Convert TV symbol to MT5/FTMO symbol
        base_sym = symbol.split(":")[-1] if ":" in symbol else symbol
        # Map through config (e.g., "YM1!" -> "US30")
        internal = self.config.internal_symbol(symbol)
        ftmo_sym = tv_to_ftmo_symbol(internal)

        try:
            import MetaTrader5 as mt5

            # Verify symbol exists on MT5
            info = mt5.symbol_info(ftmo_sym)
            if info is None:
                # Try without .cash suffix
                ftmo_sym_alt = ftmo_sym.replace(".cash", "")
                info = mt5.symbol_info(ftmo_sym_alt)
                if info is not None:
                    ftmo_sym = ftmo_sym_alt
                else:
                    print(f"[MT5_DATA] Symbol {ftmo_sym} not found on MT5", flush=True)
                    return dfs

            # Ensure symbol is visible in Market Watch
            if not info.visible:
                mt5.symbol_select(ftmo_sym, True)

            cfg = self.config
            bar_counts = {
                "W1": 52,   # 1 year of weekly bars
                "D1": 120,  # ~6 months of daily bars (ICT uses 20/40/60-day ranges)
                "H4": cfg.bar_counts.get(cfg.htf, 200),
                "H1": cfg.bar_counts.get(cfg.itf, 200),
                "M15": cfg.bar_counts.get(cfg.ltf, 200),
            }

            for tf_label, mt5_tf_key in [
                ("W1", "W1"), ("D1", "D1"),
                ("H4", "H4"), ("H1", "H1"), ("M15", "M15"),
            ]:
                mt5_tf = _TF_MAP.get(mt5_tf_key)
                if mt5_tf is None:
                    continue

                count = bar_counts.get(tf_label, 200)
                rates = mt5.copy_rates_from_pos(ftmo_sym, mt5_tf, 0, count)

                if rates is not None and len(rates) > 0:
                    df = _mt5_to_dataframe(rates)
                    if not df.empty and len(df) >= 4:
                        dfs[tf_label] = df

            # Log what we got
            w1 = len(dfs["W1"]) if dfs["W1"] is not None else 0
            d1 = len(dfs["D1"]) if dfs["D1"] is not None else 0
            h4 = len(dfs["H4"]) if dfs["H4"] is not None else 0
            h1 = len(dfs["H1"]) if dfs["H1"] is not None else 0
            m15 = len(dfs["M15"]) if dfs["M15"] is not None else 0
            if h4 + h1 + m15 > 0:
                print(f"  [{symbol}] MT5 OHLCV: W1={w1} D1={d1} H4={h4} H1={h1} M15={m15} bars", flush=True)
            else:
                print(f"  [{symbol}] MT5 OHLCV: no data for {ftmo_sym}", flush=True)

        except ImportError:
            print("[MT5_DATA] MetaTrader5 not installed", flush=True)
        except Exception as e:
            print(f"  [{symbol}] MT5 data error: {e}", flush=True)

        return dfs

    def get_current_price(self, symbol: str) -> float:
        """Get current price from MT5 tick data."""
        if not self._connected:
            return 0.0

        try:
            import MetaTrader5 as mt5
            internal = self.config.internal_symbol(symbol)
            ftmo_sym = tv_to_ftmo_symbol(internal)
            tick = mt5.symbol_info_tick(ftmo_sym)
            if tick is None:
                # Try without .cash
                tick = mt5.symbol_info_tick(ftmo_sym.replace(".cash", ""))
            if tick and tick.bid > 0:
                return (tick.bid + tick.ask) / 2
        except Exception:
            pass
        return 0.0

    def get_smt_data(self, smt_symbol: str, timeframe: str = "M15", count: int = 50) -> pd.DataFrame | None:
        """Fetch OHLCV for a correlated symbol (SMT divergence check)."""
        if not self._connected:
            return None

        try:
            import MetaTrader5 as mt5
            ftmo_sym = tv_to_ftmo_symbol(smt_symbol)
            mt5_tf = _TF_MAP.get(timeframe, mt5.TIMEFRAME_M15)
            rates = mt5.copy_rates_from_pos(ftmo_sym, mt5_tf, 0, count)
            if rates is not None and len(rates) >= 10:
                return _mt5_to_dataframe(rates)
        except Exception:
            pass
        return None
