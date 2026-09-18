# Asha

**Asha is the voice layer for the coding agent you already pay for — keep your OpenCode plan and key, talk instead of type, hear the diff back.**

Asha doesn't replace your terminal agent — it talks to it: you speak a coding task, Asha delegates to the agent running on *your own key* (including your OpenCode key), and reports the result back by voice. Speech is on-device (Moonshine STT + Kokoro TTS in-process — no cloud speech bill), and Asha carries whole-day context your terminal never sees: mail, calendar, screen, camera, memory. MIT-licensed and open source, same as OpenCode — one Mac app to install, no terminal required, no new subscription required if you BYOK.

**A voice-first assistant that does work on your Mac.** You talk; Asha listens
on-device, plans the task, runs a small team of agents (files, shell, web,
connectors, media), and answers out loud.

It is in preview and BYOK: the app is free, you bring your own model key, and
we never see it. An existing OpenCode (or OpenRouter) key works as-is — keep
your existing plan, add a voice; there is no Asha account and no server in the
middle.

Asha is open source under the [MIT licence](LICENSE) at
<https://github.com/atuljaiswal1246/asha>. Contributions are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md).

---

## Features and honest status

Status is derived from the code, not from marketing. "Working" means it runs
end-to-end today; "Experimental" means it works but has a real caveat; "Pending"
means do not count on it yet.

| Feature | Status | Notes / where |
|---|---|---|
| Voice conversation (push-to-talk, interrupt/barge-in) | Working | `prototype/ui/server.py`, frozen VAD tuning |
| On-device speech recognition (Moonshine) | Working | `server.py:6910` |
| On-device speech synthesis (Kokoro) | Working | `server.py:6916` |
| Backchannel ("mm-hm" while it thinks) | Working | `backchannel.py` |
| Native agent loop: read/write/edit files, shell, search, LSP | Working | `agent_loop.py` |
| Plan mode (research only), skills (`SKILL.md`), memory | Working | `skills.py`, `memory.py` |
| MCP connectors (local stdio + remote HTTP + registry discovery) | Working | `mcp_client.py`, `mcp_screen.py` |
| See the screen (capture + on-device OCR, vision model on request) | Working | `screen_tool.py`; needs Screen Recording permission |
| Camera ("look at this") | Working | `camera_tool.py`; needs Camera permission |
| Image generation (Leonardo BYOK, free Pollinations fallback) | Working | `imagegen_tool.py` |
| Projects board (Asha + you write it; agents read-only) | Working | `board.py` |
| Gmail / Google Calendar connectors | Experimental | `google_auth.py`; needs your own Google OAuth client, personal/test use until Google verification (public distribution is pending) |
| Figma file inspection over REST with your token | Working | `figma_rest.py` |
| Video generation via MoneyPrinterTurbo | Experimental | `mpt_video_tool.py`; separate local service you run yourself |
| Scheduler / cron jobs | Experimental | `scheduler.py`; off by default (`CRON_ENABLED=0`) |
| Wake word "Hey Jarvis" | Pending | backlog |
| Windows installer | Pending | build script exists, not runtime-verified on real hardware |
| Linux / self-host (`deploy/`) | Experimental | Docker path in the repo, not tested here |
| Hosted plans / quota gateway | Pending | `prototype/gateway/` is designed and unit-tested, not launched; the current launch is BYOK-only |

Frozen by the project owner and not to be changed as a side effect: VAD tuning
(`VAD_START_SECS=0.25`, `VAD_STOP_SECS=0.25`, `VAD_CONFIDENCE=0.75`,
`VAD_MIN_VOLUME=0.7`) — see the defaults in `prototype/ui/server.py:7709-7712`.

## Requirements

- **macOS 15 or later, Apple silicon.** The bundled speech dependency is an
  arm64-only wheel: `moonshine-voice 0.1.5` has wheel tag
  `py3-none-macosx_15_0_arm64`, and the release workflow builds on
  `macos-15` (`build_macos.sh arm64`). Intel Macs and Windows are not supported
  by the current bundle.
- **From source:** Python **3.11+** (`prototype/requirements.txt` line 1; the
  development venv is 3.13, the packaged runtime pins 3.12.14).
- **Disk:** a built app is roughly 900 MB, because the Python runtime,
  dependencies and voice models are all bundled. The `.dmg` is produced by
  `prototype/packaging/build_macos.sh`; it is not committed to the repo.
- A microphone. Camera and Screen Recording are optional and only needed for
  those features.

## Quick start (from source)

```bash
git clone https://github.com/atuljaiswal1246/asha.git
cd asha

python3 -m venv .venv
.venv/bin/pip install -r prototype/requirements.txt

.venv/bin/python prototype/ui/launch.py
```

`launch.py` starts the WebSocket bot (`server.py` on `127.0.0.1:7860`) and the
static UI (`http.server` on `127.0.0.1:8000`), then opens
<http://127.0.0.1:8000> (`prototype/ui/launch.py:70-84`). On first run Asha
asks for your key (see below); after that, allow the microphone and talk.

