#!/usr/bin/env python3
"""
Auto-Trading Launcher — thin wrapper around bridge.orchestrator.

Usage:
    python auto_trade.py                      # Paper mode, default watchlist
    python auto_trade.py --single             # Single cycle then exit
    python auto_trade.py --symbols BTCUSD ETHUSD
    python auto_trade.py --live               # Live MT5 (future)
    python auto_trade.py --interval 0         # Single cycle (alias for --single)
"""

from bridge.orchestrator import main

if __name__ == "__main__":
    main()
