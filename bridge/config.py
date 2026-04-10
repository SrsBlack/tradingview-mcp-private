"""
Unified configuration — merges rules.json (TradingView MCP) with config.yaml (trading-ai-v2).

Usage:
    from bridge.config import get_bridge_config
    cfg = get_bridge_config()
    cfg.watchlist          # ["BTCUSD", "ETHUSD", ...]
    cfg.symbol_map         # {"US100.cash": "NAS100", ...}
    cfg.ict_config         # ICTConfig from trading-ai-v2
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

MCP_ROOT = Path(__file__).resolve().parent.parent
TRADING_AI_V2 = Path.home() / "Desktop" / "trading-ai-v2"
RULES_JSON = MCP_ROOT / "rules.json"
CONFIG_YAML = TRADING_AI_V2 / "config.yaml"

# ---------------------------------------------------------------------------
# Add trading-ai-v2 to sys.path (isolate to avoid core/ collision)
# ---------------------------------------------------------------------------

_TRADING_AI_ON_PATH = False


def ensure_trading_ai_path() -> bool:
    """Add trading-ai-v2 to sys.path if it exists. Returns True if available."""
    global _TRADING_AI_ON_PATH
    if _TRADING_AI_ON_PATH:
        return True
    if TRADING_AI_V2.exists() and (TRADING_AI_V2 / "analysis").exists():
        # Insert at position 1 (after '' but before other paths) to avoid
        # shadowing the bridge package itself.
        path_str = str(TRADING_AI_V2)
        if path_str not in sys.path:
            sys.path.insert(1, path_str)
        _TRADING_AI_ON_PATH = True
        return True
    return False


# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------

# TradingView name → MT5 symbol name (FTMO naming)
# TV uses exchange-prefixed names like "BITSTAMP:BTCUSD" — strip prefix first,
# then look up the MT5 equivalent.
SYMBOL_MAP: dict[str, str] = {
    # Forex
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    # Gold / commodities — FTMO uses XAUUSD and UKOIL
    "XAUUSD": "XAUUSD",
    "XAGUSD": "XAGUSD",
    "UKOIL":  "UKOIL",
    # Dow Jones futures — FTMO calls it US30
    "YM1!":   "US30",
    "YM":     "US30",
    # S&P 500 E-mini — FTMO calls it US500
    "ES1!":   "US500",
    "ES":     "US500",
    # Nasdaq 100 E-mini — FTMO calls it US100
    "NQ1!":   "US100",
    "NQ":     "US100",
    "NAS100": "US100",
    "US100":  "US100",
    "SPX500": "US500",
    "US500":  "US500",
    # Crypto — FTMO doesn't offer crypto; will be paper-only
    "BTCUSD":  "BTCUSD",
    "ETHUSD":  "ETHUSD",
    "SOLUSD":  "SOLUSD",
}

# Reverse: trading-ai-v2 name → TradingView name (for chart switching)
REVERSE_SYMBOL_MAP: dict[str, str] = {v: k for k, v in SYMBOL_MAP.items()}

# SMT correlated pairs (mirrored from analysis/smt.py, extended for crypto)
SMT_PAIRS: dict[str, str] = {
    "US500.cash": "US100.cash",
    "US100.cash": "US500.cash",
    "ES1!": "NQ1!",
    "NQ1!": "ES1!",
    "US500": "US100",
    "US100": "US500",
    "EURUSD": "GBPUSD",
    "GBPUSD": "EURUSD",
    "XAUUSD": "XAGUSD",
    "XAGUSD": "XAUUSD",
    "BTCUSD": "ETHUSD",
    "ETHUSD": "BTCUSD",
    "SOLUSD": "BTCUSD",
}

# Timeframe strings: TV CLI → trading-ai-v2 names
TF_MAP: dict[str, str] = {
    "1":   "M1",
    "5":   "M5",
    "15":  "M15",
    "30":  "M30",
    "60":  "H1",
    "240": "H4",
    "D":   "D1",
    "W":   "W1",
}

TF_REVERSE: dict[str, str] = {v: k for k, v in TF_MAP.items()}


# ---------------------------------------------------------------------------
# Price range validation — single source of truth for contamination detection
# ---------------------------------------------------------------------------
# Keys are base symbol names (no exchange prefix).
# Range: (floor, ceiling). Upper bound = realistic ATH * ~1.2 headroom.
# Reject any price outside this range as contamination from another symbol.

PRICE_RANGES: dict[str, tuple[float, float]] = {
    "BTCUSD":  (10_000, 200_000),  # BTC ATH ~109k; 200k gives headroom
    "ETHUSD":  (100,    10_000),   # ETH ATH ~4,800; 10k gives headroom, rejects 47k contamination
    "SOLUSD":  (1,      1_000),    # SOL ATH ~260; 1k gives headroom, rejects 70k contamination
    "EURUSD":  (0.80,   1.60),     # EUR/USD never outside 0.82–1.60 in modern history
    "GBPUSD":  (1.00,   2.00),     # GBP/USD realistic range
    "YM1!":    (10_000, 50_000),   # Dow futures; ATH ~45k
    "ES1!":    (2_000,  7_000),    # S&P 500 E-mini futures; ATH ~6,100
    "NQ1!":    (8_000,  25_000),   # Nasdaq 100 E-mini futures; ATH ~22,200
    "US500":   (2_000,  7_000),    # S&P 500 CFD (same range as ES)
    "US100":   (8_000,  25_000),   # Nasdaq 100 CFD (same range as NQ)
    "XAUUSD":  (1_000,  6_000),    # Gold spot confirmed ~4,767 Apr 2026; 6k gives headroom
    "UKOIL":   (10,     150),      # Brent crude realistic range
}


def price_in_range(symbol: str, price: float) -> bool:
    """Check if price is within valid range for symbol. Returns True if valid or unknown symbol."""
    base = symbol.split(":")[-1]
    rng = PRICE_RANGES.get(base)
    if rng is None or price <= 0:
        return True  # unknown symbol — don't block
    lo, hi = rng
    return lo <= price <= hi


# ---------------------------------------------------------------------------
# BridgeConfig
# ---------------------------------------------------------------------------

@dataclass
class BridgeConfig:
    """Merged configuration from rules.json + config.yaml."""

    # Watchlist (from rules.json)
    watchlist: list[str] = field(default_factory=lambda: ["BTCUSD", "ETHUSD", "SOLUSD"])
    default_timeframe: str = "240"  # TradingView resolution string

    # Analysis timeframes (multi-TF pipeline)
    htf: str = "240"     # H4 — higher timeframe bias
    itf: str = "60"      # H1 — intermediate
    ltf: str = "15"      # M15 — trigger / entry

    # OHLCV bar counts per timeframe
    bar_counts: dict[str, int] = field(default_factory=lambda: {
        "240": 200,  # H4: 200 bars ≈ 800 hours ≈ 33 days
        "60":  200,  # H1: 200 bars ≈ 200 hours ≈ 8 days
        "15":  200,  # M15: 200 bars ≈ 50 hours ≈ 2 days
        "5":   100,  # M5:  100 bars ≈ 8 hours (Silver Bullet)
    })

    # Rules from rules.json
    bias_criteria: dict[str, list[str]] = field(default_factory=dict)
    risk_rules: list[str] = field(default_factory=list)
    strategy_ensemble: list[dict] = field(default_factory=list)

    # Grade thresholds (ICT score cutoffs for A/B/C/D)
    grade_thresholds: dict[str, int] = field(default_factory=lambda: {
        "A": 80, "B": 65, "C": 50, "D": 35
    })

    # Flags
    has_trading_ai: bool = False

    @property
    def symbol_map(self) -> dict[str, str]:
        return SYMBOL_MAP

    @property
    def smt_pairs(self) -> dict[str, str]:
        return SMT_PAIRS

    def tv_symbol(self, symbol: str) -> str:
        """Convert internal symbol name to TradingView name."""
        return REVERSE_SYMBOL_MAP.get(symbol, symbol)

    def internal_symbol(self, tv_symbol: str) -> str:
        """Convert TradingView symbol name to internal name."""
        # Strip exchange prefix if present (e.g., "BITSTAMP:BTCUSD" → "BTCUSD")
        clean = tv_symbol.split(":")[-1] if ":" in tv_symbol else tv_symbol
        return SYMBOL_MAP.get(clean, clean)

    def tv_timeframe(self, internal_tf: str) -> str:
        """Convert internal timeframe (H4) to TradingView string (240)."""
        return TF_REVERSE.get(internal_tf, internal_tf)

    def internal_timeframe(self, tv_tf: str) -> str:
        """Convert TradingView timeframe (240) to internal name (H4)."""
        return TF_MAP.get(tv_tf, tv_tf)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _load_rules() -> dict[str, Any]:
    """Load rules.json."""
    if RULES_JSON.exists():
        with open(RULES_JSON) as f:
            return json.load(f)
    return {}


def get_bridge_config() -> BridgeConfig:
    """Build merged BridgeConfig from all sources."""
    rules = _load_rules()
    has_tai = ensure_trading_ai_path()

    default_thresholds = {"A": 80, "B": 65, "C": 50, "D": 35}
    return BridgeConfig(
        watchlist=rules.get("watchlist", ["BTCUSD", "ETHUSD", "SOLUSD"]),
        default_timeframe=rules.get("default_timeframe", "240"),
        bias_criteria=rules.get("bias_criteria", {}),
        risk_rules=rules.get("risk_rules", []),
        strategy_ensemble=rules.get("strategy_ensemble", []),
        grade_thresholds={**default_thresholds, **rules.get("grade_thresholds", {})},
        has_trading_ai=has_tai,
    )


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg = get_bridge_config()
    print(f"Watchlist: {cfg.watchlist}")
    print(f"Has trading-ai-v2: {cfg.has_trading_ai}")
    print(f"HTF: {cfg.htf} ({cfg.internal_timeframe(cfg.htf)})")
    print(f"Symbol map sample: BTCUSD -> {cfg.internal_symbol('BTCUSD')}")
    print(f"SMT pair for EURUSD: {cfg.smt_pairs.get('EURUSD')}")
