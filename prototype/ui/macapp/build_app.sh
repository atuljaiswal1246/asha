#!/bin/bash
# Build the macOS app bundle (AppKit + WKWebView) -> VoiceAssistant.app
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="$HERE/build"
APP="$HERE/VoiceAssistant.app"

rm -rf "$BUILD" "$APP"
mkdir -p "$BUILD" "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
cp "$HERE/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

# Asset: simple app icon (optional; skip if none exists) - none for now.

swiftc -O -parse-as-library \
  "$HERE/App.swift" \
  -o "$APP/Contents/MacOS/VoiceAssistant" \
  -framework AppKit -framework WebKit \
  -target arm64-apple-macos13.0

codesign --force --deep --sign - --entitlements "$HERE/VoiceAssistant.entitlements" "$APP" 2>/dev/null || true
echo "Built: $APP"
