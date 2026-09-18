# Contributing to Asha

Thanks for wanting to help. Asha is a voice-first assistant for macOS, and it
is early: the most useful contributions right now are focused bug fixes, tests,
docs, and small, well-scoped features. This file tells you how to set up, run
the tests, and get a change merged.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). For
security issues, do **not** open a public issue — follow [SECURITY.md](SECURITY.md).

## Requirements

- **macOS 15 or later, Apple silicon.** The bundled speech dependency
  (`moonshine-voice==0.1.5`) ships an arm64-only wheel, so Intel Macs and
  Windows cannot run the current app. An Intel build can be produced in
  principle but is untested; see the README.
- **Python 3.11+.** The development venv is 3.13 and the packaged runtime pins
  3.12.14.
- A microphone if you want to exercise the voice loop.

## Set up a dev environment

```bash
git clone https://github.com/atuljaiswal1246/asha.git
cd asha

python3 -m venv .venv
.venv/bin/pip install -r prototype/requirements.txt
```

Run the app from source:

```bash
.venv/bin/python prototype/ui/launch.py
```

That starts the WebSocket bot on `127.0.0.1:7860` and the static UI on
`127.0.0.1:8000`, then opens <http://127.0.0.1:8000>. On first run it asks for a
model key (BYOK). You can also type instead of talking.

The app's own working files live under `prototype/`. Never commit anything from
`.env` or any `*.env` file; `.env` is gitignored for exactly this reason.

## Run the tests

The committed test gate is:

```bash
./scripts/test.sh
```

It runs the coding-path suites in `prototype/ui/` (apply-patch, orchestrator,
permissions, sessions, MCP client, skills, backends, scheduler, channels, LSP
client, agent loop, server tools) and fails loudly on the first failure. Some
suites skip cleanly if optional tools such as `pylsp` are not installed. Run it
before opening a pull request, and paste the result in the PR.

Heads-up: the `server tools` suite writes into whatever project is currently
selected in the app. Switch to a scratch project first if that matters to you.

## Pull requests

- **Keep it scoped.** One concern per PR; split unrelated changes out. Small
  diffs get reviewed and merged faster.
- **Explain the change.** Say what it does, why, and how you verified it. Link
  the issue it fixes (`Fixes #123`).
- **Run the gate.** `./scripts/test.sh` must pass. If a suite can't run in your
  environment, say so explicitly.
- **Add or update tests** for behaviour changes where a test suite already
  covers that area.
- **No secrets, ever.** No keys, tokens, `.env` contents, or personal paths in
  code, tests, docs, or screenshots.
- **Be honest in docs.** If something is experimental or unverified, say so
  rather than implying it works.
- **Licence.** All contributions are accepted under the repository's
  [MIT licence](LICENSE).

## Coding conventions

The project is Python, mostly under `prototype/ui/`. Match the style of the file
you are editing; a few conventions that reviewers will look for:

- **Read before you change.** Look at the surrounding code and imports, and
  follow the existing patterns rather than introducing a new library or
  framework for one call site.
- **Keep diffs minimal.** Change what the task needs and nothing else.
- **Config, env vars, and scripts are sensitive.** Do not change them without
  explicit maintainer agreement on the PR — this includes the frozen voice/VAD
  tuning in `prototype/ui/server.py`.
- **Report, don't touch.** If you notice something odd that is unrelated to your
  change, open an issue instead of fixing it in the same PR. Do not change
  working behaviour as a side effect of another task.
- **Comments and naming.** Prefer clear names; keep comments minimal and about
  *why*, not *what*.
- **File ownership while working in parallel.** If you are coordinating with
  other contributors, one person per file; two people editing the same file at
  once causes clobbers.

## Where to start

- The README has a feature/status table and an honest "what works today" list.
- `docs/` holds the published install guide; `prototype/` is the app, with most
  of the Python under `prototype/ui/`.
- Issues labelled `good first issue` are scoped for a first PR.

## Questions

Open an issue, or email **atul.j@hummingseo.com**.
