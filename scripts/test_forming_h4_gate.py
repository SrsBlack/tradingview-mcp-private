"""Standalone tests for the forming-H4 pre-gate.

Constructs a minimal SymbolAnalysis-like stub and exercises
ClaudeDecisionMaker._pre_gate against six scenarios:

  1. BUY + bearish forming H4 + range >= 0.5*ATR  -> BLOCKED
  2. SELL + bullish forming H4 + range >= 0.5*ATR -> BLOCKED
  3. BUY + bullish forming H4 (matching)          -> PASS
  4. SELL + bearish forming H4 (matching)         -> PASS
  5. BUY + bearish forming but small range        -> PASS (indecisive)
  6. ATR_h4 = 0 (no data)                          -> PASS (gate inactive)

Run:
  PYTHONUTF8=1 python scripts/test_forming_h4_gate.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

_BRIDGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BRIDGE))
sys.path.insert(1, str(Path("C:/Users/User/Desktop/trading-ai-v2")))

from bridge.claude_decision import ClaudeDecisionMaker  # noqa: E402


@dataclass
class _StubAnalysis:
    symbol: str = "BITSTAMP:BTCUSD"
    error: str | None = None
    grade: str = "A"
    total_score: float = 90.0
    current_price: float = 100.0
    direction: str = "BULLISH"
    atr_m15: float = 0.5
    forming_h4_open: float = 0.0
    forming_h4_high: float = 0.0
    forming_h4_low: float = 0.0
    forming_h4_close: float = 0.0
    atr_h4: float = 0.0
    advanced_factors: list[str] = field(default_factory=list)
    displacement_confirmed: bool = True
    sweep_detected: bool = True
    htf_analysis: object = None
    w1_bias: str = "BULLISH"
    d1_bias: str = "BULLISH"
    h1_bias: str = "BULLISH"
    h4_bias: str = "BULLISH"
    pd_zone: str = "discount"
    pd_aligned: bool = True
    is_kill_zone: bool = True
    session_type: str = "LONDON_OPEN"
    is_macro_time: bool = True
    fib_tp_levels: list[float] = field(default_factory=lambda: [105.0, 110.0])
    key_opens: dict = field(default_factory=lambda: {"D_OPEN": 99.0})
    htf_pd_aligned: bool = True
    htf_fvg_obstacle: bool = False
    intermarket_conflict: bool = False
    intermarket_explanation: str = ""
    is_silver_bullet: bool = False
    score_breakdown: object = None
    sweep_count: int = 1
    structure_score: float = 25.0
    judas_swing_detected: bool = False
    judas_direction: str = ""
    has_cisd: bool = False
    htf_fvg_obstacle_zone: str = ""
    range_high: float = 105.0
    range_low: float = 95.0
    open_position_count: int = 0
    mtf_aligned: bool = True
    mtf_alignment: str = "W1:BULLISH D1:BULLISH H4:BULLISH"
    confluence_factors: list = field(default_factory=list)


_failures: list[str] = []


def _check(name: str, actual_blocked: bool, expected_blocked: bool, reason: str | None) -> None:
    ok = actual_blocked == expected_blocked
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}: blocked={actual_blocked}  reason={reason}")
    if not ok:
        _failures.append(name)


def run():
    dm = ClaudeDecisionMaker()

    print("Forming-H4 gate tests:")

    # Case 1: BUY + bearish forming H4, range = 1.0 ATR -> BLOCK
    a = _StubAnalysis(
        direction="BULLISH",
        forming_h4_open=100.0, forming_h4_high=100.5, forming_h4_low=99.0,
        forming_h4_close=99.2,  # bearish bar (close < open)
        atr_h4=1.5,             # range = 1.5 = 1.0*ATR — meaningful
    )
    r = dm._pre_gate(a)
    blocked = r is not None and "FORMING-H4" in r
    _check("BUY blocked by bearish forming H4", blocked, True, r)

    # Case 2: SELL + bullish forming H4 -> BLOCK
    a = _StubAnalysis(
        direction="BEARISH",
        forming_h4_open=100.0, forming_h4_high=101.5, forming_h4_low=99.8,
        forming_h4_close=101.2,  # bullish bar
        atr_h4=1.5,
        # Disable other gates for SELL setup
        pd_zone="premium", w1_bias="BEARISH", d1_bias="BEARISH", h1_bias="BEARISH", h4_bias="BEARISH",
    )
    r = dm._pre_gate(a)
    blocked = r is not None and "FORMING-H4" in r
    _check("SELL blocked by bullish forming H4", blocked, True, r)

    # Case 3: BUY + bullish forming H4 (matching) -> PASS
    a = _StubAnalysis(
        direction="BULLISH",
        forming_h4_open=100.0, forming_h4_high=101.5, forming_h4_low=99.8,
        forming_h4_close=101.2,  # bullish bar (matches BUY)
        atr_h4=1.5,
    )
    r = dm._pre_gate(a)
    blocked = r is not None and "FORMING-H4" in r
    _check("BUY passes when forming H4 is bullish", blocked, False, r)

    # Case 4: SELL + bearish forming H4 (matching) -> PASS
    a = _StubAnalysis(
        direction="BEARISH",
        forming_h4_open=100.0, forming_h4_high=100.5, forming_h4_low=99.0,
        forming_h4_close=99.2,
        atr_h4=1.5,
        pd_zone="premium", w1_bias="BEARISH", d1_bias="BEARISH", h1_bias="BEARISH", h4_bias="BEARISH",
    )
    r = dm._pre_gate(a)
    blocked = r is not None and "FORMING-H4" in r
    _check("SELL passes when forming H4 is bearish", blocked, False, r)

    # Case 5: BUY + bearish forming but tiny range (0.2*ATR) -> PASS (indecisive)
    a = _StubAnalysis(
        direction="BULLISH",
        forming_h4_open=100.0, forming_h4_high=100.15, forming_h4_low=99.85,
        forming_h4_close=99.95,
        atr_h4=1.5,  # range 0.30 = 0.2*ATR -> below 0.5 threshold -> indecisive
    )
    r = dm._pre_gate(a)
    blocked = r is not None and "FORMING-H4" in r
    _check("BUY passes when forming H4 range is indecisive", blocked, False, r)

    # Case 6: All forming-H4 fields zero (no data) -> PASS
    a = _StubAnalysis(
        direction="BULLISH",
        forming_h4_open=0.0, forming_h4_high=0.0, forming_h4_low=0.0,
        forming_h4_close=0.0, atr_h4=0.0,
    )
    r = dm._pre_gate(a)
    blocked = r is not None and "FORMING-H4" in r
    _check("Gate inactive when forming-H4 fields are zero", blocked, False, r)

    print()
    if _failures:
        print(f"FAIL — {len(_failures)} test(s) failed: {_failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
