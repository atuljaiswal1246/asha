---
name: debugging
description: Systematically debug a failing program — reproduce, find the smallest cause, fix one thing at a time, verify.
source: agentskills.io / classic debugging discipline
verified: true
---

# Debugging

When a program fails, do not guess. Follow the loop:

1. Reproduce. Run the exact failing case; capture the real error text, not a
   paraphrase of it.
2. Read the error first. Trace the traceback/stack to the line that raised.
3. Isolate — shrink the input or the code to the smallest thing that still
   fails. Binary-search the change that introduced it if you can.
4. One hypothesis, one fix. Change exactly one thing, then re-run the original
   failing case.
5. Verify the fix and check for regressions (existing tests / related paths).
6. Only then summarize: root cause in one sentence + the fix applied.

Never edit files blindly; every change must be re-runnable and reversible.