To run the two processes by hand instead:

```bash
# terminal 1 - the bot
cd prototype/ui
../../.venv/bin/python -u server.py

# terminal 2 - the static UI
cd prototype/ui
../../.venv/bin/python -m http.server 8000 --bind 127.0.0.1 --directory static
```

There are also 24/7 supervisor and native-window scripts
(`prototype/ui/start_asha.sh`, `stop_asha.sh`, `asha_supervise.sh`) that keep the
app running in the background. They hard-code an absolute checkout path from the
maintainer's machine, so they only run from that exact location; edit the path at
the top of the script to use them from your own checkout (see "Known
limitations" below).

Run the test gate with:

```bash
./scripts/test.sh
```

## Install from the release

1. Open the [releases page](https://github.com/atuljaiswal1246/asha/releases)
   and download `Asha-0.1.0.dmg` (the build names the disk image
   `Asha-<version>.dmg`; the version lives in `prototype/packaging/VERSION`).
2. Open the `.dmg` and drag **Asha** into your **Applications** folder.
3. **The build is unsigned and not notarized**, so macOS Gatekeeper will warn.
   Right-click (or Control-click) the app and choose **Open**, then confirm.
   After the first launch it opens normally. Full steps and troubleshooting:
   [`docs/install.md`](docs/install.md).

## BYOK: how the key works

- **What the key is for.** Asha's thinking is a hosted model, reached over
  HTTPS with your key. The default brain is `deepseek-v4.1-flash`
  (`server.py:202`) on the OpenCode go gateway (`server.py:205`), and the setup
  screen asks for an **OpenCode key** (`prototype/ui/static/index.html:431`).
  Get one at opencode.ai: sign in, open account settings, API keys, create a key
  (`index.html:448-450`).
- **Keys stay on your machine.** For BYOK there is no Asha account and no
  Asha server in the request path — the app calls the provider you configured
  directly. The setup screen says it plainly: "Stored only on this Mac. It
  never leaves your machine." (`index.html:460`).
- **Where the key goes.** The first-run screen sends `provider_key_set`; the
  server writes it to the app's `.env` via `_provider_key_save()`
  (`server.py:457`, `_ENV_FILE` at `server.py:454`):
  - running from source: `prototype/.env` (gitignored);
  - packaged app: `Asha.app/Contents/Resources/app/.env`.
  Additional providers (OpenRouter, OpenAI, Anthropic, Gemini, xAI) can be added
  later under Settings → API keys (`static/app.js:2914-2921`).
- **Optional keys are separate and yours too:** web search (Exa), image
  generation (Leonardo; Pollinations needs no key), Figma, and Google are all
  bring-your-own, and each is only used if you configure it.
  `prototype/.env.example` lists the names.

## What runs locally, what talks to the network

- **On your Mac:** speech recognition (Moonshine), speech synthesis (Kokoro),
  voice-activity detection, screen capture with on-device OCR, and camera
  capture. Packaged builds point at the bundled models so voice downloads
  nothing at runtime (`Launcher.swift:61-63`, `build_macos.sh:76-90`).
- **Over the network, only when used:** the model provider you configured
  (OpenCode's gateway by default), web search (Exa), image generation
  (Leonardo), Google connectors, Figma's REST API, and any MCP server you add.

## Project layout

```
prototype/ui/         the app: server, native agent loop, tools, static UI
prototype/gateway/    hosted quota gateway for future plans (not launched)
prototype/packaging/  build the macOS .app/.dmg and Windows installer
deploy/               self-host path (Docker/install.sh), untested here
docs/                 public install guide (docs/install.md)
scripts/              dev tooling: test gate, client, agent harness
website/              public project site
```

## Contributing

Issues and pull requests are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) for the dev setup, the test gate and the PR
expectations. Keep changes scoped and run `./scripts/test.sh` before opening a
PR. Note that the `server tools` suite writes into whatever project is currently
selected — switch to a scratch project first if that matters to you. For
security reports, use [SECURITY.md](SECURITY.md); for anything else, contact
**atul.j@hummingseo.com**.

## Licence

MIT — see [LICENSE](LICENSE). You are free to use, modify and redistribute
Asha, including commercially, under the terms of that licence. The optional
paid plans are a separate hosted service, not a licence restriction.
Third-party components shipped with the app remain under their own licences; see
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

---

### Known limitations

- `prototype/ui/start_asha.sh`, `stop_asha.sh` and `asha_supervise.sh` hard-code
  an absolute checkout path from the maintainer's machine, so they are not
  portable to a checkout at any other path.
- `prototype/ui/run_ui.sh` calls a `serve_local.sh` that does not exist in the
  tree, and it checks a local `llama-server` that was removed by design
  (`asha_supervise.sh:4-5`); use `launch.py` instead.
