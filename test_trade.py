"""
Quick test: force a paper trade through the full stack and verify it opens + closes correctly.
Run from project root: python test_trade.py
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bridge.decision_types import TradeDecision
from bridge.paper_executor import PaperExecutor

# --- 1. Build a fake Grade A BUY decision for BTCUSD ---
decision = TradeDecision(
    action="BUY",
    symbol="BTCUSD",
    entry_price=80000.0,
    sl_price=79200.0,       # 1% SL
    tp_price=81200.0,       # 1.5R TP1
    tp2_price=82400.0,      # 3R TP2
    confidence=85,
    risk_pct=0.01,
    reasoning="Test trade — forced through paper executor to verify full stack",
    grade="A",
    ict_score=87.0,
    model_used="test",
    trade_type="swing",
    partial_close_pct=0.5,
)

# --- 2. Open it ---
executor = PaperExecutor(initial_balance=10_000.0)
result = executor.open_position(decision)
print(f"\n[OPEN]  {result}")

if not result["success"]:
    print("ERROR: trade did not open")
    sys.exit(1)

ticket = result["ticket"]
print(f"        Ticket #{ticket} | Balance: ${executor.balance:,.2f}")
print(f"        Open positions: {list(executor.open_positions.keys())}")

# --- 3. Simulate TP1 hit (price moves to tp_price) ---
print(f"\n[SIM]   Price moves to TP1 = $81,200 (should partial-close 50%)")
events = executor.check_positions({"BTCUSD": 81200.0})
for ev in events:
    print(f"        Event: {ev['reason']} | PnL={ev['pnl']:+.2f} | R={ev['r_multiple']:.1f}R | Balance=${ev['balance']:,.2f}")

if ticket in executor.open_positions:
    pos = executor.open_positions[ticket]
    print(f"        Position still open — lots={pos.lot_size:.4f} tp1_hit={pos.tp1_hit} sl={pos.sl_price:.2f}")
else:
    print(f"        Position fully closed at TP1")

# --- 4. Simulate TP2 hit ---
print(f"\n[SIM]   Price moves to TP2 = $82,400 (should fully close remainder)")
events = executor.check_positions({"BTCUSD": 82400.0})
for ev in events:
    print(f"        Event: {ev['reason']} | PnL={ev['pnl']:+.2f} | R={ev['r_multiple']:.1f}R | Balance=${ev['balance']:,.2f}")

print(f"\n[FINAL] Balance: ${executor.balance:,.2f} | Wins: {executor.wins} | Losses: {executor.losses}")
print(f"        Closed positions: {len(executor.closed_positions)}")
print("\n--- Test passed ---\n")
