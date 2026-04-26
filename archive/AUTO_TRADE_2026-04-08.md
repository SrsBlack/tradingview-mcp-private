# Claude Auto-Trading System

**Full end-to-end autonomous trading pipeline:**
TradingView (chart data) → TradingView MCP → ICT Scorer → Claude Agent → MT5 Execution

---

## Architecture

```
TradingView Desktop (4H chart with indicators)
    ↓ (CDP port 9222)
Morning Brief (reads chart snapshots)
    ↓
ICT Analyzer (structure, liquidity, OB, FVG scoring)
    ↓
Claude Agent (evaluates signals with Opus reasoning)
    ↓
MT5 Executor (live/paper trading)
    ↓
Streamlit Dashboard + Alerts
```

## Components

### 1. **Morning Brief** (`npm run tv -- brief`)
- Scans your TradingView watchlist (configured in `rules.json`)
- Reads all visible indicators on each symbol
- Captures real-time OHLCV data
- Outputs raw snapshot as JSON

### 2. **ICT Analyzer** (`src/services/ict_analyzer.py`)
- Scores each symbol using ICT methodology:
  - Structure (25%): Higher Highs/Lows vs Lower Highs/Lows
  - Liquidity (20%): Sweeps, draw-on-liquidity, BSL/SSL
  - Order Blocks (15%): Valid, unmitigated zones
  - Fair Value Gaps (15%): FVGs and inversions
  - Session (10%): Kill zones, Silver Bullet
  - OTE (10%): 0.618–0.786 Fibonacci levels
  - SMT (5%): Smart Money divergence
- Outputs Grade A/B/C/D + confidence
- Can bridge to `trading-ai-v2` scorer for advanced analysis

### 3. **Claude Agent** (`src/services/claude_agent.py`)
- Subscribes to ICT scores
- Uses Claude Opus with extended thinking
- Applies your trading rules (bias criteria, risk rules)
- Makes decision: BUY, SELL, or SKIP
- Calculates position size, entry, stop-loss, take-profit
- Returns trade decision with confidence

### 4. **Auto-Trader Loop** (`auto_trade.py`)
- Orchestrates full pipeline
- Runs on configurable interval (default: 60s)
- Logs every decision to `logs/`
- Paper trading by default, live mode with confirmation

---

## Setup

### Prerequisites
- TradingView Desktop running on port 9222 (CDP enabled)
- `npm install` (MCP server dependencies)
- Python 3.8+ with `anthropic` SDK

### Installation

```bash
# Install Python dependencies
pip install anthropic

# Optional: Link trading-ai-v2 for advanced ICT scoring
# (auto-detected if at ~/Desktop/trading-ai-v2)

# Verify setup
python -c "from anthropic import Anthropic; print('✓ Anthropic SDK ready')"
```

### Configure

Edit `rules.json`:
- Set your watchlist symbols
- Review ICT scoring weights
- Update bias criteria
- Confirm risk rules match your FTMO account

Example:
```json
{
  "watchlist": ["BTCUSD", "ETHUSD", "SOLUSD"],
  "default_timeframe": "240",
  "ict_scoring": {
    "weights": { "structure": 0.25, ... }
  },
  "risk_rules": [
    "Grade A signals (≥80) only",
    "Grade B+ (≥65) with 1:2+ R:R",
    "No first 15 mins of NY open",
    ...
  ]
}
```

---

## Usage

### 1. **Test Single Cycle (Paper Trading)**

```bash
# Run one auto-trade cycle
python auto_trade.py --paper --interval 0

# Output:
# [2026-04-07 08:15:23] Auto-Trader started (account: paper, interval: 0s)
# [2026-04-07 08:15:23] Running morning brief...
# [2026-04-07 08:15:25] Brief captured 3 symbols
# [2026-04-07 08:15:26] Analyzing with ICT scorer...
# [2026-04-07 08:15:27] Analysis complete: 3 signals scored
# [2026-04-07 08:15:30] Running Claude agent...
# [2026-04-07 08:15:45] Decisions: 2 trades executed, 1 skipped
```

### 2. **Run Continuous Loop (Paper Trading)**

```bash
# Run every 60 seconds (default)
python auto_trade.py --paper

# Run every 5 minutes
python auto_trade.py --paper --interval 300

# Output logs to logs/auto_trade_session_*.log
```

### 3. **LIVE Trading (Use With Caution)**

```bash
# This will EXECUTE REAL TRADES on your MT5 account
python auto_trade.py --live --interval 60

# You'll be prompted:
# ⚠️  LIVE TRADING MODE. Type 'yes' to confirm:
```

### 4. **Monitor Trades**

```bash
# View session logs
tail -f logs/auto_trade_session_*.log

# View trade execution logs (JSON)
cat logs/trades_*.jsonl | jq .

# View Streamlit dashboard (when connected to trading-ai-v2)
streamlit run monitoring/dashboard.py
```

---

## Signal Decision Flow

### Grade A (≥80 score)
✅ **Execute immediately** — High conviction entry
- Check risk rules
- Calculate position size (1% per FTMO)
- Submit buy/sell order

