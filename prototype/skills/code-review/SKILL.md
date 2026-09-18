---
name: code-review
description: Review a code diff for correctness, security, and style; report concrete issues with file/line references and a clear verdict.
source: agentskills.io / opencode code-review pattern
verified: true
---

# Code Review

Given a diff, review it like a careful senior engineer:

1. Correctness first — does it do what the task asked? Check edge cases and
   error paths, not just the happy path.
2. Security — look for unsafe input handling, missing validation,
   mismatched types, or obvious injection/unchecked failure modes. Call these
   out first when found.
3. Style and size — keep it proportionate to the diff; don't nitpick the
   whole file, only the changed lines.
4. Each issue is one line: `<file>:<line> — what is wrong and why`.
5. End with a verdict the caller can act on (accept / rework / reject) and
   name the single most important fix if the verdict is not accept.
6. Never change the code — review only, leave the working tree untouched.