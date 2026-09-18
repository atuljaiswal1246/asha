@echo off
REM Asha — no-build launcher for Windows.
REM Starts the Python backend and opens the UI in an Edge "app" window.
REM Use this if you don't want to install the .NET SDK / build the WebView2 app.
setlocal
set "HERE=%~dp0"
set "UI=%HERE%..\ui"
set "PY=%HERE%..\..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [asha] starting backend: %PY% launch.py
start "Asha backend" /min cmd /c ""%PY%" "%UI%\launch.py" --no-browser"

echo [asha] waiting for the UI on http://127.0.0.1:8000 ...
timeout /t 6 /nobreak >nul

where msedge >nul 2>nul
if errorlevel 1 (
  start "" http://127.0.0.1:8000/
) else (
  start "" msedge --app=http://127.0.0.1:8000/ --window-size=1360,900
)
endlocal
