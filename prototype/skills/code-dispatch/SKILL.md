---
name: code-dispatch
description: "Manage coding tasks dispatched to opencode: sessions, permissions, follow-ups, abort."
version: 1.0.0
author: Asha
license: Proprietary (all rights reserved)
platforms: [macos]
metadata:
  hermes:
    tags: [coding, opencode, dispatch, session, permissions, brief]
    related_skills: [voice-debugging]
---

# Code Dispatch

## Overview

Use this skill when working with Asha's coding dispatch pipeline: creating sessions, handling permission prompts, following up on active sessions, or aborting stuck tasks.

## Key Concepts

- **Session lifecycle**: `dispatch(brief)` → agent works → `followup(answer)` → `read_result()` → `abort_session()` or `commit_revert()`
- **Permission prompts**: opencode asks permission before dangerous operations. Asha relays these to the UI and waits for user approval.
- **State machine**: `_code_state` tracks the current coding task. States: `idle`, `briefing`, `dispatching`, `running`, `awaiting_permission`, `awaiting_followup`, `result`.

## Common Operations

### Dispatch a coding task
```python
result = await dispatch(brief, workspace_path="/path/to/repo")
# Returns: {"session_id": "...", "status": "dispatched"}
```

### Handle permission prompt
```python
# UI sends: {"type": "code_permission_reply", "session_id": "...", "reply": "allow"}
# Server calls: POST /api/session/{id}/permission/{perm_id}/reply
```

### Abort a running session
```python
await abort_session(session_id)
# Emits CodingResultFrame with abort message
```

## Error Patterns

- **WorkerFailed**: Agent crashed. Retry with `GO_FALLBACK_TABLE` fallback.
- **Session timeout**: Check opencode serve is running (`curl :4096/health`).
- **Permission denied**: UI rejected the prompt. Session may need manual revert.
