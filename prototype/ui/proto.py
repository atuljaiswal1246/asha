import base64
import json
import os
import struct
import time

from pipecat.frames.frames import (
    Frame,
    LLMTextFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

from pipecat.frames.frames import TTSTextFrame

TTS_TEXT_CLS = TTSTextFrame

# MIC throttle: max one log per _MIC_LOG_INTERVAL seconds
_MIC_LOG_INTERVAL = 5.0
_last_mic_log_time = 0.0
_mic_chunks_since_log = 0
_mic_samples_since_log = 0


class AudioToggleFrame(Frame):
    def __init__(self, enabled: bool):
        super().__init__()
        self.enabled = enabled


_MAX_CHAT_IMAGES = 3
_MAX_CHAT_IMAGE_BYTES = 8 * 1024 * 1024
_CHAT_IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp")


def _parse_chat_images(msg):
    """Validate + decode attached chat images from a {type:"text"} payload.

    Returns a list of (mime, raw_bytes, w, h). Malformed / oversized items
    are dropped silently — an image turn degrades to text-only, never a crash.
    w/h come from the client (canvas size); used for the vision frame only.
    """
    out = []
    raw = msg.get("images", []) or []
    for item in raw[:_MAX_CHAT_IMAGES]:
        try:
            if not isinstance(item, dict):
                continue
            url = item.get("dataUrl", "")
            w = int(item.get("w", 0))
            h = int(item.get("h", 0))
            if not isinstance(url, str) or not url.startswith("data:image/"):
                continue
            if w <= 0 or h <= 0 or w > 4000 or h > 4000:
                continue
            header, _, b64 = url.partition(",")
            mime = header.split(";")[0].split(":")[1] if ":" in header else ""
            if mime not in _CHAT_IMAGE_MIMES or not b64:
                continue
            blob = base64.b64decode(b64)
            if not blob or len(blob) > _MAX_CHAT_IMAGE_BYTES:
                continue
            out.append((mime, blob, w, h))
        except Exception:
            continue
    return out


_MAX_CHAT_FILES = 3
_MAX_CHAT_FILE_BYTES = 10 * 1024 * 1024  # 10MB per file


def _parse_chat_files(msg):
    """Validate + decode non-image file attachments from a {type:"text"} payload.

    Returns a list of dicts: {name, mime, text, raw_bytes}.
    For images, text is empty and raw_bytes is set.
    For text/PDF/code, text is the extracted content and raw_bytes is the raw data.
    Malformed / oversized items are dropped silently.
    """
    out = []
    raw = msg.get("files", []) or []
    for item in raw[:_MAX_CHAT_FILES]:
        try:
            if not isinstance(item, dict):
                continue
            data_url = item.get("dataUrl", "")
            name = item.get("name", "unknown")
            mime = item.get("mime", "")
            if not isinstance(data_url, str) or not data_url.startswith("data:"):
                continue
            header, _, b64 = data_url.partition(",")
            if not b64:
                continue
            blob = base64.b64decode(b64)
            if not blob or len(blob) > _MAX_CHAT_FILE_BYTES:
                continue
            text_content = ""
            if mime == "application/pdf":
                text_content = _extract_pdf_text(blob)
            elif mime.startswith("text/") or mime in (
                "application/json", "application/xml", "text/x-python",
                "text/javascript", "text/typescript", "text/x-shellscript",
                "text/markdown", "text/yaml", "text/csv",
            ):
                try:
                    text_content = blob.decode("utf-8", errors="replace")
                except Exception:
                    text_content = blob.decode("latin-1", errors="replace")
            if text_content:
                # Truncate absurdly long files to stay within context budget
                max_chars = 30_000
                if len(text_content) > max_chars:
                    text_content = text_content[:max_chars] + f"\n\n[truncated — {len(text_content):,} chars total]"
            out.append({"name": name, "mime": mime, "text": text_content, "raw": blob})
        except Exception:
            continue
    return out


def _extract_pdf_text(blob):
    """Extract text from PDF bytes via pdfplumber. Returns empty string on failure."""
    try:
        import io
        import pdfplumber
        with pdfplumber.open(io.BytesIO(blob)) as pdf:
            pages = []
            for page in pdf.pages[:30]:  # cap at 30 pages
                t = page.extract_text()
                if t:
                    pages.append(t)
            return "\n\n--- page break ---\n\n".join(pages)
    except Exception:
        return ""


class ImageTextFrame(TranscriptionFrame):
    """TranscriptionFrame + attached user images AND file attachments.

    Images ride the frame into on_text, which queues UserImageRawFrames ahead
    of the text so the shared LLM context carries OpenAI image parts natively.
    Files (PDFs, text, code) are extracted into text and injected as context.
    isinstance-compatible with TranscriptionFrame (aggregator untouched).
    """

    def __init__(self, *args, images=None, files=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.images = images or []
        self.files = files or []


class ImageTextFrame(TranscriptionFrame):
    """TranscriptionFrame + attached user images for vision turns.

    isinstance-compatible with TranscriptionFrame (aggregator untouched).
    """

    def __init__(self, *args, images=None, files=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.images = images or []
        self.files = files or []


class StatusFrame(Frame):
    """UI status signal. Known states: "loading", "ready", "coding".
    "coding" = Ashish background coding in progress (deferred delivery)."""
    def __init__(self, state: str = "loading"):
        super().__init__()
        self.state = state


class BrainFrame(Frame):
    """Signals the UI which LLM provider is currently active."""
    def __init__(self, provider: str, model: str = ""):
        super().__init__()
        self.provider = provider   # "OpenCode-Free" | "OpenCode-Go" | "Local"
        self.model = model


class BrainConfigFrame(Frame):
    """Current brain model + reasoning config + onboarding state.

    Pushed on connect and after brain_model_set / reasoning_set /
    provider_key_set.

    model: None (no model chosen yet — onboarding notice 1 shows) or a model
           id from the opencode model list.
    reasoning: current reasoning effort ("default", "none", "low", "medium",
               "high").
    models: the full deployable opencode model list (zen + go), as
            [{"id": ..., "name": ...}] — drives the Brain dropdown (never
            hardcoded in the client).
    key_present: whether an opencode API key is saved. When False the UI shows
                 the API-key popup before any onboarding notices.
    onboarded: True only after the user pressed OK on the final (mic) notice.
               Until then a reload resumes onboarding (skipping steps already
               done), so the mic notice is never silently skipped.
    """
    def __init__(self, model: str | None = None, reasoning: str = "default",
                 models: list | None = None, key_present: bool = True,
                 onboarded: bool = False):
        super().__init__()
        self.model = model
        self.reasoning = reasoning
        self.models = models or []
        self.key_present = key_present
        self.onboarded = onboarded


class BrainActivityFrame(Frame):
    """Live "what the brain is doing" signal for the current voice turn.

    Mirrors the activity disclosure in ChatGPT / opencode: the UI can show the
    reasoning phase, live reasoning text, and tool usage while the model works.

    phase — "reasoning" (thinking text streaming), "tool" (running a tool),
            "answering" (final answer began), "done" (turn finished)
    text  — incremental reasoning text (reasoning) or answer text
    tool  — tool name when phase == "tool"
    detail— short tool argument/command summary (optional)
    """
    def __init__(self, phase: str = "", text: str = "",
                 tool: str = "", detail: str = ""):
        super().__init__()
        self.phase = phase
        self.text = text
        self.tool = tool
        self.detail = detail


class BrainErrorFrame(Frame):
    """A turn failed and the user should see why (rate limit, bad key, ...).

    kind     — "rate_limit" | "auth" | "unavailable" | "error"
    message  — short, human sentence to show in the UI
    model    — the model id that failed (may be empty)
    """
    def __init__(self, kind: str = "error", message: str = "",
                 model: str = ""):
        super().__init__()
        self.kind = kind
        self.message = message
        self.model = model


class MemoryFrame(Frame):
    """Pushes the full memory list to the UI for the 'What Jarvis remembers' panel.

    entries: list of dicts, each with keys:
        kind  — "user" (USER.md) or "memory" (MEMORY.md)
        text  — the entry content
        id    — stable identifier for edit/delete operations
    """
    def __init__(self, entries: list[dict] | None = None):
        super().__init__()
        self.entries = entries or []


class CodingResultFrame(Frame):
    """Coding-task result bubble — distinct from normal bot_text.

    Emitted by server.py when a coding task (propose/apply/resolve) returns
    a result that should render with the amber coding-result style in the UI.
    """
    def __init__(self, text: str):
        super().__init__()
        self.text = text


class AgentPartFrame(Frame):
    """One agent output part from a live coding session (opencode serve).

    kind: "reasoning" | "text" | "tool"
    For kind="tool": tool, status, command, output are populated.
    output is truncated to ~2000 chars server-side; truncated flag signals UI.
    """
    def __init__(
        self,
        kind: str,
        text: str = "",
        tool: str = "",
        status: str = "",
        command: str = "",
        output: str = "",
        truncated: bool = False,
    ):
        super().__init__()
        self.kind = kind
        self.text = text
        self.tool = tool
        self.status = status
        self.command = command
        self.output = output
        self.truncated = truncated


class WorkSessionsFrame(Frame):
    """Session list for the workbench — pushed on Work-mode entry.

    sessions: list of {id, title, time:{created,updated}} from GET /api/session.
    """
    def __init__(self, sessions: list[dict] | None = None):
        super().__init__()
        self.sessions = sessions or []


class WorkerActivityFrame(Frame):
    """Live activity update for a single worker panel.

    worker_id: opencode session id
    status: running | completed | error | idle
    activity: list of {kind, tool, status, text, file} entries (most recent last)
    title: session title
    agent: agent type (explore, build)
    """
    def __init__(self, worker_id: str = "", status: str = "idle",
                 activity: list[dict] | None = None,
                 title: str = "", agent: str = ""):
        super().__init__()
        self.worker_id = worker_id
        self.status = status
        self.activity = activity or []
        self.title = title
        self.agent = agent


class ProjectsFrame(Frame):
    """Active project + MRU project list for the work view selector.

    current: absolute worktree path of the system-wide active project
    projects: list of {worktree, name, updated_at} (MRU first)
    Additive message — the whole system (Brain + all workers) shares the
    active project, so switching here retargets every leg.
    """
    def __init__(self, current: str = "", projects: list[dict] | None = None):
        super().__init__()
        self.current = current
        self.projects = projects or []


class FsListingFrame(Frame):
    """One directory listing for the server-side folder picker.

    path    — absolute path being shown
    parent  — absolute parent path ("" when at an allowed root)
    entries — [{name, path, git}] immediate subdirectories, git repos flagged
    error   — non-empty when the path could not be listed
    """
    def __init__(self, path: str = "", parent: str = "",
                 entries: list[dict] | None = None, error: str = ""):
        super().__init__()
        self.path = path
        self.parent = parent
        self.entries = entries or []
        self.error = error


class FsNativeResultFrame(Frame):
    """Result of the native (macOS) folder dialog.

    path  — chosen absolute path, or "" when cancelled
    error — non-empty when the dialog could not run
    """
    def __init__(self, path: str = "", error: str = ""):
        super().__init__()
        self.path = path
        self.error = error


class WorkersFrame(Frame):
    """Per-worker config for the Work view (the 3 muscles).

    workers: [{index, project, project_name, model, model_name, reasoning}]
    """
    def __init__(self, workers: list[dict] | None = None):
        super().__init__()
        self.workers = workers or []


class AgentsFrame(Frame):
    """Live status of the permanent agents, for the sidebar cue.

    agents: [{id, name, model, color, status, brief_title, note, seconds}]
    tasks:  live agent tasks under the roster
            [{id, agent_id, agent_name, title, status, note, seconds, ...}]
    running: int
    total: int
    """
    def __init__(self, agents: list[dict] | None = None, running: int = 0,
                 total: int = 0, tasks: list[dict] | None = None):
        super().__init__()
        self.agents = agents or []
        self.running = running
        self.total = total
        self.tasks = tasks or []


class ProjectGateFrame(Frame):
    """Phase 3: coding was requested but no project is chosen yet — the UI
    should prompt and open the project picker."""
    def __init__(self, message: str = ""):
        super().__init__()
        self.message = message


class CatalogFrame(Frame):
    """Model catalog grouped by provider, so a worker can pick any provider's
    model (its own pool). providers: [{id, name, models: [{id, name, free?}]}]"""
    def __init__(self, providers: list[dict] | None = None):
        super().__init__()
        self.providers = providers or []


class RecentSessionsFrame(Frame):
    """Recent chat sessions for the sidebar 'Recent' list.

    sessions: list of {session_id, title, message_count, last_time}
    Sorted most-recently-active first. Push on connect and on new-chat.
    """
    def __init__(self, sessions: list[dict] | None = None):
        super().__init__()
        self.sessions = sessions or []


class SessionMessagesFrame(Frame):
    """Full stored message log for one session, oldest first.

    messages: list of {role, text, time}
    """
    def __init__(self, messages: list[dict] | None = None):
        super().__init__()
        self.messages = messages or []


class BotImageFrame(Frame):
    """Bot-generated image for the chat — url is a static-server path
    (e.g. /generated/<uuid>.jpg), caption is short spoken-safe text."""
    def __init__(self, url: str = "", caption: str = ""):
        super().__init__()
        self.url = url
        self.caption = caption


class UIFrameSerializer(FrameSerializer):
    """Screen/serializer for the minimal browser UI.

    Server -> client: JSON messages (user/bot transcript, TTS state) and bot
    audio as JSON {type:"audio"} base64 PCM16.
    Client -> server: {type:"audio"} base64 PCM16 @16k.

    Apply-gate wire format (§6A.3):
      Server -> UI:  {"type":"proposal", "text":"...", "session_id":"..."}
      UI -> server:  {"type":"approve",  "session_id":"..."}
      UI -> server:  {"type":"reject",   "session_id":"..."}
    The proposal bubble is shown until the user acts or stops.

    Memory wire format (M2c):
      Server -> UI:  {"type":"memory", "entries":[{"kind":"user"|"memory","text":"...","id":"..."}]}
      UI -> server:  {"type":"memory", "action":"edit",  "id":"...", "text":"..."}
      UI -> server:  {"type":"memory", "action":"delete","id":"..."}
    The memory panel refreshes from the latest {type:"memory"} push.

    Permission wire format (Muse Spark agent access):
      Server -> UI:  {"type":"permission", "permissionID":"...", "label":"...", "detail":"..."}
      UI -> server:  {"type":"permission_response", "permissionID":"...", "response":"allow"|"deny", "remember":false}
    Default-deny on server side if user doesn't act; UI just clears on status update.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._text_cb = kwargs.get("text_cb")
        self._approve_cb = kwargs.get("approve_cb")
        self._reject_cb = kwargs.get("reject_cb")
        self._stop_cb = kwargs.get("stop_cb")
        self._permission_cb = kwargs.get("permission_cb")
        self._worker_permission_cb = kwargs.get("worker_permission_cb")
        self._memory_cb = kwargs.get("memory_cb")
        self._work_mode_cb = kwargs.get("work_mode_cb")
        self._project_set_cb = kwargs.get("project_set_cb")
        self._projects_cb = kwargs.get("projects_cb")
        self._brain_provider_cb = kwargs.get("brain_provider_cb")
        self._provider_config_cb = kwargs.get("provider_config_cb")
        self._provider_key_cb = kwargs.get("provider_key_cb")
        self._provider_custom_cb = kwargs.get("provider_custom_cb")
        self._mcp_cb = kwargs.get("mcp_cb")
        self._recent_sessions_cb = kwargs.get("recent_sessions_cb")
        self._new_chat_cb = kwargs.get("new_chat_cb")
        self._session_open_cb = kwargs.get("session_open_cb")
        self._brain_model_set_cb = kwargs.get("brain_model_set_cb")
        self._reasoning_set_cb = kwargs.get("reasoning_set_cb")
        self._onboarding_done_cb = kwargs.get("onboarding_done_cb")
        self._fs_list_cb = kwargs.get("fs_list_cb")
        self._fs_native_cb = kwargs.get("fs_native_cb")
        self._workers_get_cb = kwargs.get("workers_get_cb")
        self._worker_set_cb = kwargs.get("worker_set_cb")
        self._catalog_cb = kwargs.get("catalog_cb")
        self._orchestrate_cb = kwargs.get("orchestrate_cb")

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            return json.dumps(
                {
                    "type": "audio",
                    "sampleRate": frame.sample_rate,
                    "data": base64.b64encode(frame.audio).decode("ascii"),
                }
            )
        if isinstance(frame, TranscriptionFrame):
            role = "user" if frame.user_id else "bot"
            return json.dumps({"type": f"{role}_transcript", "text": frame.text})
        if isinstance(frame, LLMTextFrame):
            return json.dumps({"type": "bot_text", "text": frame.text})
        for _cls, _name in ((TTS_TEXT_CLS, "bot_text"),):
            if isinstance(frame, _cls):
                return json.dumps({"type": _name, "text": getattr(frame, "text", "")})
        if isinstance(frame, StatusFrame):
            return json.dumps({"type": "status", "state": frame.state})
        if isinstance(frame, BrainFrame):
            return json.dumps({
                "type": "brain",
                "provider": frame.provider,
                "model": frame.model,
            })
        if isinstance(frame, BrainConfigFrame):
            return json.dumps({
                "type": "brain_config",
                "model": frame.model,
                "reasoning": frame.reasoning,
                "models": frame.models,
                "key_present": frame.key_present,
                "onboarded": frame.onboarded,
            })
        if isinstance(frame, BrainErrorFrame):
            return json.dumps({
                "type": "brain_error",
                "kind": frame.kind,
                "message": frame.message,
                "model": frame.model,
            })
        if isinstance(frame, BrainActivityFrame):
            msg = {"type": "brain_activity", "phase": frame.phase}
            if frame.text:
                msg["text"] = frame.text
            if frame.tool:
                msg["tool"] = frame.tool
            if frame.detail:
                msg["detail"] = frame.detail
            return json.dumps(msg)
        if isinstance(frame, MemoryFrame):
            return json.dumps({
                "type": "memory",
                "entries": frame.entries,
            })
        if isinstance(frame, CodingResultFrame):
            return json.dumps({
                "type": "coding_result",
                "text": frame.text,
            })
        if isinstance(frame, BotImageFrame):
            return json.dumps({
                "type": "bot_image",
                "url": frame.url,
                "caption": frame.caption,
            })
        if isinstance(frame, AgentPartFrame):
            msg: dict = {"type": "agent_part", "kind": frame.kind}
            if frame.kind == "tool":
                msg["tool"] = frame.tool
                msg["status"] = frame.status
                msg["command"] = frame.command
                msg["output"] = frame.output
                if frame.truncated:
                    msg["truncated"] = True
            else:
                msg["text"] = frame.text
            return json.dumps(msg)
        if isinstance(frame, WorkSessionsFrame):
            return json.dumps({
                "type": "work_sessions",
                "list": frame.sessions,
            })
        if isinstance(frame, ProjectsFrame):
            return json.dumps({
                "type": "projects",
                "current": frame.current,
                "list": frame.projects,
            })
        if isinstance(frame, FsListingFrame):
            return json.dumps({
                "type": "fs_listing",
                "path": frame.path,
                "parent": frame.parent,
                "entries": frame.entries,
                "error": frame.error,
            })
        if isinstance(frame, FsNativeResultFrame):
            return json.dumps({
                "type": "fs_native_result",
                "path": frame.path,
                "error": frame.error,
            })
        if isinstance(frame, WorkersFrame):
            return json.dumps({
                "type": "workers",
                "list": frame.workers,
            })
        if isinstance(frame, AgentsFrame):
            return json.dumps({
                "type": "agents",
                "agents": frame.agents,
                "running": frame.running,
                "total": frame.total,
                "tasks": frame.tasks,
            })
        if isinstance(frame, ProjectGateFrame):
            return json.dumps({
                "type": "project_gate",
                "message": frame.message,
            })
        if isinstance(frame, CatalogFrame):
            return json.dumps({
                "type": "catalog",
                "providers": frame.providers,
            })
        if isinstance(frame, RecentSessionsFrame):
            return json.dumps({
                "type": "recent_sessions",
                "list": frame.sessions,
            })
        if isinstance(frame, SessionMessagesFrame):
            return json.dumps({
                "type": "session_messages",
                "messages": frame.messages,
            })
        if isinstance(frame, WorkerActivityFrame):
            return json.dumps({
                "type": "worker_activity",
                "worker_id": frame.worker_id,
                "status": frame.status,
                "activity": frame.activity,
                "title": frame.title,
                "agent": frame.agent,
            })
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", "ignore")
        try:
            msg = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(msg, dict):
            return None
        if msg.get("type") == "debug_ui":
            print(f"[UI-DEBUG] {msg}", flush=True)
            return None
        if msg.get("type") == "audio":
            raw = base64.b64decode(msg.get("data", ""))
            if raw:
                from pipecat.frames.frames import InputAudioRawFrame

                self.__dict__["_af"] = self.__dict__.get("_af", 0) + len(raw) // 2
                n = self.__dict__["_af"]
                global _last_mic_log_time, _mic_chunks_since_log, _mic_samples_since_log
                _mic_chunks_since_log += 1
                _mic_samples_since_log += len(raw) // 2
                now = time.monotonic()
                if now - _last_mic_log_time >= _MIC_LOG_INTERVAL:
                    import statistics
                    vals = struct.unpack(f"<{min(len(raw) // 2, 4096)}h", raw[:8192])
                    energy = int(statistics.fmean(map(abs, vals)))
                    print(
                        f"[MIC] {_mic_chunks_since_log} chunks, "
                        f"{_mic_samples_since_log} samples, "
                        f"energy={energy}, "
                        f"avg {_mic_samples_since_log / max(1, _mic_chunks_since_log):.0f} samples/chunk",
                        flush=True,
                    )
                    _last_mic_log_time = now
                    _mic_chunks_since_log = 0
                    _mic_samples_since_log = 0
                return InputAudioRawFrame(audio=raw, sample_rate=16000, num_channels=1)
        if msg.get("type") == "audio_toggle":
            return AudioToggleFrame(enabled=bool(msg.get("enabled", True)))
        if msg.get("type") == "text":
            from datetime import datetime, timezone

            frame = ImageTextFrame(
                text=msg.get("data", ""),
                user_id="user",
                timestamp=datetime.now(timezone.utc).isoformat(),
                finalized=True,
                images=_parse_chat_images(msg),
                files=_parse_chat_files(msg),
            )
            if self._text_cb:
                await self._text_cb(frame)
                return None
            return frame
        if msg.get("type") == "approve":
            session_id = msg.get("session_id", "")
            if self._approve_cb:
                await self._approve_cb(session_id)
            return None
        if msg.get("type") == "reject":
            session_id = msg.get("session_id", "")
            if self._reject_cb:
                await self._reject_cb(session_id)
            return None
        if msg.get("type") == "stop":
            if self._stop_cb:
                await self._stop_cb()
            return None
        if msg.get("type") == "permission_response":
            permission_id = msg.get("permissionID", "")
            response = msg.get("response", "deny")
            remember = msg.get("remember", False)
            # worker panel permission response (worker_id + decision)
            worker_id = msg.get("worker_id", "")
            decision = msg.get("decision", "")
            if worker_id and self._worker_permission_cb:
                await self._worker_permission_cb(worker_id, decision)
            elif self._permission_cb:
                await self._permission_cb(permission_id, response, remember)
            return None
        if msg.get("type") == "memory":
            action = msg.get("action", "")
            if action in ("edit", "delete") and self._memory_cb:
                await self._memory_cb(action, msg)
            return None
        if msg.get("type") == "work_mode":
            enabled = bool(msg.get("enabled", False))
            if self._work_mode_cb:
                await self._work_mode_cb(enabled)
            return None
        if msg.get("type") == "projects_get":
            if self._projects_cb:
                await self._projects_cb()
            return None
        if msg.get("type") == "project_set":
            path = msg.get("path", "")
            if self._project_set_cb:
                await self._project_set_cb(path)
            return None
        if msg.get("type") == "fs_list":
            if self._fs_list_cb:
                await self._fs_list_cb(msg)
            return None
        if msg.get("type") == "fs_pick_native":
            if self._fs_native_cb:
                await self._fs_native_cb()
            return None
        if msg.get("type") == "workers_get":
            if self._workers_get_cb:
                await self._workers_get_cb()
            return None
        if msg.get("type") == "worker_set":
            if self._worker_set_cb:
                await self._worker_set_cb(msg)
            return None
        if msg.get("type") == "catalog_get":
            if self._catalog_cb:
                await self._catalog_cb()
            return None
        if msg.get("type") == "orchestrate":
            if self._orchestrate_cb:
                await self._orchestrate_cb(msg)
            return None
        if msg.get("type") == "brain_provider":
            provider = msg.get("provider", "")
            if self._brain_provider_cb:
                await self._brain_provider_cb(provider)
            return None
        if msg.get("type") == "get_brain_usage":
            if self._brain_provider_cb:
                await self._brain_provider_cb("__usage__")
            return None
        if msg.get("type") in ("provider_config_get", "provider_config_set"):
            if self._provider_config_cb:
                await self._provider_config_cb(msg)
            return None
        if msg.get("type") == "provider_key_set":
            if self._provider_key_cb:
                await self._provider_key_cb(msg)
            return None
        if msg.get("type") == "provider_custom":
            if self._provider_custom_cb:
                await self._provider_custom_cb(msg)
            return None
        if msg.get("type") == "mcp":
            if self._mcp_cb:
                await self._mcp_cb(msg)
            return None
        if msg.get("type") == "recent_sessions_get":
            if self._recent_sessions_cb:
                await self._recent_sessions_cb()
            return None
        if msg.get("type") == "new_chat":
            if self._new_chat_cb:
                await self._new_chat_cb()
            return None
        if msg.get("type") == "session_open":
            if self._session_open_cb:
                await self._session_open_cb(msg.get("session_id", ""))
            return None
        if msg.get("type") == "brain_model_set":
            if self._brain_model_set_cb:
                await self._brain_model_set_cb(msg)
            return None
        if msg.get("type") == "reasoning_set":
            if self._reasoning_set_cb:
                await self._reasoning_set_cb(msg)
            return None
        if msg.get("type") == "onboarding_done":
            if self._onboarding_done_cb:
                await self._onboarding_done_cb()
            return None
        return None
