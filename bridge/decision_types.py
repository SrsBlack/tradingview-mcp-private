"""
Typed dataclasses for trade decisions flowing through the bridge pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


@dataclass
class TradeDecision:
    """Output from the Claude decision layer."""
    action: str  # "BUY", "SELL", "SKIP", "WAIT"
    symbol: str
    entry_price: float = 0.0
    sl_price: float = 0.0
    tp_price: float = 0.0
    tp2_price: float = 0.0
    partial_close_pct: float = 0.5
    trade_type: str = "intraday"  # "swing" or "intraday"
    confidence: int = 0       # 0-100
    risk_pct: float = 0.0     # e.g., 0.01 = 1%
    reasoning: str = ""
    grade: str = ""
    ict_score: float = 0.0
    model_used: str = ""      # which Claude model made the decision
    decided_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_trade(self) -> bool:
        return self.action in ("BUY", "SELL")

    @property
    def risk_reward_ratio(self) -> float:
        if not self.is_trade or self.sl_price == 0 or self.entry_price == 0:
            return 0.0
        risk = abs(self.entry_price - self.sl_price)
        if risk == 0:
            return 0.0
        reward = abs(self.tp_price - self.entry_price)
        return round(reward / risk, 2)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "tp2_price": self.tp2_price,
            "partial_close_pct": self.partial_close_pct,
            "trade_type": self.trade_type,
            "confidence": self.confidence,
            "risk_pct": self.risk_pct,
            "risk_reward_ratio": self.risk_reward_ratio,
            "reasoning": self.reasoning,
            "grade": self.grade,
            "ict_score": self.ict_score,
            "model_used": self.model_used,
            "decided_at": self.decided_at,
        }


@dataclass
class PaperPosition:
    """An open paper trading position."""
    ticket: int
    symbol: str
    direction: str           # "BUY" or "SELL"
    entry_price: float
    sl_price: float
    tp_price: float
    lot_size: float
    risk_pct: float
    opened_at: str
    ict_grade: str = ""
    ict_score: float = 0.0
    reasoning: str = ""
    tp2_price: float = 0.0
    trade_type: str = "intraday"

    # Mutable state
    current_price: float = 0.0
    floating_pnl: float = 0.0
    trailing_sl: float = 0.0  # Updated as price moves in favor
    partial_closed: bool = False
    tp1_hit: bool = False

    @property
    def r_multiple(self) -> float:
        """Current R-multiple (profit in units of initial risk)."""
        if self.sl_price == 0 or self.entry_price == 0:
            return 0.0
        risk = abs(self.entry_price - self.sl_price)
        if risk == 0:
            return 0.0
        if self.direction == "BUY":
            return (self.current_price - self.entry_price) / risk
        else:
            return (self.entry_price - self.current_price) / risk

    def to_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "tp2_price": self.tp2_price,
            "lot_size": self.lot_size,
            "current_price": self.current_price,
            "floating_pnl": round(self.floating_pnl, 2),
            "r_multiple": round(self.r_multiple, 2),
            "trailing_sl": self.trailing_sl,
            "opened_at": self.opened_at,
            "ict_grade": self.ict_grade,
            "tp1_hit": self.tp1_hit,
        }


@dataclass
class ClosedPosition:
    """A closed paper trading position."""
    ticket: int
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    sl_price: float
    tp_price: float
    lot_size: float
    pnl: float
    r_multiple: float
    opened_at: str
    closed_at: str
    close_reason: str  # "TP", "SL", "TRAILING_SL", "MANUAL"
    ict_grade: str = ""
    ict_score: float = 0.0
