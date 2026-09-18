# Jarvis — self-host on your own server

Run the full Jarvis assistant (voice + coding) on your own VPS, like Hermes.
Two paths:

## A. Docker (recommended)
```bash
cp prototype/.env.example prototype/.env     # add your key(s)
docker compose -f deploy/docker-compose.yml up -d --build
```
Open `http://<server-ip>:8000`. The assistant WebSocket is on `:7860`.
Data (sessions, memory, skills, prefs) persists in the `jarvis_data` volume.

## B. systemd on a bare VPS (Debian/Ubuntu)
```bash
git clone <your repo> jarvis && cd jarvis
sudo bash deploy/install.sh
# then edit /opt/jarvis/prototype/.env with your key(s):
sudo systemctl restart jarvis
```
Installs system deps (espeak-ng for TTS, libgomp1 for onnxruntime), a venv with
all packages, the voice models, and a `jarvis.service`. Open
`http://<server-ip>:8000`.

## HTTPS + a domain (optional)
Download the voice models once, point Caddy at it, and the browser uses
`wss://<domain>/ws`:
```bash
caddy run --config deploy/Caddyfile      # edit the domain first
```
The UI is proxy-friendly: over HTTPS it connects to `wss://<host>/ws`, over HTTP
to `ws://<host>:7860` (see `static/app.js`).

## Connecting the desktop app to your server
The bundled app runs everything locally. To use a *remote* server instead, point
the app at it (Settings → provider) or use the `jarvis` plan provider with your
gateway. (Remote-client mode in the packaged app is a follow-up.)

## Keys & cost
- Keys live only in `prototype/.env` on your server (gitignored, never in the image).
- Default model is a cheap, capable open model; you can switch in Settings.
- For a company/team, run the **gateway** (`prototype/gateway/gateway.py`) in
  front so every member shares one key with per-user quotas.

## Updating
```bash
git pull && docker compose -f deploy/docker-compose.yml up -d --build
# or, systemd: sudo cp -R prototype /opt/jarvis/ && sudo systemctl restart jarvis
```