### Grade B (65-79 score)
⚠️ **Execute with strict risk management**
- Require 1:2+ R:R
- Reduce position size to 0.5%
- Wait for pullback if possible

### Grade C (50-64 score)
❓ **Pull-back zone only**
- Enter only on pullback to support/resistance
- Max 0.25% position size
- Timeframe 1H or higher

### Grade D or lower (<50)
❌ **Skip** — Wait for better setup

---

## Example: Single Cycle Breakdown

**TradingView Chart (4H, BTCUSD):**
- Current price: $69,017
- Structure: Higher Highs + Higher Lows (bullish)
- Liquidity: Sweep confirmed at PDL
- Order Block: Valid OB at $68,500
- FVG: Bullish gap unfilled at $68,600

**Morning Brief Output:**
```json
{
  "symbol": "BTCUSD",
  "timeframe": "240",
  "quote": { "close": 69017, ... }
}
```

**ICT Analyzer:**
```json
{
  "symbol": "BTCUSD",
  "direction": "BULLISH",
  "total_score": 78,
  "grade": "B",
  "confidence": 0.82,
  "breakdown": {
    "structure": 20,
    "liquidity": 18,
    "order_block": 12,
    ...
  }
}
```

**Claude Agent Decision:**
```json
{
  "decision": "BUY",
  "entry_price": 69050,
  "stop_loss": 68700,
  "take_profit": 69400,
  "position_size_pct": 0.5,
  "confidence": 82,
  "reasoning": "Grade B signal with clean structure + liquidity sweep. OB behind price. R:R 2.3:1."
}
```

**Execution:**
```
[2026-04-07 08:15:45] BUY BTCUSD @ 69050 | SL: 68700 | TP: 69400 | Size: 0.5%
Order ID: BTCUSD_1712500545.123
Status: EXECUTED (paper trading log)
```

---

## Risk Management

### FTMO Compliance
- **1% per ICT trade** (Grade A/B)
- **0.2% per EA trade** (if you add EA engine)
- **5% max daily loss** — Pause if hit
- **10% max drawdown** — Session stop-loss

### Position Sizing
Calculated automatically based on:
- Grade (A=1%, B=0.5%, C=0.25%)
- Risk per trade (FTMO rules)
- Account equity
- Entry/SL distance

### Trade Management
- Trailing stop-loss after breakeven
- Partial TP at 50% entry, 50% at full TP
- Max 2 open positions
- Pause if 2 consecutive losses

---

## Integration with trading-ai-v2

If trading-ai-v2 is at `~/Desktop/trading-ai-v2`, the ICT Analyzer will auto-detect and use:
- `analysis.ict.scorer.ICTScoreBreakdown` for advanced scoring
- `analysis.structure` for market structure analysis
- `analysis.liquidity` for liquidity detection
- Real MT5 execution via `execution.executor`

To enable:
1. Ensure trading-ai-v2 path is correct
2. Configure `config.yaml` in trading-ai-v2
3. Set `.env` with MT5 credentials (if going live)

---

## Troubleshooting

### TradingView not connecting
```bash
# Check CDP port 9222
curl http://localhost:9222/json/version

# If not running:
TradingView --remote-debugging-port=9222

# Or use launcher:
npm run tv -- launch
```

### Claude API errors
```bash
# Check API key
echo $ANTHROPIC_API_KEY

# If missing:
export ANTHROPIC_API_KEY="sk-ant-..."
```

### ICT Analyzer not working
```bash
# Test in isolation
python -m src.services.ict_analyzer logs/brief_*.json

# Check for trading-ai-v2
ls ~/Desktop/trading-ai-v2/analysis/ict/
```

### No trades executing
- Check Grade thresholds in `rules.json`
- Verify Claude's decision logic (increase confidence threshold)
- Review logs: `cat logs/trades_*.jsonl`

---

## Next Steps

1. **Test in paper trading** for 1 week
   - Monitor decisions quality
   - Adjust bias criteria based on results
   - Tune position sizing

2. **Backtest strategy**
   - Export `rules.json` config
   - Run `trading-ai-v2` backtest with same rules
   - Compare live vs backtest P&L

3. **Go live (carefully)**
   - Start with smallest position size (0.1%)
   - Enable Telegram alerts for every trade
   - Monitor equity drawdown closely
   - Be ready to pause at any time

4. **Iterate**
   - Compare daily briefs (saved with `session save`)
   - Analyze losing trades
   - Adjust rules/bias criteria
   - Improve Claude's decision criteria

---

## Command Reference

```bash
# Morning brief (one-shot)
npm run tv -- brief

# Save/load session
npm run tv -- session save
npm run tv -- session get 2026-04-07

# Auto-trading pipeline
python auto_trade.py --paper                    # Paper, 60s interval
python auto_trade.py --paper --interval 300    # Paper, 5min interval
python auto_trade.py --live                    # LIVE (with confirmation)

# Check logs
tail -f logs/auto_trade_session_*.log
cat logs/trades_*.jsonl | jq .
```

---

**Built with:** TradingView MCP + Claude API + trading-ai-v2 + Anthropic SDK

**Disclaimer:** Not financial advice. Use at your own risk. Backtest thoroughly before live trading.
