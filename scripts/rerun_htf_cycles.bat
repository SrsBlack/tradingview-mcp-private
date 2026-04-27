@echo off
REM Post-event re-run for the HTF rejection cycles bench.
REM Trigger this AFTER the 11:15 UTC ETH chart event has materialised
REM (or any time you want a fresh run against the current trading.log).
REM
REM Steps:
REM   1. Refresh M15 cache so the latest M15 bars are available
REM   2. Re-run cycles bench (writes/overwrites bench_htf_rejection_cycles_<DATE>.txt)
REM
REM Output is shown in this window AND piped into the dated frozen file.

cd /d C:\Users\User\tradingview-mcp-jackson

setlocal
set PYTHONUTF8=1

echo === Refreshing M15 cache ===
python scripts\refresh_cache.py
if errorlevel 1 (
    echo Cache refresh failed — aborting.
    pause
    exit /b 1
)

echo.
echo === Running cycle-log replay bench ===
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%I
echo Output will be saved to scripts\bench_htf_rejection_cycles_%TODAY%.txt

python scripts\bench_htf_rejection_cycles.py > scripts\bench_htf_rejection_cycles_%TODAY%.txt 2>&1
type scripts\bench_htf_rejection_cycles_%TODAY%.txt

echo.
echo Done.
echo Frozen output: scripts\bench_htf_rejection_cycles_%TODAY%.txt
pause
