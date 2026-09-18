# Jarvis — Windows app

Two ways to run Jarvis on Windows. Both start the same local Python backend
(`prototype/ui/launch.py`) and show the UI.

## 0. One-time setup (required for both)
1. Install **Python 3.11+** (python.org; tick "Add python.exe to PATH").
2. In the repo root, create the venv and install deps:
   ```bat
   py -3 -m venv .venv
   .venv\Scripts\python.exe -m pip install --upgrade pip
   .venv\Scripts\python.exe -m pip install -r prototype\requirements.txt
   ```
3. (Optional) Add your API key(s): open Jarvis → **Settings → API keys**, or drop
   them in `prototype\.env` (gitignored). Custom OpenAI-compatible endpoints go in
   the **Custom providers** section of Settings.

## 1. Quick run (no build) — `start_jarvis.bat`
Double-click `start_jarvis.bat`. It launches the backend and opens the UI in an
Edge "app" window. This is the fastest way to confirm everything works.

## 2. Native app (WebView2) — `build.bat`
Needs the **.NET 8 SDK** (https://dotnet.microsoft.com/download). Then:
```bat
build.bat
dist\Jarvis.exe
```
`Jarvis.exe` starts the backend itself and shows the UI in an embedded window
(no browser). WebView2 Runtime ships with Windows 10/11; if missing, install the
"Evergreen WebView2 Runtime" from Microsoft.

## Voice notes
The voice pipeline is **browser-mediated** (the page captures the mic and streams
audio to the local WS server), so it is not tied to Windows audio APIs. It needs
the full `prototype/requirements.txt` (pipecat, kokoro-onnx, moonshine-voice,
onnxruntime) — all ship Windows wheels. **This stack is required for the app to
start** (the server imports it at boot); if a package fails to install on your
Windows machine, that is the first thing to fix — share `pip`'s error and it can
be made optional.

## Troubleshooting
- **"Could not find the Jarvis repo"** — set `JARVIS_HOME` to the repo folder
  (the one containing `prototype\ui\server.py`).
- **Backend won't start** — check `.venv\Scripts\python.exe prototype\ui\server.py`
  runs (it prints the error); ensure the venv deps installed.
- **Blank window** — the backend was still starting; wait a few seconds (the app
  retries), or check `python prototype\ui\doctor.py`.
