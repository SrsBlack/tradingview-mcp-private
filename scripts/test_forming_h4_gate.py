"""Standalone tests for the G2 compound pre-gate.

Constructs a minimal SymbolAnalysis-like stub and exercises
ClaudeDecisionMaker._pre_gate. The G2 gate fires only when ALL THREE
conditions hold:
  - SELL trade
  - Bullish forming H4 bar with range >= 0.5*ATR_h4
  - >=2 opposing (bullish) HTF FVGs within 0.5% of price

Test scenarios:
  1. SELL + bullish forming + 2 opposing FVGs    -> BLOCKED
  2. SELL + bullish forming + 1 opposing FVG     -> PASS (need >=2)
  3. SELL + bullish forming + 0 opposing FVGs    -> PASS
  4. SELL + bearish forming + 2 opposing FVGs    -> PASS (forming agrees)
  5. BUY + bearish forming + 2 opposing FVGs     -> PASS (gate is sell-only)
  6. SELL + indecisive forming (small range)     -> PASS
  7. ATR_h4 = 0 (no data)                         -> PASS (gate inactive)

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
    htf_opposing_fvg_count_05pct: int = 0


_failures: list[str] = []


def _check(name: str, actual_blocked: bool, expected_blocked: bool, reason: str | None) -> None:
    ok = actual_blocked == expected_blocked
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}: blocked={actual_blocked}  reason={reason}")
    if not ok:
        _failures.append(name)


def run():
    dm = ClaudeDecisionMaker()
    sell_kw = dict(
        pd_zone="premium",
        w1_bias="BEARISH", d1_bias="BEARISH", h1_bias="BEARISH", h4_bias="BEARISH",
    )

    print("G2 compound gate tests:")

    # Case 1: SELL + bullish forming + 2 opposing FVGs -> BLOCK
    a = _StubAnalysis(
        direction="BEARISH",
        forming_h4_open=100.0, forming_h4_high=101.5, forming_h4_low=99.8,
        forming_h4_close=101.2,  # bullish
        atr_h4=1.5,
        htf_opposing_fvg_count_05pct=2,
        **sell_kw,
    )
    r = dm._pre_gate(a)
    blocked = r is not None and "G2 COMPOUND" in r
    _check("SELL + bullish forming + 2 stacked FVGs blocks", blocked, True, r)

    # Case 2: SELL + bullish forming + only 1 opposing FVG -> PASS (need >=2)
    a = _StubAnalysis(
        direction="BEARISH",
        forming_h4_open=100.0, forming_h4_high=101.5, forming_h4_low=99.8,
        forming_h4_close=101.2, atr_h4=1.5,
        htf_opposing_fvg_count_05pct=1,
        **sell_kw,
    )
    r = dm._pre_gate(a)
    blocked = r is not None and "G2 COMPOUND" in r
    _check("SELL with only 1 opposing FVG passes", blocked, False, r)

    # Case 3: SELL + bullish forming + 0 opposing FVGs -> PASS
    a = _StubAnalysis(
        direction="BEARISH",
        forming_h4_open=100.0, forming_h4_high=101.5, forming_h4_low=99.8,
        forming_h4_close=101.2, atr_h4=1.5,
        htf_opposing_fvg_count_05pct=0,
        **sell_kw,
    )
    r = dm._pre_gate(a)
    blocked = r is not None and "G2 COMPOUND" in r
    _check("SELL with 0 opposing FVGs passes", blocked, False, r)

    # Case 4: SELL + bearish forming + 2 opposing FVGs -> PASS (forming agrees)
    a = _StubAnalysis(
        direction="BEARISH",
        forming_h4_open=100.0, forming_h4_high=100.5, forming_h4_low=99.0,
        forming_h4_close=99.2,  # bearish (matches SELL)
        atr_h4=1.5,
        htf_opposing_fvg_count_05pct=2,
        **sell_kw,
    )
    r = dm._pre_gate(a)
    blocked = r is not None and "G2 COMPOUND" in r
    _check("SELL with bearish forming H4 passes", blocked, False, r)

    # Case 5: BUY + bearish forming + 2 opposing FVGs -> PASS (gate is sell-only)
    a = _StubAnalysis(
        direction="BULLISH",
        forming_h4_open=100.0, forming_h4_high=100.5, forming_h4_low=99.0,
        forming_h4_close=99.2, atr_h4=1.5,
        htf_opposing_fvg_count_05pct=2,
    )
    r = dm._pre_gate(a)
    blocked = r is not None and "G2 COMPOUND" in r
    _check("BUY does NOT trigger G2 (sell-only)", blocked, False, r)

    # Case 6: SELL + indecisive (range < 0.5 ATR) -> PASS
    a = _StubAnalysis(
        direction="BEARISH",
        forming_h4_open=100.0, forming_h4_high=100.15, forming_h4_low=99.85,
        forming_h4_close=100.05, atr_h4=1.5,  # range 0.30 = 0.2*ATR
        htf_opposing_fvg_count_05pct=2,
        **sell_kw,
    )
    r = dm._pre_gate(a)
    blocked = r is not None and "G2 COMPOUND" in r
    _check("SELL with indecisive forming H4 passes", blocked, False, r)

    # Case 7: ATR_h4 = 0 (no data) -> PASS (gate inactive)
    a = _StubAnalysis(
        direction="BEARISH",
        forming_h4_open=0.0, forming_h4_high=0.0, forming_h4_low=0.0,
        forming_h4_close=0.0, atr_h4=0.0,
        htf_opposing_fvg_count_05pct=2,
        **sell_kw,
    )
    r = dm._pre_gate(a)
    blocked = r is not None and "G2 COMPOUND" in r
    _check("Gate inactive when forming-H4 fields are zero", blocked, False, r)

    print()
    if _failures:
        print(f"FAIL — {len(_failures)} test(s) failed: {_failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
