---
name: hello-world
description: Write a minimal, dependency-free hello world program in any language — the smallest correct version, runnable directly.
source: anthropics/skills (hello-world)
verified: true
---

# Hello World

Write a program that prints "hello" (or "hello world") and nothing else.

Rules:
- One file, no dependencies, no build step.
- The program must run directly: `python hello.py`, `node hello.js`, `go run main.go`, etc.
- Fix the language from the request; default to python.
- Keep it minimal — no args parsing, no logging setup, no wrappers.
- Verify by running it and confirming the exact output.
- Touch nothing else in the workspace.