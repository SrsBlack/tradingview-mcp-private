@echo off
REM ============================================================
REM  Auto-Trading Bridge — LIVE MODE (real MT5 execution)
REM  WARNING: This places REAL trades with REAL money in MT5!
REM ============================================================

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set TV_EXE=C:\Program Files\WindowsApps\TradingView.Desktop_3.0.0.7652_x64__n534cwy3pjxzj\TradingView.exe
set ANTHROPIC_API_KEY=YOUR_ANTHROPIC_API_KEY_HERE

REM Ensure node/npm are on PATH
set PATH=C:\Program Files\nodejs;C:\Users\User\AppData\Roaming\npm;%PATH%

cd /d "%~dp0"

echo.
echo  =============================================
echo   TradingView Auto-Trading Bridge - LIVE MODE
echo   *** REAL MONEY TRADING - USE WITH CAUTION ***
echo  =============================================
echo.
echo  Checks:
echo   - MT5 must be running and logged in
echo   - TradingView must be open on port 9222
echo   - Symbols must be added to MT5 watchlist
echo.
echo  Press CTRL+C NOW if you are not ready for live trading!
echo.
timeout /t 5 /nobreak

REM Check TradingView CDP
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 9222 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] TradingView not running on port 9222. Start TradingView first.
    pause
    exit /b 1
)

echo  TradingView connected!
echo  Starting LIVE bridge...
echo  Trades will be executed in MT5 with magic number 99002.
echo.

python auto_trade.py --mode live %*

pause
