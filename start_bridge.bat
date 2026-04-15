@echo off
REM ============================================================
REM  Auto-Trading Bridge — TradingView -> ICT + 36 Strategies -> Claude -> Paper/Live
REM ============================================================

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set TV_EXE=C:\Program Files\WindowsApps\TradingView.Desktop_3.0.0.7652_x64__n534cwy3pjxzj\TradingView.exe
REM ANTHROPIC_API_KEY is loaded from .env by the bridge at startup

REM Ensure node/npm are on PATH
set PATH=C:\Program Files\nodejs;C:\Users\User\AppData\Roaming\npm;%PATH%

cd /d "%~dp0"

echo.
echo  =============================================
echo   TradingView Auto-Trading Bridge
echo  =============================================
echo.

REM Check if CDP port 9222 is already listening
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 9222 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo  TradingView not running with CDP. Launching now...
    powershell -NoProfile -Command "Stop-Process -Name TradingView -Force -ErrorAction SilentlyContinue" >nul 2>&1
    start "" "%TV_EXE%" --remote-debugging-port=9222
    echo  Waiting for TradingView to start...
    timeout /t 6 /nobreak >nul

    powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 9222 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo  [ERROR] TradingView did not open port 9222 in time.
        echo.
        pause
        exit /b 1
    )
    echo  TradingView launched with CDP on port 9222.
    timeout /t 3 /nobreak >nul
) else (
    echo  TradingView already running on port 9222.
)

echo.
echo  Checking TradingView connection...
npm run tv -- status 2>nul | findstr /i "success" >nul
if errorlevel 1 (
    echo.
    echo  [ERROR] CDP health check failed - TradingView may still be loading.
    echo  Try again in a few seconds.
    echo.
    pause
    exit /b 1
)

echo  TradingView connected!
echo.
REM Check if --live flag was passed
set MODE=paper
echo %* | findstr /i "\-\-live" >nul && set MODE=live

if "%MODE%"=="live" (
    echo  Starting bridge in LIVE mode ^(trades sent to MT5^)...
    echo  WARNING: Real money trades will be executed!
    echo  Press Ctrl+C to stop.
    echo.
    python auto_trade.py --mode live %*
) else (
    echo  Starting bridge in PAPER mode...
    echo  Press Ctrl+C to stop.
    echo.
    python auto_trade.py %*
)

pause
