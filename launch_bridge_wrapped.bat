@echo off
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "C:\Users\User\tradingview-mcp-jackson"
del /f "%USERPROFILE%\.tradingview-mcp\bridge.lock" >nul 2>&1
echo Starting bridge at %date% %time%...
python auto_trade.py --mode live 2>&1
echo.
echo Bridge exited with code %ERRORLEVEL% at %date% %time%
echo Press any key to restart, or close window to stop.
pause >nul
goto :eof
