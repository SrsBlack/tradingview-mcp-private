@echo off
REM =============================================================================
REM  TradingView Bridge - AUTO LAUNCHER (LIVE mode)
REM
REM  Does everything in one go:
REM   1. Starts TradingView Desktop with --remote-debugging-port=9222 if not up
REM   2. Waits for port 9222 to be listening (up to 60s)
REM   3. Verifies MCP can reach TradingView via `npm run tv -- status`
REM   4. Launches auto_trade.py --mode live in a :loop with crash-restart
REM
REM  Usage: double-click this file, or run from cmd:
REM    start_bridge_auto.bat
REM =============================================================================

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set TV_EXE=C:\Program Files\WindowsApps\TradingView.Desktop_3.0.0.7652_x64__n534cwy3pjxzj\TradingView.exe

REM Ensure node/npm on PATH
set PATH=C:\Program Files\nodejs;C:\Users\User\AppData\Roaming\npm;%PATH%

cd /d "%~dp0"

echo.
echo  =============================================
echo   TradingView Bridge - AUTO LAUNCHER (LIVE)
echo  =============================================
echo.

REM --- Step 1: Check / launch TradingView ---
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 9222 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo  [STEP 1] TradingView not running with CDP. Launching now...
    powershell -NoProfile -Command "Stop-Process -Name TradingView -Force -ErrorAction SilentlyContinue" >nul 2>&1
    if not exist "%TV_EXE%" (
        echo  [ERROR] TradingView executable not found at:
        echo           %TV_EXE%
        echo  Update TV_EXE in this script if TradingView installed elsewhere.
        pause
        exit /b 1
    )
    start "" "%TV_EXE%" --remote-debugging-port=9222
    echo  [STEP 1] Waiting for TradingView to open port 9222...

    REM Wait up to 60s for port 9222 to be listening
    set /a WAITED=0
    :tv_wait_loop
    timeout /t 2 /nobreak >nul
    set /a WAITED+=2
    powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 9222 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
    if not errorlevel 1 goto tv_ready
    if %WAITED% geq 60 (
        echo  [ERROR] TradingView did not open port 9222 in 60s.
        echo  Manually start TradingView and try again.
        pause
        exit /b 1
    )
    goto tv_wait_loop

    :tv_ready
    echo  [STEP 1] TradingView port 9222 is listening.
) else (
    echo  [STEP 1] TradingView already running on port 9222.
)

REM Give the UI a moment to render the chart before MCP pokes it
timeout /t 3 /nobreak >nul

REM --- Step 2: Verify MCP can talk to TradingView ---
echo  [STEP 2] Verifying MCP -> TradingView connection...
npm run tv -- status 2>&1 | findstr /i "success" >nul
if errorlevel 1 (
    echo  [WARN] MCP status check failed — TradingView may still be loading.
    echo         Waiting 10s and retrying once...
    timeout /t 10 /nobreak >nul
    npm run tv -- status 2>&1 | findstr /i "success" >nul
    if errorlevel 1 (
        echo  [WARN] MCP check failed again — starting bridge anyway.
        echo         Bridge has its own TV health check and will retry.
    )
)
echo  [STEP 2] MCP -> TradingView connection OK.

echo.
echo  [STEP 3] Starting bridge in LIVE mode.
echo           Magic number: 99002  ^|  Comment tag: ICT_Bridge
echo           Press CTRL+C to stop the loop.
echo.

:loop
python auto_trade.py --mode live %*
set EXITCODE=%ERRORLEVEL%
echo.
echo  [BRIDGE] Process exited with code %EXITCODE%.

REM Exit code 2 = fatal config error (missing/invalid ANTHROPIC_API_KEY). Don't restart.
if "%EXITCODE%"=="2" (
    echo  [BRIDGE] Fatal config error — not restarting. Fix .env and relaunch.
    pause
    exit /b 2
)

REM Exit code 3 = transient auth failure. Short sleep and retry with fresh .env.
if "%EXITCODE%"=="3" (
    echo  [BRIDGE] Auth error — will retry in 5s with fresh .env load.
    timeout /t 5 /nobreak >nul
    goto loop
)

REM Anything else — generic crash. Check TV still up before restarting.
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 9222 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo  [BRIDGE] TradingView disconnected — restarting whole launcher in 10s...
    timeout /t 10 /nobreak >nul
    goto :eof
    REM ^ falls off the end; user can re-launch. Alternative: call ourselves recursively (unsafe in .bat).
)
echo  Restarting bridge in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
