@echo off
REM Build the Jarvis Windows app (WebView2 shell). Requires the .NET 8 SDK.
setlocal
cd /d "%~dp0"

where dotnet >nul 2>nul
if errorlevel 1 (
  echo [build] .NET 8 SDK not found. Install it from https://dotnet.microsoft.com/download
  exit /b 1
)

dotnet publish -c Release -r win-x64 --self-contained false -o dist
if errorlevel 1 exit /b 1

echo.
echo [build] done -^> "%~dp0dist\Jarvis.exe"
echo [build] double-click Jarvis.exe to launch (make sure the Python venv is set up).
endlocal
