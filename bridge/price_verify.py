"""
Independent price verification via MT5 (FTMO) + Alpaca + Finnhub.

Cross-checks TradingView prices against external live feeds to catch
contamination before it reaches trade decisions.

Priority order:
1. MT5/FTMO: forex, indices, gold, oil (most accurate — same broker we trade on)
2. Alpaca: crypto (BTC, ETH, SOL)
3. Finnhub: US indices via ETF proxies (SPY→US500, QQQ→US100) as fallback

Usage:
    from bridge.price_verify import PriceVerifier
    verifier = PriceVerifier()
    ok, external_price = verifier.verify("BTCUSD", tv_price=72000.0)
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    import MetaTrader5 as mt5
    _HAS_MT5 = True
except ImportError:
    _HAS_MT5 = False

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Alpaca data endpoints
_CRYPTO_URL = "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes"

# Map TradingView symbol names to Alpaca symbol format
_TV_TO_ALPACA: dict[str, str] = {
    "BTCUSD":  "BTC/USD",
    "ETHUSD":  "ETH/USD",
    "SOLUSD":  "SOL/USD",
}

# Map TradingView base symbols to Finnhub ETF proxies + conversion factor
# US500 ≈ SPY * 10 (SPY is S&P 500 / 10), US100 ≈ QQQ * 30 (approximate)
_TV_TO_FINNHUB: dict[str, tuple[str, float]] = {
    "US500": ("SPY", 10.0),   # SPY ≈ S&P 500 / 10
    "US100": ("QQQ", 30.0),   # QQQ ≈ Nasdaq 100 / 30
}

# TV base symbol → MT5/FTMO symbol name
# FTMO uses .cash suffix for CFDs (indices, commodities)
_TV_TO_MT5: dict[str, str] = {
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "XAUUSD": "XAUUSD",
    "UKOIL":  "UKOIL.cash",
    "YM1!":   "US30.cash",
    "US500":  "US500.cash",
    "US100":  "US100.cash",
    "US30":   "US30.cash",
}

# Maximum allowed deviation between TV and external price (as fraction of price).
# If TV price differs by more than this, it's likely contaminated.
_MAX_DEVIATION = 0.02

# MT5/FTMO is the actual broker — tighter tolerance (1%)
_MAX_MT5_DEVIATION = 0.01

# Per-symbol MT5 deviation overrides (some instruments have structural price differences)
# UKOIL: TradingView shows ICE Brent front-month, FTMO uses a CFD with basis spread (~2-3%)
_MT5_DEVIATION_OVERRIDES: dict[str, float] = {
    "UKOIL": 0.035,  # 3.5% — Brent CFD vs futures basis
}

# Finnhub ETF proxies are approximate — allow 5% for the conversion factor drift
_MAX_FINNHUB_DEVIATION = 0.05


class PriceVerifier:
    """Cross-check TradingView prices against MT5/FTMO, Alpaca, and Finnhub."""

    def __init__(self):
        self._api_key = os.environ.get("ALPACA_API_KEY", "")
        self._secret = os.environ.get("ALPACA_SECRET_KEY", "")
        self._finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
        self._headers = {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._secret,
        }
        self._cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, timestamp)
        self._cache_ttl = 30.0  # seconds
        self._enabled = bool(self._api_key and self._secret and _HAS_REQUESTS)
        self._finnhub_enabled = bool(self._finnhub_key and _HAS_REQUESTS)

        # MT5 — try to initialize if available
        self._mt5_enabled = False
        if _HAS_MT5:
            try:
                if not mt5.initialize():
                    mt5.initialize()
                # Test with a quick symbol check
                info = mt5.symbol_info("EURUSD")
                if info is not None:
                    self._mt5_enabled = True
                    print("[PRICE_VERIFY] MT5/FTMO connected — primary verification for forex/indices/commodities", flush=True)
                else:
                    print("[PRICE_VERIFY] MT5 initialized but no symbol data available", flush=True)
            except Exception as e:
                print(f"[PRICE_VERIFY] MT5 init failed: {e}", flush=True)
        else:
            print("[PRICE_VERIFY] MetaTrader5 not installed — MT5 verification disabled", flush=True)

        if not _HAS_REQUESTS:
            print("[PRICE_VERIFY] requests library not installed — API verification disabled", flush=True)
        elif not self._api_key:
            print("[PRICE_VERIFY] ALPACA_API_KEY not set — crypto verification disabled", flush=True)

        if self._finnhub_enabled:
            print("[PRICE_VERIFY] Finnhub enabled — US500/US100 index verification via ETF proxy", flush=True)

    @property
    def is_enabled(self) -> bool:
        return self._enabled or self._finnhub_enabled or self._mt5_enabled

    def get_mt5_price(self, symbol: str) -> float | None:
        """Get latest mid-price from MT5/FTMO for a symbol. Returns None if unavailable."""
        if not self._mt5_enabled:
            return None

        base = symbol.split(":")[-1]
        mt5_sym = _TV_TO_MT5.get(base)
        if not mt5_sym:
            return None

        # Check cache
        cache_key = f"mt5_{base}"
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[1]) < self._cache_ttl:
            return cached[0]

        try:
            tick = mt5.symbol_info_tick(mt5_sym)
            if tick is None:
                return None
            bid, ask = tick.bid, tick.ask
            if bid <= 0 or ask <= 0:
                return None
            mid = (bid + ask) / 2.0
            self._cache[cache_key] = (mid, time.time())
            return mid
        except Exception:
            return None

    def get_alpaca_price(self, symbol: str) -> float | None:
        """Get latest price from Alpaca for a symbol. Returns None if unavailable."""
        if not self._enabled:
            return None

        base = symbol.split(":")[-1]
        alpaca_sym = _TV_TO_ALPACA.get(base)
        if not alpaca_sym:
            return None  # Symbol not supported on Alpaca (forex, commodities)

        # Check cache
        cached = self._cache.get(base)
        if cached and (time.time() - cached[1]) < self._cache_ttl:
            return cached[0]

        try:
            resp = requests.get(
                _CRYPTO_URL,
                headers=self._headers,
                params={"symbols": alpaca_sym},
                timeout=5,
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            quote = data.get("quotes", {}).get(alpaca_sym)
            if not quote:
                return None

            # Use midpoint of bid/ask
            bid = float(quote.get("bp", 0))
            ask = float(quote.get("ap", 0))
            if bid <= 0 or ask <= 0:
                return None

            mid = (bid + ask) / 2.0
            self._cache[base] = (mid, time.time())
            return mid

        except Exception:
            return None

    def get_finnhub_price(self, symbol: str) -> float | None:
        """Get estimated index price from Finnhub via ETF proxy. Returns None if unavailable."""
        if not self._finnhub_enabled:
            return None

        base = symbol.split(":")[-1]
        proxy = _TV_TO_FINNHUB.get(base)
        if not proxy:
            return None

        etf_symbol, multiplier = proxy

        # Check cache
        cache_key = f"fh_{base}"
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[1]) < self._cache_ttl:
            return cached[0]

        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": etf_symbol, "token": self._finnhub_key},
                timeout=5,
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            etf_price = float(data.get("c", 0))
            if etf_price <= 0:
                return None

            # Convert ETF price to index price
            index_price = etf_price * multiplier
            self._cache[cache_key] = (index_price, time.time())
            return index_price

        except Exception:
            return None

    def verify(self, symbol: str, tv_price: float) -> tuple[bool, float | None]:
        """Verify a TradingView price against Alpaca or Finnhub.

        Args:
            symbol: TradingView symbol (e.g. "BITSTAMP:BTCUSD" or "CAPITALCOM:US500")
            tv_price: Price reported by TradingView

        Returns:
            (is_valid, external_price) — is_valid is True if prices match within tolerance,
            or if external data isn't available for this symbol (pass-through).
            external_price is None if not available.
        """
        if tv_price <= 0:
            return False, None

        # Priority 1: MT5/FTMO — most accurate for forex/indices/commodities
        mt5_price = self.get_mt5_price(symbol)
        if mt5_price is not None:
            base = symbol.split(":")[-1]
            max_dev = _MT5_DEVIATION_OVERRIDES.get(base, _MAX_MT5_DEVIATION)
            deviation = abs(tv_price - mt5_price) / mt5_price
            if deviation > max_dev:
                print(
                    f"[PRICE_VERIFY] MISMATCH on {base}: "
                    f"TV={tv_price:.2f}, MT5/FTMO={mt5_price:.2f}, "
                    f"deviation={deviation:.1%} (max {max_dev:.1%})",
                    flush=True,
                )
                return False, mt5_price
            return True, mt5_price

        # Priority 2: Alpaca — crypto (BTC, ETH, SOL)
        alpaca_price = self.get_alpaca_price(symbol)
        if alpaca_price is not None:
            deviation = abs(tv_price - alpaca_price) / alpaca_price
            if deviation > _MAX_DEVIATION:
                base = symbol.split(":")[-1]
                print(
                    f"[PRICE_VERIFY] MISMATCH on {base}: "
                    f"TV={tv_price:.2f}, Alpaca={alpaca_price:.2f}, "
                    f"deviation={deviation:.1%} (max {_MAX_DEVIATION:.0%})",
                    flush=True,
                )
                return False, alpaca_price
            return True, alpaca_price

        # Priority 3: Finnhub — US indices via ETF proxy (fallback if MT5 down)
        finnhub_price = self.get_finnhub_price(symbol)
        if finnhub_price is not None:
            deviation = abs(tv_price - finnhub_price) / finnhub_price
            if deviation > _MAX_FINNHUB_DEVIATION:
                base = symbol.split(":")[-1]
                print(
                    f"[PRICE_VERIFY] MISMATCH on {base}: "
                    f"TV={tv_price:.2f}, Finnhub(proxy)={finnhub_price:.2f}, "
                    f"deviation={deviation:.1%} (max {_MAX_FINNHUB_DEVIATION:.0%})",
                    flush=True,
                )
                return False, finnhub_price
            return True, finnhub_price

        # Can't verify — pass through
        return True, None

    def verify_batch(self, prices: dict[str, float]) -> dict[str, tuple[bool, float | None]]:
        """Verify multiple prices at once. Fetches all Alpaca prices in one call.

        Args:
            prices: {"BTCUSD": 72000.0, "ETHUSD": 2180.0, ...}

        Returns:
            {"BTCUSD": (True, 71800.0), "ETHUSD": (True, 2185.0), ...}
        """
        results: dict[str, tuple[bool, float | None]] = {}

        # Batch crypto via Alpaca
        if self._enabled:
            alpaca_syms = []
            sym_map = {}
            for sym in prices:
                base = sym.split(":")[-1]
                alpaca_sym = _TV_TO_ALPACA.get(base)
                if alpaca_sym:
                    alpaca_syms.append(alpaca_sym)
                    sym_map[alpaca_sym] = sym

            if alpaca_syms:
                try:
                    resp = requests.get(
                        _CRYPTO_URL,
                        headers=self._headers,
                        params={"symbols": ",".join(alpaca_syms)},
                        timeout=5,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        for alpaca_sym, quote in data.get("quotes", {}).items():
                            tv_sym = sym_map.get(alpaca_sym)
                            if not tv_sym:
                                continue
                            bid = float(quote.get("bp", 0))
                            ask = float(quote.get("ap", 0))
                            if bid > 0 and ask > 0:
                                mid = (bid + ask) / 2.0
                                base = tv_sym.split(":")[-1]
                                self._cache[base] = (mid, time.time())
                                tv_price = prices[tv_sym]
                                deviation = abs(tv_price - mid) / mid if mid > 0 else 0
                                is_valid = deviation <= _MAX_DEVIATION
                                if not is_valid:
                                    print(
                                        f"[PRICE_VERIFY] MISMATCH on {base}: "
                                        f"TV={tv_price:.2f}, Alpaca={mid:.2f}, "
                                        f"deviation={deviation:.1%}",
                                        flush=True,
                                    )
                                results[tv_sym] = (is_valid, mid)
                except Exception:
                    pass

        # Check remaining via Finnhub (indices)
        for sym in prices:
            if sym not in results:
                ok, ext_price = self.verify(sym, prices[sym])
                results[sym] = (ok, ext_price)

        # Fill in symbols we couldn't verify
        for sym in prices:
            if sym not in results:
                results[sym] = (True, None)

        return results
