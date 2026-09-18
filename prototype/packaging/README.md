# Asha packaging & shipping

Build a **single installer** for each OS. Everything a developer needs is
bundled — a relocatable Python, all deps (pipecat, onnxruntime, kokoro,
moonshine), the voice models, the app, and the UI — so a friend installs and it
works, voice included.

**OmniRoute is deliberately NOT bundled.** It is an optional, user-installed
add-on (third-party, ~2.1 GB unpacked with its `node_modules`) that a user can
point the app at with the shipped template's `OMNIROUTE_BASE_URL` setting. It is
not required to use Asha: the app's own transports (`deepseek` / `opencode` /
an explicit gateway base URL) work with the user's own key. Bundling can be
revisited for the later commercial product if a one-click install is wanted.

## Status
- **macOS: built + bundle-verified** (latest full build 2026-09-18, keyless, no
  bundled gateway; `Asha.app` ~1.2 GB, `Asha-0.1.0.dmg` ~869 MB). The launcher
  and voice pipeline were boot-tested on the 2026-09-16 build. The shipped
  non-secret config loads, the default model auto-selects, and the voice
  pipeline comes up. **Unsigned** (add signing for a warning-free install).
- **Windows: build script + CI ready, not yet built here.** Run the
  `release` GitHub Action (Actions → release → Run workflow) to produce
  `Asha-Setup-<ver>.exe` on a Windows runner.
- **Self-host: `deploy/` ready, not tested here** (needs Docker/Linux).
- **OmniRoute: not bundled** (decision 2026-09-18). The old vendoring step
  downloaded the npm tarball and ran `npm install` (~2.1 GB of `node_modules`,
  Next.js); it repeatedly got OOM-killed on the dev machine, so it is gone from
  both build scripts. The app reaches a user-installed OmniRoute through
  `OMNIROUTE_BASE_URL` instead.

## Keyless builds — no credentials ship
Every build is **keyless**. The build scripts copy `shipped.env.template` (never
`prototype/.env`) into the bundle as `Resources/app/.env`, so the app contains
**no provider API keys at all**. Provider keys live only on the **Asha proxy
(Cloudflare)**; a managed-plan user's token is issued at sign-in and stored
outside the bundle.

- `shipped.env.template` carries only non-secret settings: the brain model name,
  the brain transport, and the local gateway base URL. It has **no** credential
  line.
- A build-time **keyless gate** (step 6a on macOS, step 5b-i on Windows) scans
  the first-party app code and the shipped `.env` for key-shaped strings and
  credential-shaped assignments. It **fails the build** if it finds one — fail
  closed, so a sneaked-in secret can never ship. The build also fails if
  `vendor/` or `runtime-node/` (the old bundled gateway) ever reappears.
- CI no longer bakes keys: the old `JARVIS_ENV_B64` "Bake keys" step in
  `.github/workflows/release.yml` is dead weight now (the build ignores
  `prototype/.env`). It should be removed (see the note in that workflow), but
  even if it runs it cannot put a key in the installer.

At startup the launcher logs a keyless attestation so support can confirm the
build without reading files:

```text
[config] keyless build: no provider API keys in app/.env; loaded: \
  JARVIS_BRAIN_MODEL=deepseek-v4.1-flash, JARVIS_BRAIN_TRANSPORT=omniroute, \
  OMNIROUTE_BASE_URL=http://127.0.0.1:20128/v1
```

macOS writes it to `<data>/logs/latest.log`; Windows to
`%LOCALAPPDATA%\Jarvis\logs\startup.log`.

## macOS
```bash
bash prototype/packaging/build_macos.sh            # arm64 (Apple Silicon)
# or: bash prototype/packaging/build_macos.sh x86_64
```
Outputs `prototype/packaging/dist/Asha.app` and `Asha-<ver>.dmg`.
Needs: `curl`, `tar`, `shasum`, `swiftc` (Xcode Command Line Tools). **Node/npm
are not needed.**

Sign + notarize (removes the "unidentified developer" warning):
```bash
codesign --deep --force --options runtime --sign "Developer ID Application: YOU" dist/Asha.app
xcrun notarytool submit dist/Asha-<ver>.dmg --keychain-profile notary --wait
xcrun stapler staple dist/Asha-<ver>.dmg
```

