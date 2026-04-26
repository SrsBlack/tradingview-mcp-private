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

REM Pre-flight: run lint to surface memory/code drift. Visibility only —
REM does not block startup (use the git pre-push hook for hard enforcement).
if exist "scripts\lint_memory.py" (
    echo  [LINT] Pre-flight memory + code drift check...
    python scripts\lint_memory.py 2>&1 | findstr /R "Summary: \[FAIL\] \[WARN\]"
    echo.
)

:loop
REM Redirect both stdout and stderr to logs\trading.log (append mode) so the rich
REM cycle output (CYCLE headers, per-symbol scoring, Decision/Reason lines) is
REM visible to anyone tailing the file. Without this redirect, those prints go
REM only to the cmd console window. ERRORLEVEL is preserved (unlike a `| tee`
REM pipeline which would expose the tee exit code instead of python's).
python auto_trade.py --mode live %* >> logs\trading.log 2>&1
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
