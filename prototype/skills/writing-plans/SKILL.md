---
name: writing-plans
description: Write a short numbered implementation plan — ordered steps, one file per step, a verification command with each step.
source: Asha supervisor brain (workbench Plans)
verified: true
---

# Writing Plans

Before any implementation, write a plan with these properties:

- Short and ordered: 3-8 numbered steps, each a concrete action an agent can
  take without more context.
- One file per step where possible; name the exact path.
- Each step ends with how to know it worked (a command or a visible result).
- Say what is intentionally NOT in scope, in one line at the end.
- The plan is the contract: the implementation must be checkable against it.

Example shape:

1. create `app.py` with a minimal HTTP route stub.
2. add the handler + wire the index page.
3. run `python app.py` and open the page to confirm.

Keep steps independent so a single failure is easy to isolate.