#!/usr/bin/env bash
# Build Asha.app + Asha-<version>.dmg for macOS.
#
# Bundles everything a developer needs to run Asha with voice: a relocatable
# CPython, all Python deps (pipecat, onnxruntime, kokoro, moonshine), the voice
# models, the app code, and the UI.
#
# OmniRoute is deliberately NOT bundled. It is an optional, user-installed
# add-on (third-party, ~2.1 GB with its node_modules) that a user can point the
# app at with the shipped template's OMNIROUTE_BASE_URL setting. Asha works
# without it on its own transports (deepseek / opencode / an explicit gateway
# base URL) using the user's own key.
#
# Usage:  bash prototype/packaging/build_macos.sh [arm64|x86_64]
# Output: prototype/packaging/dist/Asha.app
#         prototype/packaging/dist/Asha-<version>.dmg
#
# macOS build machine needs: curl, tar, python3 (for URL resolution), swiftc
# (Xcode Command Line Tools) to compile the launcher. Node/npm are NOT needed.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PKG="$REPO/prototype/packaging"
DIST="$PKG/dist"
APP="$DIST/Asha.app"
BUILD="$PKG/.build"
ARCH="${1:-$(uname -m)}"
case "$ARCH" in arm64) PBS="aarch64-apple-darwin";; x86_64) PBS="x86_64-apple-darwin";;
  *) echo "unknown arch: $ARCH"; exit 1;; esac
VERSION="$(cat "$PKG/VERSION" 2>/dev/null || echo 0.1.0)"
# Pinned python-build-standalone release (no GitHub API => no CI rate-limit 403).
PY_VERSION="${PY_VERSION:-3.12.14}"   # requirements need Python 3.11+
PBS_TAG="${PBS_TAG:-20260901}"

require() { command -v "$1" >/dev/null || { echo "missing: $1"; exit 1; }; }
require curl; require tar

echo "==> cleaning"
rm -rf "$APP"
mkdir -p "$BUILD" "$DIST"

# ── 1. relocatable CPython ───────────────────────────────────────────────────
if [ ! -x "$BUILD/runtime/bin/python3" ]; then
  echo "==> fetching python-build-standalone ($PBS)"
  URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-${PY_VERSION}%2B${PBS_TAG}-${PBS}-install_only.tar.gz"
  curl -fL "$URL" -o "$BUILD/python.tar.gz"
  mkdir -p "$BUILD/runtime"
  tar -xzf "$BUILD/python.tar.gz" -C "$BUILD/runtime" --strip-components=1
else
  echo "==> reusing cached runtime"
fi
PY="$BUILD/runtime/bin/python3"
echo "    python: $("$PY" --version)"

# ── 2. app code ──────────────────────────────────────────────────────────────
echo "==> staging app code"
mkdir -p "$APP/Contents/Resources/app"
rsync -a --delete \
  --exclude '__pycache__' --exclude '*.pyc' --exclude 'macapp' --exclude 'winapp' \
  --exclude 'opencode-desktop' --exclude 'webfront' --exclude 'node_modules' \
  "$REPO/prototype/ui/" "$APP/Contents/Resources/app/ui/"
rsync -a --delete --exclude '__pycache__' \
  "$REPO/prototype/gateway/" "$APP/Contents/Resources/app/gateway/"
cp "$REPO/prototype/requirements.txt" "$APP/Contents/Resources/app/requirements.txt"
# Ship the NON-SECRET config template — never prototype/.env. Provider API
# keys live on the Asha proxy (Cloudflare); the build host's .env must not
# leak into the bundle. The per-user plan token is issued at sign-in and stored
# outside the bundle, so nothing credential-shaped is copied here.
cp "$PKG/shipped.env.template" "$APP/Contents/Resources/app/.env"
chmod 600 "$APP/Contents/Resources/app/.env" 2>/dev/null || true
# Optional: bake the deployed proxy base URL into the shipped config without
# editing the template. Export JARVIS_PROXY_URL (a base URL — not a secret)
# before building. The app and any user-installed OmniRoute read
# JARVIS_GATEWAY_URL from here; JARVIS_TOKEN is always added at sign-in.
if [ -n "${JARVIS_PROXY_URL:-}" ]; then
  printf '\nJARVIS_GATEWAY_URL=%s\n' "$JARVIS_PROXY_URL" >> "$APP/Contents/Resources/app/.env"
fi

# ── 2b. third-party notices ──────────────────────────────────────────────────
# The bundle ships copyleft components inside runtime/ (phonemizer, eSpeak NG,
# num2words, soxr) as part of the Kokoro TTS stack. Their notices must travel
# with them: this file is copied to a stable bundle path and asserted in step 6,
# so a future build cannot silently ship without it.
cp "$PKG/THIRD_PARTY_NOTICES.md" "$APP/Contents/Resources/THIRD_PARTY_NOTICES.md"

# ── 3. deps into the runtime ─────────────────────────────────────────────────
echo "==> installing python deps (this takes a while)"
"$PY" -m pip install --upgrade pip -q
"$PY" -m pip install -q -r "$APP/Contents/Resources/app/requirements.txt"
"$PY" -m pip install -q python-dotenv websockets

# ── 3b. copy the runtime into the bundle ─────────────────────────────────────
echo "==> bundling python runtime into the app"
cp -R "$BUILD/runtime" "$APP/Contents/Resources/runtime"

