@echo off
setlocal
cd /d "%~dp0"

REM Same env cleanup as run.bat: clear stale PYTHONHOME + MiniMax key
set PYTHONHOME=
set MINIMAX_API_KEY=
set PYTHONUTF8=1

echo === Web UI: http://127.0.0.1:7890 ===
echo (browser will open automatically)
echo.
".venv\Scripts\python.exe" -m hsg.ui
pause
endlocal
