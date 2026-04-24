@echo off
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "C:\Users\User\tradingview-mcp-jackson"
del /f "%USERPROFILE%\.tradingview-mcp\bridge.lock" >nul 2>&1
python auto_trade.py --mode live
pause
