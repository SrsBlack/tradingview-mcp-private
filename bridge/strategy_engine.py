"""
Strategy Engine — runs trading-ai-v2's 37 strategies on TradingView chart data.

Loads all EA (33) + ICT (4) strategies via their respective engines,
feeds them BarEvents built from TradingView OHLCV DataFrames, and
returns AggregatedSignalEvents after cluster voting.

Usage:
    from bridge.strategy_engine import StrategyEngine
    engine = StrategyEngine()
    signals = engine.process_symbol("BTCUSD", dataframes)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from bridge.config import ensure_trading_ai_path, get_bridge_config, BridgeConfig, TF_MAP
from bridge.tv_client import TVClient, TVClientError
from bridge.tv_data_adapter import bars_to_dataframe, validate_dataframe

ensure_trading_ai_path()

from core.types import Direction, TimeFrame, SessionType, RegimeType, Symbol
from core.events import BarEvent, AggregatedSignalEvent, SignalEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TimeFrame helpers
# ---------------------------------------------------------------------------

_TV_TO_TF: dict[str, TimeFrame] = {
    "1": TimeFrame.M1,
    "5": TimeFrame.M5,
    "15": TimeFrame.M15,
    "30": TimeFrame.M30,
    "60": TimeFrame.H1,
    "240": TimeFrame.H4,
    "D": TimeFrame.D1,
    "W": TimeFrame.W1,
}


def _df_to_bar_events(df: pd.DataFrame, symbol: str, tf: TimeFrame) -> list[BarEvent]:
    """Convert a pandas DataFrame to a list of BarEvents (last N bars)."""
    events = []
    for idx, row in df.iterrows():
        ts = idx if isinstance(idx, datetime) else pd.Timestamp(idx)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        events.append(BarEvent(
            symbol=symbol,
            timeframe=tf,
            timestamp=ts,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume", 0)),
            spread=float(row.get("spread", 0)),
        ))
    return events


# ---------------------------------------------------------------------------
# Strategy Engine
# ---------------------------------------------------------------------------

class StrategyEngine:
    """
    Wraps trading-ai-v2's EA and ICT engines to run all 37 strategies
    on TradingView chart data.

    Flow:
    1. Collect OHLCV from TradingView for multiple timeframes
    2. Build data_store (symbol -> tf -> DataFrame)
    3. Create BarEvent from the latest bar
    4. Feed to EAEngine.process_bar() and ICTEngine.process_bar()
    5. Return AggregatedSignalEvents
    """

    def __init__(self):
        self.config = get_bridge_config()
        self.tv_client = TVClient()
        self._ea_engine = None
        self._ict_engine = None
        self._data_store: dict[str, dict[str, pd.DataFrame]] = {}
        self._init_engines()

    def _init_engines(self) -> None:
        """Load EA and ICT engines with all strategies."""
        try:
            from engines.ea_engine import EAEngine
            self._ea_engine = EAEngine()
            ea_count = len(self._ea_engine._strategies) if hasattr(self._ea_engine, '_strategies') else 0
            print(f"[ENGINE] EA engine loaded: {ea_count} strategies", flush=True)
        except Exception as e:
            print(f"[ENGINE] EA engine failed to load: {e}", flush=True)

        try:
            from engines.ict_engine import ICTEngine
            self._ict_engine = ICTEngine()
            ict_count = len(self._ict_engine._strategies) if hasattr(self._ict_engine, '_strategies') else 0
            print(f"[ENGINE] ICT engine loaded: {ict_count} strategies", flush=True)
        except Exception as e:
            print(f"[ENGINE] ICT engine failed to load: {e}", flush=True)

    # ------------------------------------------------------------------
    # Data collection
    # ------------------------------------------------------------------

    def collect_data(
        self,
        symbol: str,
        timeframes: list[str] | None = None,
    ) -> dict[str, pd.DataFrame]:
        """
        Collect OHLCV data from TradingView for a symbol across timeframes.

        Args:
            symbol: TradingView symbol (e.g., "BTCUSD")
            timeframes: TV resolution strings (default: ["240", "60", "15", "5"])

        Returns:
            {tv_tf: pd.DataFrame, ...}
        """
        timeframes = timeframes or ["240", "60", "15", "5"]
        counts = self.config.bar_counts
        result: dict[str, pd.DataFrame] = {}

        try:
            switch_result = self.tv_client.set_symbol(symbol, require_ready=True)
            if not switch_result.get("chart_ready", False):
                logger.warning(f"[ENGINE] Chart not ready for {symbol} — skipping")
                return result

            # Verify quote confirms the symbol
            quote = self.tv_client.get_quote()
            target_sym = symbol.split(":")[-1]
            chart_sym = quote.get("symbol", "").split(":")[-1]
            if chart_sym != target_sym:
                logger.warning(f"[ENGINE] Quote mismatch: expected {target_sym}, got {chart_sym} — skipping")
                return result
        except TVClientError as e:
            logger.warning(f"[ENGINE] Failed to switch to {symbol}: {e}")
            return result

        for tf in timeframes:
            try:
                self.tv_client.set_timeframe(tf)
                time.sleep(0.7)
                raw = self.tv_client._run(["ohlcv", "-n", str(min(counts.get(tf, 200), 500))])
                df = bars_to_dataframe(raw)
                if df is not None and not df.empty:
                    result[tf] = df
            except (TVClientError, Exception) as e:
                logger.warning(f"[ENGINE] {symbol} {tf}: {e}")

        return result

    def _build_data_store(
        self,
        symbol: str,
        dataframes: dict[str, pd.DataFrame],
    ) -> None:
        """Populate the shared data store for strategies."""
        self._data_store[symbol] = {}
        for tv_tf, df in dataframes.items():
            tf_enum = _TV_TO_TF.get(tv_tf)
            if tf_enum is not None:
                self._data_store[symbol][tf_enum.value] = df

        # Inject into engines
        if self._ea_engine is not None:
            self._ea_engine.data_store = self._data_store
        if self._ict_engine is not None:
            self._ict_engine.data_store = self._data_store

    # ------------------------------------------------------------------
    # Process
    # ------------------------------------------------------------------

    def process_symbol(
        self,
        symbol: str,
        dataframes: dict[str, pd.DataFrame] | None = None,
        trigger_tf: str = "15",
    ) -> list[AggregatedSignalEvent]:
        """
        Run all 37 strategies on a symbol's data.

        Args:
            symbol: Trading symbol
            dataframes: Pre-collected data {tv_tf: DataFrame}. If None, collects from TV.
            trigger_tf: Which timeframe triggers strategy evaluation (default M15)

        Returns:
            List of AggregatedSignalEvents (0-2: one from EA, one from ICT)
        """
        # Collect data if not provided
        if dataframes is None:
            dataframes = self.collect_data(symbol)

        if not dataframes:
            return []

        # Build data store
        self._build_data_store(symbol, dataframes)

        # Get trigger timeframe DataFrame
        trigger_df = dataframes.get(trigger_tf)
        if trigger_df is None or trigger_df.empty:
            return []

        # Build BarEvent from the last completed bar
        tf_enum = _TV_TO_TF.get(trigger_tf, TimeFrame.M15)
        last_row = trigger_df.iloc[-1]
        last_ts = trigger_df.index[-1]
        if not isinstance(last_ts, datetime):
            last_ts = pd.Timestamp(last_ts)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)

        bar_event = BarEvent(
            symbol=symbol,
            timeframe=tf_enum,
            timestamp=last_ts,
            open=float(last_row["open"]),
            high=float(last_row["high"]),
            low=float(last_row["low"]),
            close=float(last_row["close"]),
            volume=float(last_row.get("volume", 0)),
            spread=float(last_row.get("spread", 0)),
        )

        results: list[AggregatedSignalEvent] = []

        # EA Engine (33 strategies with cluster voting)
        if self._ea_engine is not None:
            try:
                ea_signal = self._ea_engine.process_bar(bar_event)
                if ea_signal is not None:
                    results.append(ea_signal)
                    strat_count = ea_signal.strategy_count if hasattr(ea_signal, 'strategy_count') else 0
                    print(
                        f"  [EA] {symbol}: {ea_signal.direction.value} "
                        f"Score={ea_signal.final_score:.0f} Grade={ea_signal.grade.value} "
                        f"({strat_count} strategies fired)",
                        flush=True,
                    )
            except Exception as e:
                logger.warning(f"[EA] {symbol} error: {e}")

        # ICT Engine (4 strategies)
        if self._ict_engine is not None:
            try:
                ict_signal = self._ict_engine.process_bar(bar_event)
                if ict_signal is not None:
                    # If EA also fired same direction, keep higher score
                    if results and results[0].direction == ict_signal.direction:
                        if ict_signal.final_score > results[0].final_score:
                            results[0] = ict_signal
                    else:
                        results.append(ict_signal)

                    strat_count = ict_signal.strategy_count if hasattr(ict_signal, 'strategy_count') else 0
                    print(
                        f"  [ICT-E] {symbol}: {ict_signal.direction.value} "
                        f"Score={ict_signal.final_score:.0f} Grade={ict_signal.grade.value} "
                        f"({strat_count} strategies fired)",
                        flush=True,
                    )
            except Exception as e:
                logger.warning(f"[ICT-E] {symbol} error: {e}")

        return results

    def process_watchlist(
        self,
        symbols: list[str] | None = None,
    ) -> dict[str, list[AggregatedSignalEvent]]:
        """
        Run all strategies on all watchlist symbols.

        Returns:
            {symbol: [AggregatedSignalEvent, ...], ...}
        """
        symbols = symbols or self.config.watchlist
        results: dict[str, list[AggregatedSignalEvent]] = {}

        for symbol in symbols:
            print(f"[ENGINE] Processing {symbol}...", flush=True)
            t0 = time.time()
            signals = self.process_symbol(symbol)
            elapsed = time.time() - t0
            results[symbol] = signals
            print(f"[ENGINE] {symbol}: {len(signals)} signals [{elapsed:.1f}s]", flush=True)

        return results


# ---------------------------------------------------------------------------
# Signal to TradeDecision converter
# ---------------------------------------------------------------------------

def signal_to_decision(signal: AggregatedSignalEvent) -> dict:
    """Convert an AggregatedSignalEvent to a dict compatible with TradeDecision."""
    return {
        "action": "BUY" if signal.direction == Direction.BULLISH else "SELL",
        "symbol": signal.symbol,
        "entry_price": signal.entry_price,
        "sl_price": signal.sl_price,
        "tp_price": signal.tp_price,
        "confidence": int(signal.final_score),
        "risk_pct": signal.risk_pct,
        "grade": signal.grade.value,
        "ict_score": signal.final_score,
        "reasoning": (
            f"EA ensemble: {signal.strategy_count} strategies, "
            f"clusters: {list(signal.cluster_scores.keys()) if hasattr(signal, 'cluster_scores') else []}"
        ),
        "model_used": signal.engine.value,
    }


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    engine = StrategyEngine()
    print(f"\nProcessing BTCUSD with all strategies...\n")

    signals = engine.process_symbol("BTCUSD")

    if signals:
        for sig in signals:
            print(f"\nSignal: {sig.direction.value} {sig.symbol}")
            print(f"  Score: {sig.final_score:.0f} | Grade: {sig.grade.value}")
            print(f"  Entry: {sig.entry_price} | SL: {sig.sl_price} | TP: {sig.tp_price}")
            print(f"  Strategies: {sig.strategy_count}")
            d = signal_to_decision(sig)
            print(f"  Decision: {json.dumps(d, indent=2)}")
    else:
        print("No signals from any strategy.")
