"""Symbol normalization for consistent reporting and storage.

TradingView uses broker-prefixed symbols (e.g. "BITSTAMP:BTCUSD",
"COINBASE:SOLUSD", "OANDA:XAUUSD", "TVC:UKOIL"). MT5 and internal logic
use the bare instrument (e.g. "BTCUSD"). Reports must dedupe these.
"""
from __future__ import annotations

_KNOWN_PREFIXES = (
    "BITSTAMP:", "COINBASE:", "OANDA:", "TVC:", "FX:",
    "NYMEX:", "CBOT:", "CME:", "NASDAQ:", "NYSE:",
)


def normalize_symbol(symbol: str | None) -> str:
    """Return the bare instrument for a TradingView-prefixed symbol.

    >>> normalize_symbol("BITSTAMP:BTCUSD")
    'BTCUSD'
    >>> normalize_symbol("COINBASE:SOLUSD")
    'SOLUSD'
    >>> normalize_symbol("BTCUSD")
    'BTCUSD'
    >>> normalize_symbol(None)
    ''
    """
    if not symbol:
        return ""
    sym = symbol.strip().upper()
    for p in _KNOWN_PREFIXES:
        if sym.startswith(p):
            return sym[len(p):]
    if ":" in sym:
        return sym.split(":", 1)[1]
    return sym