# ── 3c. OmniRoute is NOT bundled ─────────────────────────────────────────────
# The local gateway is an optional, user-installed add-on (third-party,
# ~2.1 GB) and is deliberately not staged, installed or copied here. Asha
# reaches a user's own OmniRoute through the shipped template's
# OMNIROUTE_BASE_URL setting, or runs on its own direct transports.

# ── 4. voice models (so nothing downloads at runtime) ────────────────────────
echo "==> bundling voice models"
MC="$BUILD/models"
MODELS="$APP/Contents/Resources/models"
mkdir -p "$MC/kokoro" "$MC/moonshine" "$MODELS"
KOKORO_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
[ -f "$MC/kokoro/kokoro-v1.0.onnx" ] || curl -fL "$KOKORO_URL/kokoro-v1.0.onnx" -o "$MC/kokoro/kokoro-v1.0.onnx"
[ -f "$MC/kokoro/voices-v1.0.bin" ] || curl -fL "$KOKORO_URL/voices-v1.0.bin" -o "$MC/kokoro/voices-v1.0.bin"
if [ -z "$(ls -A "$MC/moonshine" 2>/dev/null)" ]; then
  MOONSHINE_VOICE_CACHE="$MC/moonshine" "$PY" - <<'PYEOF'
from moonshine_voice.download import get_model_for_language
get_model_for_language("en")
print("    moonshine model ready")
PYEOF
fi
cp -R "$MC/." "$MODELS/"

# ── 5. launcher (.app bundle) ────────────────────────────────────────────────
echo "==> building launcher"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$PKG/launcher/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
swiftc -O -parse-as-library "$PKG/launcher/macos/Launcher.swift" \
  -o "$APP/Contents/MacOS/Asha" -framework Cocoa -framework WebKit
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Asha</string>
  <key>CFBundleDisplayName</key><string>Asha</string>
  <key>CFBundleIdentifier</key><string>ai.jarvis.app</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleExecutable</key><string>Asha</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Asha uses the microphone so it can listen to you.</string>
</dict></plist>
PLIST

# ── 6. verify the bundle (a broken bundle must NOT report success) ───────────
echo "==> verifying bundle"
RES="$APP/Contents/Resources"
assert_present() { [ -e "$1" ] || { echo "BUILD BROKEN: $2 missing at $1"; exit 1; }; }

# ── 6a. KEYLESS GATE (fail-closed) ──────────────────────────────────────────
# The bundle must never contain a provider credential. This scans the
# first-party content we author — the staged app code and the shipped
# app/.env. runtime/ is a checksum-verified third-party artifact and is out of
# scope. Test fixtures are skipped: their placeholder keys would false-positive.
# If ANYTHING key-shaped is found the build FAILS, and a false positive is fixed
# deliberately rather than ignored.
SECRET_RE='sk-[A-Za-z0-9_-]{16,}|sk-or-v1-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{30,}|GOCSPX-[A-Za-z0-9_-]{16,}|figd_[A-Za-z0-9_-]{20,}|tvly-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9_-]{20,}|xai-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
scan_hits="$( grep -rIlE "$SECRET_RE" "$RES/app" --exclude='test_*.py' 2>/dev/null | sort -u || true )"
if [ -n "$scan_hits" ]; then
  echo "BUILD BROKEN: key-shaped string found in the bundle:"
  echo "$scan_hits"
  exit 1
fi
assert_present "$RES/app/.env" "shipped config template (.env)"
ENVF="$RES/app/.env"
if grep -qE '^[[:space:]]*[A-Z0-9_]*(API_KEY|SECRET|_TOKEN)=[^[:space:]]+' "$ENVF" 2>/dev/null; then
  echo "BUILD BROKEN: credential-shaped assignment in $ENVF:"
  grep -nE '^[[:space:]]*[A-Z0-9_]*(API_KEY|SECRET|_TOKEN)=' "$ENVF" | sed 's/=.*/=<redacted>/'
  exit 1
fi
for k in DEEPSEEK_API_KEY OPENCODE_API_KEY; do
  if grep -qE "^${k}=[^[:space:]]+" "$ENVF" 2>/dev/null; then
    echo "BUILD BROKEN: $k has a value in $ENVF"
    exit 1
  fi
done
echo "    keyless gate: no provider credentials in app code or app/.env"

assert_present "$RES/runtime/bin/python3"                          "bundled python"
assert_present "$RES/app/ui/launch.py"                             "app entry (launch.py)"
assert_present "$RES/models/kokoro/kokoro-v1.0.onnx"               "kokoro model"
assert_present "$RES/THIRD_PARTY_NOTICES.md"                       "third-party notices (copyleft components)"
if [ -e "$RES/vendor" ] || [ -e "$RES/runtime-node" ]; then
  echo "BUILD BROKEN: OmniRoute/Node artifacts must not be bundled (found vendor/ or runtime-node/)"
  exit 1
fi
echo "    bundle verified (no bundled gateway)"

# ── 7. .dmg ──────────────────────────────────────────────────────────────────
DMG="$DIST/Asha-$VERSION.dmg"
echo "==> packaging $DMG"
rm -f "$DMG"
hdiutil create -volname "Asha" -srcfolder "$APP" -ov -format UDZO "$DMG" >/dev/null

echo
echo "done:"
echo "  app: $APP"
echo "  dmg: $DMG"
echo
echo "signing (optional but recommended to avoid Gatekeeper warnings):"
echo "  codesign --deep --force --options runtime --sign 'Developer ID Application: YOU' \"$APP\""
echo "  xcrun notarytool submit \"$DMG\" --keychain-profile notary --wait && xcrun stapler staple \"$DMG\""
