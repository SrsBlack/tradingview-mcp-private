"""
Data adapter: TradingView MCP JSON → pandas DataFrame (trading-ai-v2 compatible).

TradingView OHLCV format:
    {"bars": [{"time": 1775523600, "open": 24132.4, "high": 24141.2,
               "low": 23998.9, "close": 24008.4, "volume": 13515}, ...]}

trading-ai-v2 expects:
    pandas DataFrame with:
    - UTC DatetimeIndex (timezone-aware)
    - Columns: open, high, low, close, volume (float64)
    - Optional: spread (defaults to 0.0)

Usage:
    from bridge.tv_data_adapter import bars_to_dataframe, parse_pine_levels
    df = bars_to_dataframe(ohlcv_response)
    levels = parse_pine_levels(pine_lines_response)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# OHLCV conversion
# ---------------------------------------------------------------------------

def bars_to_dataframe(ohlcv_response: dict[str, Any]) -> pd.DataFrame:
    """
    Convert TradingView MCP OHLCV response to a pandas DataFrame.

    Args:
        ohlcv_response: Output from tv_client.get_ohlcv()
            Must contain "bars" key with list of {time, open, high, low, close, volume}.

    Returns:
        DataFrame with UTC DatetimeIndex and float64 OHLCV columns.
        Sorted by timestamp ascending. Empty DataFrame if no bars.
    """
    bars = ohlcv_response.get("bars", [])
    if not bars:
        return _empty_df()

    # Extract arrays
    times = [b["time"] for b in bars]
    opens = [float(b["open"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    closes = [float(b["close"]) for b in bars]
    volumes = [float(b.get("volume", 0)) for b in bars]

    # Build UTC DatetimeIndex from Unix timestamps
    index = pd.to_datetime(times, unit="s", utc=True)

    df = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "spread": 0.0,  # Not available from TradingView; default to 0
        },
        index=index,
        dtype=np.float64,
    )

    # Ensure sorted ascending by time
    df.sort_index(inplace=True)

    # Remove duplicate timestamps (can happen on TradingView chart transitions)
    df = df[~df.index.duplicated(keep="last")]

    return df


def _empty_df() -> pd.DataFrame:
    """Return an empty DataFrame with the expected schema."""
    return pd.DataFrame(
        columns=["open", "high", "low", "close", "volume", "spread"],
        dtype=np.float64,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_dataframe(df: pd.DataFrame, min_bars: int = 20) -> tuple[bool, str]:
    """
    Validate that a DataFrame is suitable for ICT analysis.

    Returns:
        (is_valid, reason) — True if OK, False with explanation if not.
    """
    if df.empty:
        return False, "Empty DataFrame"
    if len(df) < min_bars:
        return False, f"Only {len(df)} bars (need {min_bars}+)"
    if not isinstance(df.index, pd.DatetimeIndex):
        return False, "Index is not DatetimeIndex"
    if df.index.tz is None:
        return False, "Index is not timezone-aware (need UTC)"

    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        return False, f"Missing columns: {missing}"

    # Check for NaN in price columns
    price_cols = ["open", "high", "low", "close"]
    nan_count = df[price_cols].isna().sum().sum()
    if nan_count > 0:
        return False, f"{nan_count} NaN values in price columns"

    # Check timestamp freshness (last bar should be within last 24 hours for active charts)
    last_bar_time = df.index[-1].to_pydatetime()
    age_hours = (datetime.now(timezone.utc) - last_bar_time).total_seconds() / 3600
    if age_hours > 48:
        return False, f"Data is {age_hours:.0f}h old (stale?)"

    return True, "OK"


# ---------------------------------------------------------------------------
# Pine indicator level parsing
# ---------------------------------------------------------------------------

def parse_pine_levels(pine_lines_response: dict[str, Any]) -> list[dict]:
    """
    Parse horizontal price levels from Pine indicator drawings.

    Args:
        pine_lines_response: Output from tv_client.get_pine_lines()

    Returns:
        List of {"price": float, "indicator": str, "label": str or None}
        sorted by price descending (highest first).
    """
    levels: list[dict] = []

    studies = pine_lines_response.get("studies", [])
    for study in studies:
        indicator_name = study.get("name", "Unknown")
        for line in study.get("lines", []):
            price = line.get("price")
            if price is not None:
                levels.append({
                    "price": float(price),
                    "indicator": indicator_name,
                    "label": line.get("label"),
                })

    # Sort highest to lowest
    levels.sort(key=lambda x: x["price"], reverse=True)
    return levels


def parse_pine_boxes(pine_boxes_response: dict[str, Any]) -> list[dict]:
    """
    Parse price zone boxes from Pine indicator drawings (e.g., FVG zones).

    Args:
        pine_boxes_response: Output from tv_client.get_pine_boxes()

    Returns:
        List of {"high": float, "low": float, "indicator": str, "mid": float}
        sorted by mid price descending.
    """
    zones: list[dict] = []

    studies = pine_boxes_response.get("studies", [])
    for study in studies:
        indicator_name = study.get("name", "Unknown")
        for box in study.get("boxes", []):
            high = box.get("high")
            low = box.get("low")
            if high is not None and low is not None:
                zones.append({
                    "high": float(high),
                    "low": float(low),
                    "mid": (float(high) + float(low)) / 2.0,
                    "indicator": indicator_name,
                })

    zones.sort(key=lambda x: x["mid"], reverse=True)
    return zones


def parse_pine_labels(pine_labels_response: dict[str, Any]) -> list[dict]:
    """
    Parse text annotations from Pine indicator labels.

    Args:
        pine_labels_response: Output from tv_client.get_pine_labels()

    Returns:
        List of {"text": str, "price": float, "indicator": str}
    """
    labels: list[dict] = []

    studies = pine_labels_response.get("studies", [])
    for study in studies:
        indicator_name = study.get("name", "Unknown")
        for label in study.get("labels", []):
            text = label.get("text", "")
            price = label.get("price")
            if price is not None:
                labels.append({
                    "text": text,
                    "price": float(price),
                    "indicator": indicator_name,
                })

    return labels


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Test with sample TradingView data
    sample = {
        "success": True,
        "bar_count": 5,
        "bars": [
            {"time": 1775523600, "open": 24132.4, "high": 24141.2, "low": 23998.9, "close": 24008.4, "volume": 13515},
            {"time": 1775527200, "open": 24008.9, "high": 24051.3, "low": 23983.1, "close": 24041.4, "volume": 11517},
            {"time": 1775530800, "open": 24041.9, "high": 24061.4, "low": 24022.5, "close": 24050.4, "volume": 8212},
            {"time": 1775534400, "open": 24050.9, "high": 24093.6, "low": 24028.0, "close": 24076.5, "volume": 8967},
            {"time": 1775538000, "open": 24077.3, "high": 24088.8, "low": 24070.8, "close": 24083.7, "volume": 2709},
        ]
    }

    df = bars_to_dataframe(sample)
    print(f"DataFrame shape: {df.shape}")
    print(f"Index type: {type(df.index).__name__}, tz: {df.index.tz}")
    print(f"Columns: {list(df.columns)}")
    print(f"Dtypes:\n{df.dtypes}")
    print(f"\nData:\n{df}")

    valid, reason = validate_dataframe(df, min_bars=3)
    print(f"\nValid: {valid} ({reason})")
