#!/usr/bin/env bash
# Asha — self-host installer for a Linux VPS (Debian/Ubuntu).
#
# One command sets up the full assistant (voice + coding) as a systemd service:
#   sudo bash deploy/install.sh
#
# It installs system deps, a Python venv with all packages, the voice models,
# and a systemd unit. Then: edit /opt/asha/prototype/.env with your key(s)
# and `systemctl restart asha`. Open http://<server-ip>:8000.
set -euo pipefail

PREFIX="${JARVIS_PREFIX:-/opt/asha}"
SERVICE_USER="${JARVIS_USER:-${SUDO_USER:-$USER}}"
PORT_UI="${JARVIS_UI_PORT:-8000}"
PORT_WS="${WS_PORT:-7860}"

SRC="$(cd "$(dirname "$0")/.." && pwd)"
say() { printf '\n\033[1;36m[asha]\033[0m %s\n' "$1"; }

if [ "$(id -u)" != "0" ]; then echo "run with sudo"; exit 1; fi

say "installing system packages (espeak-ng for TTS, libgomp1 for onnxruntime)"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip espeak-ng libgomp1 curl ca-certificates

say "copying Asha to $PREFIX"
mkdir -p "$PREFIX"
cp -R "$SRC/prototype" "$PREFIX/prototype"
[ -f "$SRC/deploy/Dockerfile" ] && cp -R "$SRC/deploy" "$PREFIX/deploy" || true
chown -R "$SERVICE_USER" "$PREFIX"

PY="$PREFIX/venv/bin/python"
say "creating venv + installing python deps (this takes a few minutes)"
sudo -u "$SERVICE_USER" python3 -m venv "$PREFIX/venv"
sudo -u "$SERVICE_USER" "$PY" -m pip install --upgrade pip -q
sudo -u "$SERVICE_USER" "$PY" -m pip install -q -r "$PREFIX/prototype/requirements.txt"
sudo -u "$SERVICE_USER" "$PY" -m pip install -q python-dotenv websockets

MODELS="$PREFIX/models"
say "downloading voice models to $MODELS"
sudo -u "$SERVICE_USER" mkdir -p "$MODELS/kokoro" "$MODELS/moonshine"
K="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
sudo -u "$SERVICE_USER" curl -fL "$K/kokoro-v1.0.onnx" -o "$MODELS/kokoro/kokoro-v1.0.onnx"
sudo -u "$SERVICE_USER" curl -fL "$K/voices-v1.0.bin" -o "$MODELS/kokoro/voices-v1.0.bin"
sudo -u "$SERVICE_USER" env MOONSHINE_VOICE_CACHE="$MODELS/moonshine" "$PY" - <<'PY'
from moonshine_voice.download import get_model_for_language
get_model_for_language("en")
print("moonshine model ready")
PY

ENV="$PREFIX/prototype/.env"
if [ ! -f "$ENV" ]; then
  say "creating $ENV (add your API key)"
  sudo -u "$SERVICE_USER" cp "$PREFIX/prototype/.env.example" "$ENV" 2>/dev/null || \
    sudo -u "$SERVICE_USER" touch "$ENV"
fi

say "installing systemd service"
cat > /etc/systemd/system/asha.service <<UNIT
[Unit]
Description=Asha assistant (voice + coding)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$PREFIX/prototype/ui
Environment=JARVIS_HOST=0.0.0.0
Environment=WS_HOST=0.0.0.0
Environment=KOKORO_MODEL_PATH=$MODELS/kokoro/kokoro-v1.0.onnx
Environment=KOKORO_VOICES_PATH=$MODELS/kokoro/voices-v1.0.bin
Environment=MOONSHINE_VOICE_CACHE=$MODELS/moonshine
ExecStart=$PY launch.py --no-browser --host 0.0.0.0 --port $PORT_UI
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now asha

say "done."
cat <<EOF

  Asha is running.

   1. put your key in:  $ENV          (then: sudo systemctl restart asha)
   2. open:             http://<server-ip>:$PORT_UI
   3. logs:             journalctl -u asha -f

  Ports: UI $PORT_UI, assistant WS $PORT_WS. For HTTPS + a domain, use
  $PREFIX/deploy/Caddyfile with Caddy (see deploy/README.md).
EOF
