@echo off
REM Single-shot bridge launcher — NO restart loop
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "C:\Users\User\tradingview-mcp-jackson"
del /f "%USERPROFILE%\.tradingview-mcp\bridge.lock" >nul 2>&1
title TradingView Bridge LIVE (single)
echo Starting bridge...
python auto_trade.py --mode live
echo Bridge exited with code %ERRORLEVEL%.
pause