## Windows
```powershell
powershell -ExecutionPolicy Bypass -File prototype\packaging\build_windows.ps1
```
Outputs `prototype\packaging\dist\win\Asha\` (portable) and, if Inno Setup 6
(`ISCC.exe`) is installed, `prototype\packaging\dist\Asha-Setup-<ver>.exe`.
Needs: .NET 8 SDK, PowerShell. **Node/npm are not needed.**
WebView2 Runtime ships with Win10/11.

## CI (recommended — no local toolchains)
`.github/workflows/release.yml` builds **both** on a tag (`v0.1.0`) or manual run,
and uploads the `.dmg` and `.exe` as artifacts. This is how the Windows installer
gets built without a Windows machine.

## What goes in the bundle
```
Asha(.app)/
  launcher            Asha.exe / Contents/MacOS/Asha  → starts the app
  runtime/            relocatable CPython + site-packages (pipecat, onnx, …)
  app/                ui/ + gateway/  (the Asha code)
  models/             kokoro/ (onnx + voices), moonshine/   (voice, bundled)
  app/.env            shipped non-secret config (shipped.env.template) — no keys
  THIRD_PARTY_NOTICES.md  notices for bundled copyleft components (GPL/LGPL)
```
The bundle is **keyless**: `app/.env` is the non-secret template (model name,
brain transport, local gateway URL). No provider API key is copied, and the
keyless gate fails the build if one ever appears. There is **no `runtime-node/`
and no `vendor/`** — OmniRoute and its Node runtime are not bundled.
The launcher sets `KOKORO_MODEL_PATH`, `KOKORO_VOICES_PATH`, and
`MOONSHINE_VOICE_CACHE` at the bundled `models/`, so **voice never downloads
anything at runtime**.

## The optional OmniRoute add-on
A user who wants the local gateway installs OmniRoute themselves (npm) and runs
it, then points Asha at it. The app already reads these exact names
(`prototype/ui/server.py`, `_resolve_brain_transport`):

| Setting | Meaning |
|---|---|
| `JARVIS_BRAIN_TRANSPORT=omniroute` | select the local-gateway transport |
| `OMNIROUTE_BASE_URL` | base URL of the running gateway (default `http://127.0.0.1:20128/v1`) |
| `OMNIROUTE_API_KEY` | optional; the default keyless OmniRoute install does not need it for chat |

The shipped `app/.env` sets the first two; a user running OmniRoute elsewhere
only edits `OMNIROUTE_BASE_URL`. With no OmniRoute, switch
`JARVIS_BRAIN_TRANSPORT` to `deepseek` or `opencode` and supply that provider's
key. No app-code change is involved — this is configuration only.

**Where its state lives.** A user's own OmniRoute keeps its database, secrets
and logs wherever that install puts them; Asha never writes gateway state into
the bundle. Managed-plan proxy settings are the two non-secret/secret names
`JARVIS_GATEWAY_URL` (base URL) and `JARVIS_TOKEN` (issued at sign-in, never
shipped).

## Plans and where the keys live
- **Managed (plans):** provider keys live only in the **Asha proxy on
  Cloudflare** (Cloudflare secrets), never in the installer. The brain talks to
  a gateway, which forwards managed usage to the proxy. At build time set
  `JARVIS_PROXY_URL` (a base URL, not a secret) to bake `JARVIS_GATEWAY_URL`
  into the shipped `.env`; the user's `JARVIS_TOKEN` is issued at sign-in and
  stored outside the bundle.
- **BYOK (optional, user's own key):** if a user brings their own provider key,
  it is theirs and stays on their machine; it does not unlock paid features.
- There is no "demo with my key" build any more — that path shipped credentials
  and is gone.

## Versioning
Set `prototype/packaging/VERSION` (e.g. `0.1.0`); it names the `.dmg`/installer.

## Finish the Windows installer (no Windows machine needed)
1. Commit + push to GitHub.
2. Repo → **Actions → release → Run workflow** (or push a tag `v0.1.0`).
3. Download the `Jarvis-windows` artifact → `Asha-Setup-<ver>.exe`.
   (The CI artifact/job names still say "Jarvis" — that is an internal
   identifier, phase 2 of the rename.)

## Signing (removes the scary warnings)
- macOS: `codesign` + `notarytool` + `stapler` (Apple Developer ID).
- Windows: `signtool` with a code-signing cert on `Asha.exe` / the installer.
Until then: macOS → right-click → Open; Windows → More info → Run anyway.
