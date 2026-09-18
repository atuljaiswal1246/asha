# Installing Asha on macOS

This page covers the released desktop app: installing the `.dmg`, getting past
the Gatekeeper warning, granting first-run permissions, finding your data and
logs, configuring the direct provider path (and the optional OmniRoute
gateway), troubleshooting, and uninstalling cleanly. It does not cover running from source — for that see the quick start in
the [README](../README.md).

## Before you start

- **macOS 15 or later, Apple silicon.** The bundled speech dependency
  (`moonshine-voice 0.1.5`) ships an arm64-only wheel tagged
  `py3-none-macosx_15_0_arm64`, and releases are built on a `macos-15` arm64
  runner. Intel Macs and Windows are not supported by the current bundle.
- **About 1.2 GB installed; the download is about 870 MB.** The app bundles a
  Python runtime and the voice models, so nothing is fetched or installed at
  first run. (These figures are from the local build; see
  [What could not be verified](#what-could-not-be-verified-for-this-page).)
- **A microphone**. Camera and Screen Recording are optional, needed only for
  the camera and see-the-screen features, and can be granted later.
- **The app is keyless, but a fresh install still needs a provider.** Asha ships
  with **no** provider API key of ours. The default is the **direct provider
  path**: one key you supply, no local router. A managed plan's keys live on the
  **Asha proxy** (hosted on Cloudflare) and are reached through a gateway; a BYOK
  key is yours and stays on your Mac. Without a key, Asha has no brain to talk
  to — see [You must provide a model provider](#you-must-provide-a-model-provider).
- **OmniRoute is not included.** The local router is an **optional add-on you
  install yourself** — see [The optional OmniRoute add-on](#the-optional-omniroute-add-on).
  It is one way to supply a provider, not a requirement.

## You must provide a model provider

Asha ships **keyless and with no provider configured**. The shipped template
points the brain at the **direct DeepSeek path**
(`JARVIS_BRAIN_TRANSPORT=deepseek` in `Asha.app/Contents/Resources/app/.env`),
so setup is one thing: **your own provider key**. Add it to that same file —
the exact file the app reads, and the one the first-run setup screen writes to.

The one required line:

```text
DEEPSEEK_API_KEY=your-key-here
```

`JARVIS_BRAIN_TRANSPORT=deepseek` and `JARVIS_BRAIN_MODEL=deepseek-v4.1-flash`
are already in the shipped file; `DEEPSEEK_BASE_URL` is optional (defaults to
`https://api.deepseek.com`). That is the whole setup: one key, done.

Prefer a different provider? These are the exact names the app reads
(`prototype/ui/server.py`, `_resolve_brain_transport`):

| Provider | Set in `app/.env` |
|---|---|
| **DeepSeek** (where the default brain model runs) | `JARVIS_BRAIN_TRANSPORT=deepseek` and `DEEPSEEK_API_KEY=…` |
| **Any OpenAI-compatible provider** (OpenRouter, OpenAI, a self-hosted gateway, …) | `JARVIS_BRAIN_BASE_URL=https://openrouter.ai/api/v1` and `JARVIS_BRAIN_API_KEY=…` |
| **OpenCode** (dev-oriented) | `JARVIS_BRAIN_TRANSPORT=opencode` and `SUPERVISOR_API_KEY=…` (or `OPENCODE_API_KEY=…`) |

`JARVIS_BRAIN_BASE_URL` (with `JARVIS_BRAIN_API_KEY`) works for any
OpenAI-compatible endpoint, which is how OpenRouter and most other providers
are used. If your provider names the model differently, override it with
`JARVIS_BRAIN_MODEL` (the shipped default is `deepseek-v4.1-flash`).

**Installing OmniRoute yourself is optional.** If you would rather run a local
router for multi-provider fallback or compression, install it separately and
set `JARVIS_BRAIN_TRANSPORT=omniroute` — see
[The optional OmniRoute add-on](#the-optional-omniroute-add-on). It is one
option, not a requirement.

**What happens with no provider configured.** Asha still starts and never
crashes. At startup it logs exactly one line naming the transport it resolved,
for example:

```text
[LLM] brain transport: transport=deepseek base=https://api.deepseek.com host=api.deepseek.com model=deepseek-v4.1-flash key_env=(none)
```

`key_env=(none)` means no key was found for that transport. A turn then reports
that the model is not reachable (or is busy) instead of failing silently
(`_emit_brain_error`, `prototype/ui/server.py:3642`). Nothing is lost; add a
provider as above and try again.

## Install

1. Open the [releases page](https://github.com/atuljaiswal1246/asha/releases)
   and download the macOS disk image. The asset is named
   `Asha-<version>.dmg`; for the current release that is
   `Asha-0.1.0.dmg` (the version is read from `prototype/packaging/VERSION`).
2. Double-click the `.dmg` to mount it. The window shows **Asha.app** (the
   image is built from the app folder, so there is no Applications shortcut in
   it).
3. Drag **Asha** into your **Applications** folder (open Applications in
   Finder and drag it there).
4. Eject the disk image (drag it to the Trash, or press the eject button in
   Finder).
5. Open Asha from **Applications**. Because the build is unsigned, the first
   open needs the Gatekeeper step below.

## The Gatekeeper warning (unsigned build)

The current release is **not signed with an Apple Developer ID and not
notarized**, so macOS cannot verify it and blocks the first launch with a
message like *"Asha cannot be opened because Apple cannot check it for
malicious software"* or *"…is from an unidentified developer"*. This is
expected for now.

Open it anyway:

1. In Finder, **Control-click** (or right-click) **Asha.app** and choose
   **Open**.
2. In the dialog, click **Open** again.
3. If macOS still refuses, open **System Settings → Privacy & Security**, find
   the notice about Asha near the bottom, and click **Open Anyway**.
4. After this first approval, Asha opens normally with a double-click.

If you prefer the command line, remove the quarantine attribute from the
installed app after dragging it to Applications:

```bash
xattr -dr com.apple.quarantine /Applications/Asha.app
```

Do not disable Gatekeeper globally. The right-click/Open-Anyway route or the
one-app `xattr` command above is enough.

## First run and permissions

The shipped app is keyless, so there is nothing provider-side to enter: it
walks you through a short onboarding ending with the microphone notice. If a
plan build asks you to sign in, that sign-in issues **your plan token** — it is
not a provider key, and it is stored outside the app bundle
(under `~/Library/Application Support/Jarvis`). If you choose to bring your own
provider key (BYOK, optional), that key is yours and stays on your Mac.

### Microphone (required for voice)

Asha's whole point is talking, so the microphone is required. macOS prompts
for it the first time; allow it. If you missed the prompt:

- **System Settings → Privacy & Security → Microphone**, enable **Asha**.

Without the microphone, Asha can still be used by typing, but it cannot hear
you.

### Screen Recording (optional — for "look at the screen")

The screen-reading tool captures your screen and reads it on-device (OCR), with
an optional vision model for questions. macOS requires **Screen Recording**
permission for the app doing the capture.

- Open **System Settings → Privacy & Security → Screen Recording** and enable
  **Asha** (or the terminal/host app, if you are running from source).
- If the permission is missing, Asha does not crash. It returns a plain
  message saying Screen Recording permission is likely missing and what to grant
  (`prototype/ui/screen_tool.py:213`, `:312`).

### Camera (optional — for "look at this")

The camera tool captures a single still frame when you ask Asha to look.

- macOS prompts on first use; allow it.
- To change it later: **System Settings → Privacy & Security → Camera**.
- If permission is refused, the tool reports "camera permission not granted"
  instead of failing silently (`prototype/ui/camera_tool.py:69`).

After granting Screen Recording or Camera, macOS may ask you to restart the app
for the change to take effect. Quit Asha with **Asha → Quit Asha** (or Cmd-Q)
and reopen it.

## Where your data and keys live

When you install the packaged app, everything user-specific stays on your Mac.
The shipped app is **keyless** — it contains no provider API key from us:

| What | Location |
|---|---|
| App data (memory, chat sessions, board, preferences) | `~/Library/Application Support/Jarvis` |
| Default working folder for coding/file tasks | `~/Jarvis` |
| Shipped non-secret config (model, transport, provider base URL) | `Asha.app/Contents/Resources/app/.env` |
| Your plan token (issued at sign-in, if you have a plan) | `~/Library/Application Support/Jarvis/gateway-token` |
| Google connector tokens | macOS Keychain, account `jarvis`, services `jarvis-google-*` |

> The data directory is still named `Jarvis` internally. The app now presents as
> **Asha**, but the internal identifier and data path are unchanged for now so
> existing installs keep their data (phase 2 of the rename).

**Provider API keys are not on your Mac at all.** They live only on the Asha
proxy (hosted on Cloudflare), which a gateway calls. The shipped `app/.env` is a
public, non-secret template — the brain model name, the brain transport, and the
provider base URL. Your plan token is not in the bundle: sign-in stores it
under `~/Library/Application Support/Jarvis/gateway-token` and a gateway reads it
from there. If you bring your own provider key (BYOK, optional), it is the only
provider key that ever touches your machine, and it stays in the app's own
`.env` / Keychain.

The data directory comes from `JARVIS_DATA_DIR`, set by the launcher
(`prototype/packaging/launcher/macos/Launcher.swift:73-74`); the default project
folder is `~/Jarvis` (`Launcher.swift:74-75`). The app's config path is
`_ENV_FILE` in `prototype/ui/server.py:454`, which resolves next to the app's
server code, i.e. inside the bundle for the packaged app.

At every launch the launcher writes a one-line attestation to
`~/Library/Application Support/Jarvis/logs/latest.log` naming the non-secret
settings it loaded and stating that the build is keyless, so support can confirm
it from the log alone:

```text
[config] keyless build: no provider API keys in app/.env; loaded: JARVIS_BRAIN_MODEL=deepseek-v4.1-flash, JARVIS_BRAIN_TRANSPORT=deepseek
```

## The optional OmniRoute add-on

Asha does **not** bundle a local gateway. OmniRoute is third-party software
(MIT) that you install separately; it is **optional** and not required to use
Asha, whose direct transports talk to a provider with your own key.

If you want it, install OmniRoute yourself (npm; see
<https://www.npmjs.com/package/omniroute>) and run it, then in
`Asha.app/Contents/Resources/app/.env`:

1. Set `JARVIS_BRAIN_TRANSPORT=omniroute` (it is commented out in the shipped
   file — uncomment it).
2. Point `OMNIROUTE_BASE_URL` at where it listens (default
   `http://127.0.0.1:20128/v1`; version 3.8.50 was tested with this).

Those are the exact names the app reads (`prototype/ui/server.py`,
`_resolve_brain_transport`); no code changes are needed. OmniRoute's own state
(its database, secrets, logs) lives in *your* install, not inside `Asha.app` —
Asha only calls the URL you give it. If `omniroute` is selected but nothing is
listening, either start it, or switch `JARVIS_BRAIN_TRANSPORT` back to
`deepseek` and supply your own key.

### Where the brain connects (transport settings)

The brain's endpoint and credentials are an env-driven choice, so the app is not
tied to any one provider. Resolution order, first match wins
(`prototype/ui/server.py`, `_resolve_brain_transport`):

1. **`JARVIS_BRAIN_BASE_URL`** — an explicit base URL. Highest precedence and
   fully backwards compatible with earlier builds.
2. **`JARVIS_BRAIN_TRANSPORT`** — an explicit transport selector:
   `deepseek`, `omniroute`, or `opencode`.
3. **Auto** — `deepseek` for the packaged app; `opencode` when running from
   source.

| Transport | Setting | Base URL (default) | Key variable |
|---|---|---|---|
| **DeepSeek direct (default)** | `JARVIS_BRAIN_TRANSPORT=deepseek` | `https://api.deepseek.com` (`DEEPSEEK_BASE_URL`) | `DEEPSEEK_API_KEY` |
| Custom base URL | `JARVIS_BRAIN_BASE_URL=…` | whatever you set | `JARVIS_BRAIN_API_KEY`, else `SUPERVISOR_API_KEY`, else `OPENCODE_API_KEY` |
| Local gateway (optional) | `JARVIS_BRAIN_TRANSPORT=omniroute` | `http://127.0.0.1:20128/v1` (`OMNIROUTE_BASE_URL`) | `OMNIROUTE_API_KEY` (optional) |
| OpenCode (dev-only) | `JARVIS_BRAIN_TRANSPORT=opencode` | `https://opencode.ai/zen/go/v1` | `SUPERVISOR_API_KEY`, else `OPENCODE_API_KEY` |

What the packaged app defaults to, and what is the fallback:

- **Packaged app:** the template sets `JARVIS_BRAIN_TRANSPORT=deepseek`, so the
  brain talks directly to DeepSeek with your `DEEPSEEK_API_KEY` — one key, no
  router. To use the optional local gateway instead, select `omniroute` (above).
- **Dev/source run:** the OpenCode endpoint above, so development is unchanged when
  nothing is configured.
- **OpenCode is a dev-only fallback:** it is never selected when a product setting
  (`JARVIS_BRAIN_BASE_URL` or `JARVIS_BRAIN_TRANSPORT`) is present. To run the brain
  with no OpenCode at all, set the DeepSeek-direct transport and `DEEPSEEK_API_KEY`.

The model is not changed by any of this — the brain still runs
`deepseek-v4.1-flash` (overridable only by the invisible `JARVIS_BRAIN_MODEL`).
If you use OmniRoute, make sure that model name resolves to a provider in the
gateway (e.g. a model alias/combo); that is gateway configuration, not app code.
The app asks OmniRoute to skip compression for brain requests
(`x-omniroute-compression: off`) so the prompt reaches the model unchanged.

**What is my brain actually talking to?** At startup the server logs exactly one
line (never the key):

```text
[LLM] brain transport: transport=deepseek base=https://api.deepseek.com host=api.deepseek.com model=deepseek-v4.1-flash key_env=DEEPSEEK_API_KEY
```

`key_env=(none)` means no key was found for the selected transport. A missing key
never stops the app starting; the brain simply reports that its model is
unavailable.

The Asha proxy (`proxy/`, hosted on Cloudflare) is reached **through** a
gateway. The two names are `JARVIS_GATEWAY_URL` (the proxy base URL, not a
secret) and `JARVIS_TOKEN` (your plan token, issued at sign-in). The **keyless
build ships no value for either**: the template keeps them commented out, and a
build can bake only the non-secret base URL by exporting `JARVIS_PROXY_URL`
(`JARVIS_TOKEN` is always added at runtime). Point your gateway at the proxy and
it routes managed usage through it — a configuration change, never a code
change.

## Logs

- **Packaged app:** `~/Library/Application Support/Jarvis/logs/latest.log` is a
  symlink to the current run's `server-<timestamp>.log` in the same folder. The
  newest 10 per-boot logs are kept and older ones are pruned
  (`Launcher.swift:141-166`). Send `latest.log` when reporting a problem.
- **Running from source:** `launch.py` runs the bot in the foreground, so logs
  go to that terminal. The supervisor scripts write to
  `/tmp/asha-ws.log`, `/tmp/asha-supervisor.log` and `/tmp/asha-static.log`
  (`prototype/ui/asha_supervise.sh`).

## Troubleshooting

**Asha opens but stays silent / never hears me.**
Check the microphone: **System Settings → Privacy & Security → Microphone →
Asha**. Also make sure the correct input device is selected in **System
Settings → Sound → Input**, and that it is not muted.

**Asha talks, but no sound comes out.**
Check the output device in **System Settings → Sound → Output** and the system
volume. This is a known failure mode the project is careful about
(`notes/ROADMAP.md`, "Report, don't touch").

**"Could not look" or an empty screen capture.**
Grant **Screen Recording** to Asha (above) and reopen the app. Until then the
tool correctly reports the missing permission.

**Camera does not work.**
Grant **Camera** to Asha, then reopen the app. If there is no camera, the tool
reports it instead of failing.

**The brain says its model is unavailable.**
Check the `[LLM] brain transport:` line in the log (above). If `key_env=(none)`,
no provider key was found: add your key (`DEEPSEEK_API_KEY` for the default
direct path) in `Asha.app/Contents/Resources/app/.env`. If it says
`transport=omniroute` and you have not installed OmniRoute, either install and
run it, or switch `JARVIS_BRAIN_TRANSPORT` back to `deepseek`. See
[The optional OmniRoute add-on](#the-optional-omniroute-add-on).

**A key was rejected (BYOK only).**
The shipped app needs no provider key; this only applies if you chose to bring
your own. Create a fresh key at your provider and paste it again on the setup
screen. The screen reports whether the key saved and whether the check passed.
A plan token is different: if sign-in fails, sign in again — the token is issued
by us and stored under `~/Library/Application Support/Jarvis/gateway-token`.

**"Another Asha server is already running."**
Only one process can own the WebSocket port (`:7860`). Quit the other copy of
Asha (or the manual `server.py` from a source run) before starting a new one
(`notes/jarvis-handoff.md`, section 6).

**Something looks broken after an update.**
Keep `latest.log` from just before the problem; that is the evidence the
maintainers need. Send it to **atul.j@hummingseo.com**.

## Updating

Download the new `.dmg` from the releases page, quit Asha, drag the new
**Asha** into **Applications**, and choose **Replace** when Finder asks.
Your data in `~/Library/Application Support/Jarvis` is untouched, and your plan
token (stored there) survives the replace. The bundle's `app/.env` is replaced
with the new non-secret template; if you had added a BYOK provider key or a
custom `OMNIROUTE_BASE_URL` to the bundle, re-enter it after the replace.

## Uninstall cleanly

1. Quit Asha: **Asha → Quit Asha** (Cmd-Q). If it will not quit, open
   Activity Monitor, find **Asha**, and quit it there.
2. Drag **Asha** from **Applications** to the Trash, or run:
   ```bash
   rm -rf /Applications/Asha.app
   ```
   (This removes only the keyless app. Any BYOK key you added lived in the
   bundle and goes with it; nothing provider-side of ours was ever there.)
3. Remove your data:
   ```bash
   rm -rf "$HOME/Library/Application Support/Jarvis"
   ```
   This also removes your plan token (`gateway-token`). It does **not** touch a
   separate OmniRoute install you made yourself — uninstall that by its own
   instructions.
4. If you used the Google connectors, remove the tokens from the Keychain.
   Open **Keychain Access**, search for `jarvis-google`, and delete the matching
   items. From the command line:
   ```bash
   security delete-generic-password -a jarvis -s jarvis-google-gmail 2>/dev/null
   security delete-generic-password -a jarvis -s jarvis-google-google-calendar 2>/dev/null
   ```
5. Optionally remove the default working folder if you let Asha create it:
   ```bash
   rm -rf "$HOME/Jarvis"
   ```
   Do this only if you are sure there is no work of your own in there.

---

### What could not be verified for this page

- The exact wording of the current macOS Gatekeeper dialog, since it varies by
  macOS version; the steps above match the standard behaviour rather than a
  captured screenshot.
- The published release assets were not downloaded here; the file name and
  version pattern come from `.github/workflows/release.yml` and
  `prototype/packaging/VERSION`.
- The `.dmg` size (869 MB) and installed size (1.2 GB) are from the local
  `Asha.app` build run on 2026-09-18
  (`prototype/packaging/dist/Asha.app` / `Asha-0.1.0.dmg`) after the OmniRoute
  bundling was removed; the published artifact should match, though the exact
  compressed size can vary slightly.
- OmniRoute's own install commands and current health endpoints were not re-run
  for this page; the `GET /api/health` → `200` and dashboard behaviour cited are
  from the version 3.8.50 test recorded in
  `notes/research/omniroute-real-path-2026-09-18.md` (today, on this machine).
