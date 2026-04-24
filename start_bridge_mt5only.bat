@echo off
REM =============================================================================
REM  ICT Bridge - MT5-only launcher (no TradingView dependency)
REM  Use this when MT5 is the primary data source (current default).
REM  If you need the TV fallback layer verified, use start_bridge_auto.bat instead.
REM =============================================================================

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

echo.
echo  ===========================================
echo   ICT BRIDGE -- LIVE MODE (MT5 primary)
echo   Reasoning gate ACTIVE (Apr 24 2026)
echo  ===========================================
echo.

:loop
python auto_trade.py --mode live %*
set EXITCODE=%ERRORLEVEL%
echo.
echo  [BRIDGE] Exited code %EXITCODE%.

if "%EXITCODE%"=="2" (
    echo  [BRIDGE] Fatal config error -- not restarting. Fix .env and relaunch.
    pause
    exit /b 2
)

if "%EXITCODE%"=="3" (
    echo  [BRIDGE] Auth error -- retrying in 5s with fresh .env load.
    timeout /t 5 /nobreak >nul
    goto loop
)

echo  Restarting bridge in 10 seconds... (Ctrl+C to stop)
timeout /t 10 /nobreak >nul
goto loop
