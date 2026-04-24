@echo off
REM Launch bridge as a detached process that survives terminal close
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

REM Clean stale lock if needed
del /f "%USERPROFILE%\.tradingview-mcp\bridge.lock" >nul 2>&1

REM Start in a new minimized window
start "TradingView Bridge LIVE" /min python auto_trade.py --mode live
echo Bridge launched. Check the "TradingView Bridge LIVE" window.
