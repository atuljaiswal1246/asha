"""M2a Memory Loop — Hermes-derived persistent memory for Jarvis.

Design: notes/memory-design.md section 2.1.
Threat scan pattern adapted from Hermes tools/memory_tool.py (MIT, NousResearch).
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from openai import AsyncOpenAI
from pipecat.frames.frames import LLMFullResponseEndFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor

logger = logging.getLogger("asha.memory")

try:
    import memory_similar
except Exception:  # pragma: no cover - defensive: never block memory saves
    memory_similar = None

try:
    import memory_meta
except Exception:  # pragma: no cover - defensive: sidecar is optional
    memory_meta = None

ENTRY_DELIMITER = "\n§\n"

# Soft cap: at/above this fraction of the char budget the store prefers
# replacing/merging a near-duplicate entry over appending a new one, so the
# store stays bounded. The hard char_limit still applies above it.
DEFAULT_CAP_RATIO = 0.9

# Only entries at least this lexically similar to the new fact are eligible
# for a cap-time merge (near-duplicates already clear the higher 0.75 bar).
MERGE_SIMILARITY_THRESHOLD = 0.5


def _near_duplicate(a: str, b: str, threshold: float = 0.75) -> bool:
    """Variant-nearest dedupe: token-set overlap catches reordered/rephrased
    variants that exact-match dedupe misses (e.g. "My dog's name is Bruno"
    vs "Dog's name is Bruno"). Deterministic, stdlib only."""
    if not a or not b:
        return False
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return False
    return len(ta & tb) / max(len(ta), len(tb)) >= threshold


# Deterministic backstop to the reviewer prompt: a durable fact is one worth
# keeping a month from now. One-off requests ("check my desktop for X"),
# actions Jarvis performed, and transient questions are not.
_REQUEST_START_RE = re.compile(
    r"^\s*(?:please\s+)?(?:check|find|search|look\s+for|look\s+up|show|tell|"
    r"give|open|create|make|delete|remove|add|set|send|run|download|install|"
    r"play|pause|stop|start|book|schedule|remind|call|text|email|write|draft|"
    r"summari[sz]e|translate|convert|fix|update|change|move|copy|rename|list|"
    r"count|calculate|compute|read|scan|verify|confirm)\b",
    re.IGNORECASE,
)
_ACTION_MENTION_RE = re.compile(
    r"\b(?:asked|told|requested|instructed)\s+(?:jarvis|you|the\s+assistant)\b"
    r"|\b(?:jarvis|you)\s+(?:checked|searched|found|opened|created|looked|ran|"
    r"downloaded|installed|scheduled|sent|set|made|deleted|removed|added|wrote|"
    r"drafted|updated|changed|moved|copied|renamed|listed|counted|fixed|"
    r"started|stopped|played|paused|called|texted|emailed|verified|confirmed)\b",
    re.IGNORECASE,
)
_REQUEST_PHRASE_RE = re.compile(r"^\s*(?:can|could|would|will)\s+you\b", re.IGNORECASE)


def is_durable_fact(text: str) -> bool:
    """True when ``text`` reads as a durable user fact, not a one-off request.

    Primary extraction filtering lives in the reviewer prompt; this is the
    deterministic backstop. Returns False for request-shaped statements
    ("check my desktop for X"), actions Jarvis performed, transient questions
    (trailing "?") and explicit request phrasing. Conservative by design: only
    clearly transient phrasings are rejected.
    """
    text = (text or "").strip()
    if not text:
        return False
    if text.endswith("?"):
        return False
    if _REQUEST_START_RE.search(text):
        return False
    if _ACTION_MENTION_RE.search(text):
        return False
    if _REQUEST_PHRASE_RE.search(text):
        return False
    return True


def _merge_entry_text(old: str, new: str) -> str:
    """Fold ``new`` into ``old`` without duplicating a covered fact."""
    old_lower, new_lower = old.lower(), new.lower()
    if new_lower in old_lower:
        return old
    if old_lower in new_lower:
        return new
    return f"{old}; {new}"


# ---------------------------------------------------------------------------
# Threat Scanner
# ---------------------------------------------------------------------------

class ThreatScanner:
    """Regex prompt-injection detector (Hermes strict-scope pattern, MIT)."""

    PATTERNS = [
        (r"ignore\s+(all\s+)?previous\s+(instructions?|prompts?|rules?)", "prompt override"),
        (r"you\s+are\s+now\s+", "role reassignment"),
        (r"(new|override|replacement)\s+system\s+(prompt|instructions?)", "system injection"),
        (r"act\s+as\s+if\s+you\s+(are|were)\s+", "role manipulation"),
        (r"pretend\s+(to\s+be|you\s+are)\s+", "role manipulation"),
        (r"disregard\s+(all\s+)?(previous|earlier|above)", "instruction override"),
        (r"forget\s+(everything|all|what)\s+(you|was|were)\s+(told|instructed|know)", "memory wipe"),
        (r"(do\s+not|don'?t)\s+tell\s+(anyone|the\s+user|anybody)", "secrecy injection"),
        (r"keep\s+this\s+(secret|between\s+us)", "secrecy injection"),
        (r"<\|im_start\|>|<\|im_end\|>|<\|system\|>", "template injection"),
        (r"\[system\]|\[INST\]|\[/INST\]", "template marker injection"),
    ]

    def scan(self, text: str) -> list[str]:
        threats = []
        lower = text.lower()
        for pattern, desc in self.PATTERNS:
            if re.search(pattern, lower):
                threats.append(desc)
        return threats


# ---------------------------------------------------------------------------
# Memory Store
# ---------------------------------------------------------------------------

class MemoryStore:
    """Section-entry memory store with char budgets, dedupe, threat scan, atomic writes."""

    def __init__(
        self,
        path: Path,
        char_limit: int,
        label: str = "memory",
        cap_ratio: float = DEFAULT_CAP_RATIO,
        merge_threshold: float = MERGE_SIMILARITY_THRESHOLD,
    ):
        self._path = path
        self._char_limit = char_limit
        self._label = label
        self._cap_ratio = cap_ratio
        self._merge_threshold = merge_threshold
        self._lock = asyncio.Lock()
        self._scanner = ThreatScanner()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)
        try:
            if memory_meta is not None:
                self._meta = memory_meta.MemoryMetaStore(
                    self._path.parent / "memory_meta.json"
                )
            else:
                self._meta = None
        except Exception:
            self._meta = None
            logger.warning(
                "[MEM] memory_meta sidecar unavailable; decay disabled (fail-open).",
                exc_info=True,
            )

    @property
    def char_limit(self) -> int:
        return self._char_limit

    def _meta_stamp(self, text: str, first_seen: Optional[float] = None) -> None:
        """Record first/last seen for ``text``. Fail-open: never raises."""
        try:
            if self._meta is not None:
                self._meta.stamp(memory_meta.entry_key(text), first_seen=first_seen)
        except Exception:
            logger.warning("[MEM] Meta stamp failed (fail-open).", exc_info=True)

    def _meta_forget(self, text: str) -> None:
        """Drop ``text``'s timing record. Fail-open: never raises."""
        try:
            if self._meta is not None:
                self._meta.forget(memory_meta.entry_key(text))
        except Exception:
            logger.warning("[MEM] Meta forget failed (fail-open).", exc_info=True)

    def _meta_decay(self, text: str) -> float:
        """Decay score for ``text``; 1.0 (fresh) on any failure/unknown."""
        try:
            if self._meta is not None:
                return self._meta.decay_score(memory_meta.entry_key(text))
        except Exception:
            logger.warning("[MEM] Meta decay lookup failed (fail-open).", exc_info=True)
        return 1.0

    def _ordered_entries(self, entries: list[str]) -> list[str]:
        """Entries freshest-first by decay score (stable; content unchanged).

        Unknown/unscored entries rank as fresh (1.0) and any failure returns
        the input order untouched.
        """
        try:
            return sorted(entries, key=self._meta_decay, reverse=True)
        except Exception:
            logger.warning("[MEM] Meta ordering failed (fail-open).", exc_info=True)
            return entries

    def mark_recalled(self, entries) -> None:
        """Touch last-seen for the given entry texts (selective-recall seam).

        Deliberately NOT called from ``snapshot``: a blanket touch would erase
        the staleness signal this sidecar exists to provide. Fail-open.
        """
        try:
            if self._meta is not None:
                for text in entries or []:
                    self._meta.touch(memory_meta.entry_key(text))
        except Exception:
            logger.warning("[MEM] Meta recall touch failed (fail-open).", exc_info=True)

    def _load_entries(self) -> list[str]:
        try:
            text = self._path.read_text(encoding="utf-8").strip()
        except Exception:
            return []
        if not text:
            return []
        return [e.strip() for e in text.split(ENTRY_DELIMITER) if e.strip()]

    def _entries_char_count(self, entries: list[str]) -> int:
        if not entries:
            return 0
        return sum(len(e) for e in entries) + len(ENTRY_DELIMITER) * (len(entries) - 1)

    def _save_entries(self, entries: list[str]):
        text = ENTRY_DELIMITER.join(entries)
        tmp = self._path.with_suffix(".tmp")
        lock = self._path.with_suffix(".lock")
        try:
            lock.write_text(str(os.getpid()), encoding="utf-8")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._path)
        finally:
            lock.unlink(missing_ok=True)

    def current_usage(self) -> tuple[str, int, int]:
        """Returns (header_string, chars_used, char_limit)."""
        entries = self._load_entries()
        used = self._entries_char_count(entries)
        pct = int(100 * used / self._char_limit) if self._char_limit else 0
        header = f"[{self._label.upper()} {pct}% — {used}/{self._char_limit} chars]"
        return header, used, self._char_limit

    def snapshot(self) -> str:
        """Frozen snapshot for system prompt injection (called once per session).

        Entries are emitted freshest-first (decay DESC) so stale memories are
        deprioritised in recall. Content is identical — only order changes.
        """
        entries = self._ordered_entries(self._load_entries())
        if not entries:
            return ""
        used = self._entries_char_count(entries)
        pct = int(100 * used / self._char_limit) if self._char_limit else 0
        sep = "═" * 46
        header = f"{sep}\n{self._label.upper()} (your personal notes) [{pct}% — {used}/{self._char_limit} chars]\n{sep}"
        return header + "\n" + ENTRY_DELIMITER.join(entries)

    def entries_text(self) -> str:
        """Current entries as delimited text (for reviewer prompt)."""
        entries = self._load_entries()
        return ENTRY_DELIMITER.join(entries) if entries else "(empty)"

    def entry_list(self) -> list[str]:
        """Current entries as a read-only list (disk order preserved)."""
        return self._load_entries()

    def _at_cap(self, used: int) -> bool:
        """True when the store has reached the documented soft cap."""
        if not self._char_limit or self._cap_ratio <= 0:
            return False
        return used >= self._cap_ratio * self._char_limit

    def _cap_merge(
        self,
        entries: list[str],
        content: str,
        near: Optional[tuple[int, float]],
        best_index: int,
        best_score: float,
        used: int,
    ) -> Optional[tuple[int, str]]:
        """Pick an existing entry to fold ``content`` into at the cap.

        Reuses the similarity scan already run by ``add`` (no second
        mechanism). Returns ``(index, merged_text)`` or ``None`` when there is
        no eligible entry or the merge would exceed the hard char limit.
        """
        if not entries:
            return None
        index: Optional[int] = near[0] if near is not None else None
        if index is None and best_index >= 0 and best_score >= self._merge_threshold:
            index = best_index
        if index is None:
            return None
        merged_text = _merge_entry_text(entries[index], content)
        if not merged_text:
            return None
        candidate_used = used - len(entries[index]) + len(merged_text)
        if self._char_limit and candidate_used > self._char_limit:
            if len(content) <= len(entries[index]):
                merged_text = content
            else:
                return None
        return index, merged_text

    async def add(self, content: str) -> str:
        async with self._lock:
            content = content.strip()
            if not content:
                return "Empty content, nothing to add."

            threats = self._scanner.scan(content)
            if threats:
                return f"Blocked: threat detected ({', '.join(threats)})."

            entries = self._load_entries()
            used = self._entries_char_count(entries)
            at_cap = self._at_cap(used)

            content_lower = content.lower()
            if any(content_lower == e.lower() for e in entries):
                return "Duplicate entry, skipping."

            # One similarity scan serves both near-duplicate blocking and the
            # cap-time merge. Fail-open: a similarity error must NEVER block a
            # memory save.
            near: Optional[tuple[int, float]] = None
            best_index = -1
            best_score = 0.0
            try:
                if memory_similar is not None:
                    near = memory_similar.near_duplicate(
                        content_lower,
                        entries,
                        threshold=memory_similar.DEFAULT_THRESHOLD,
                    )
                    for i, e in enumerate(entries):
                        score = memory_similar.similar(content_lower, e.lower())
                        if score > best_score:
                            best_score, best_index = score, i
                    if near is None and best_index >= 0 and best_score >= 0.65:
                        logger.info(
                            "Saved entry despite closeness to an existing one "
                            "(best score %.2f, below threshold %.2f).",
                            best_score,
                            memory_similar.DEFAULT_THRESHOLD,
                        )
                else:
                    for i, e in enumerate(entries):
                        if _near_duplicate(content_lower, e.lower()):
                            near = (i, 1.0)
                            break
            except Exception:
                logger.warning(
                    "Similarity check failed; saving memory anyway (fail-open).",
                    exc_info=True,
                )

            if at_cap:
                merged = self._cap_merge(
                    entries, content, near, best_index, best_score, used
                )
                if merged is not None:
                    index, merged_text = merged
                    entries[index] = merged_text
                    self._save_entries(entries)
                    self._meta_stamp(merged_text)
                    return (
                        f"Merged into entry #{index + 1} at cap "
                        f"({int(self._cap_ratio * 100)}%): replaced instead of "
                        f"appending. ({len(entries)} entries, "
                        f"{self._entries_char_count(entries)}/{self._char_limit} chars)"
                    )

            if near is not None:
                index, score = near
                return (
                    f"Near-duplicate of entry #{index} (score {score:.2f}), "
                    "skipping. Use 'replace' with old_text to update it instead."
                )

            new_used = used + len(content) + (len(ENTRY_DELIMITER) if entries else 0)
            if new_used > self._char_limit:
                return (
                    f"Memory at {used}/{self._char_limit} chars. "
                    f"Adding this entry ({len(content)} chars) would exceed the limit. "
                    f"Consolidate now: use 'replace' to merge overlapping entries or "
                    f"'remove' stale entries, then retry this add."
                )

            entries.append(content)
            self._save_entries(entries)
            self._meta_stamp(content)
            return f"Added. ({len(entries)} entries, {new_used}/{self._char_limit} chars)"

    async def replace(self, old_text: str, new_text: str) -> str:
        async with self._lock:
            new_text = new_text.strip()
            if not new_text:
                return "Empty replacement, nothing to do."

            entries = self._load_entries()
            old_lower = old_text.lower()
            matches = [i for i, e in enumerate(entries) if old_lower in e.lower()]

            if len(matches) == 0:
                return f"No entry contains '{old_text[:50]}'. Be more specific."
            if len(matches) > 1:
                return f"Multiple entries match '{old_text[:50]}'. Be more specific."

            threats = self._scanner.scan(new_text)
            if threats:
                return f"Blocked: threat detected in replacement ({', '.join(threats)})."

            old_entry = entries[matches[0]]
            old_first_seen: Optional[float] = None
            try:
                if self._meta is not None:
                    old_first_seen = self._meta.first_seen(
                        memory_meta.entry_key(old_entry)
                    )
            except Exception:
                logger.warning(
                    "[MEM] Meta first_seen read failed (fail-open).", exc_info=True
                )

            entries[matches[0]] = new_text
            self._save_entries(entries)
            self._meta_stamp(new_text, first_seen=old_first_seen)
            if old_entry != new_text:
                self._meta_forget(old_entry)
            return f"Replaced. ({len(entries)} entries)"

    async def remove(self, old_text: str) -> str:
        async with self._lock:
            entries = self._load_entries()
            old_lower = old_text.lower()
            matches = [i for i, e in enumerate(entries) if old_lower in e.lower()]

            if len(matches) == 0:
                return f"No entry contains '{old_text[:50]}'. Be more specific."
            if len(matches) > 1:
                return f"Multiple entries match '{old_text[:50]}'. Be more specific."

            old_entry = entries.pop(matches[0])
            self._save_entries(entries)
            self._meta_forget(old_entry)
            return f"Removed. ({len(entries)} entries)"


def suggest_stale_entries(
    store: MemoryStore,
    max_age_days: float = 90.0,
    max_decay: float = 0.5,
    limit: int = 20,
) -> list[dict]:
    """Read-only cleanup candidates for the user to review.

    Returns stalest-first entries whose decay score has fallen to
    ``max_decay`` or whose recorded age exceeds ``max_age_days``. This NEVER
    mutates the store, the sidecar, or the memory file and is never called
    automatically — deletion stays a user action.
    """
    now = time.time()
    candidates = []
    for text in store.entry_list():
        score = store._meta_decay(text)
        age_days: Optional[float] = None
        try:
            if store._meta is not None:
                age_days = store._meta.age_days(memory_meta.entry_key(text), now=now)
        except Exception:
            age_days = None
        stale = score < max_decay or (
            age_days is not None and age_days > max_age_days
        )
        if stale:
            candidates.append(
                {"text": text, "decay_score": score, "age_days": age_days}
            )
    candidates.sort(key=lambda c: (c["decay_score"], -(c["age_days"] or 0.0)))
    return candidates[:limit]


# ---------------------------------------------------------------------------
# Memory Manager
# ---------------------------------------------------------------------------

class MemoryManager:
    """Thin manager: builds frozen session snapshot from disk-backed stores."""

    def __init__(self, data_dir: Path, memory_char_limit: int, user_char_limit: int):
        self._memory = MemoryStore(data_dir / "MEMORY.md", memory_char_limit, label="memory")
        self._user = MemoryStore(data_dir / "USER.md", user_char_limit, label="user")
        self._base_system_prompt: str = ""
        self._snapshot: str = ""

    def initialize(self, base_system_prompt: str):
        """Call once at WS connect. Builds frozen snapshot from disk."""
        self._base_system_prompt = base_system_prompt
        mem_snap = self._memory.snapshot()
        user_snap = self._user.snapshot()
        blocks = [b for b in (mem_snap, user_snap) if b]
        self._snapshot = base_system_prompt
        if blocks:
            self._snapshot = base_system_prompt + "\n\n" + "\n\n".join(blocks)
        logger.info(
            f"[MEM] Snapshot built: {len(self._snapshot)} chars "
            f"(base={len(base_system_prompt)}, mem={len(mem_snap)}, user={len(user_snap)})"
        )

    def system_instruction_suffix(self) -> str:
        """Memory/user blocks ONLY (without base prompt).

        Appended via pipecat's ``append_system_instruction`` so the request
        has exactly ONE system message (llama.cpp Qwen3.5 template rejects two).
        """
        mem_snap = self._memory.snapshot()
        user_snap = self._user.snapshot()
        blocks = [b for b in (mem_snap, user_snap) if b]
        return "\n\n".join(blocks)

    @property
    def frozen_snapshot(self) -> str:
        return self._snapshot

    @property
    def memory_store(self) -> MemoryStore:
        return self._memory

    @property
    def user_store(self) -> MemoryStore:
        return self._user

    def build_system_prompt(self) -> str:
        return self._snapshot


# ---------------------------------------------------------------------------
# Memory Reviewer
# ---------------------------------------------------------------------------

class MemoryReviewer:
    """Background idle-gated review: extracts facts from conversation via LLM."""

    def __init__(
        self,
        llm_base_url: str,
        llm_model: str,
        context,
        memory_manager: MemoryManager,
        review_interval: int = 10,
        idle_seconds: float = 60.0,
        journal_path: Optional[Path] = None,
        llm_api_key: str = "local",
        extra_headers: Optional[dict] = None,
    ):
        self._client = AsyncOpenAI(
            base_url=llm_base_url,
            api_key=llm_api_key or "local",
            default_headers=extra_headers,
        )
        self._model = llm_model or "gpt-4.1"
        self._llm_base_url = llm_base_url
        logger.info(
            f"[MEM] Reviewer brain: {llm_base_url} model={self._model}"
        )
        self._context = context
        self._manager = memory_manager
        self._review_interval = review_interval
        self._idle_seconds = idle_seconds
        self._journal_path = journal_path or Path("/dev/null")

        self._turn_count = 0
        self._last_activity = time.monotonic()
        self._review_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None

        self._journal_path.parent.mkdir(parents=True, exist_ok=True)

    def on_user_speaking(self):
        self._last_activity = time.monotonic()
        if self._review_task and not self._review_task.done():
            self._review_task.cancel()
            logger.info("[MEM] Review cancelled (user started speaking)")

    def on_turn_completed(self):
        self._turn_count += 1
        self._last_activity = time.monotonic()
        if self._turn_count >= self._review_interval:
            if self._review_task and not self._review_task.done():
                return
            logger.info(f"[MEM] Turn threshold ({self._turn_count}), scheduling review")
            self._turn_count = 0
            self._review_task = asyncio.create_task(self._run_review())

    def start_idle_watchdog(self):
        if self._watchdog_task and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.create_task(self._idle_watchdog())

    async def _idle_watchdog(self):
        while True:
            await asyncio.sleep(10)
            idle = time.monotonic() - self._last_activity
            if idle >= self._idle_seconds and self._turn_count > 0:
                if self._review_task and not self._review_task.done():
                    continue
                logger.info(f"[MEM] Idle threshold ({idle:.0f}s), scheduling review")
                self._turn_count = 0
                self._review_task = asyncio.create_task(self._run_review())

    def cancel(self):
        if self._review_task and not self._review_task.done():
            self._review_task.cancel()
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()

    async def _run_review(self):
        try:
            memory = self._manager.memory_store
            user = self._manager.user_store

            digest = self._extract_digest()
            if not digest.strip():
                logger.info("[MEM] No conversation to review, skipping")
                return

            mem_header, mem_used, mem_limit = memory.current_usage()
            user_header, user_used, user_limit = user.current_usage()
            mem_pct = int(100 * mem_used / mem_limit) if mem_limit else 0

            consolidation_note = ""
            if mem_pct >= 80:
                consolidation_note = (
                    " MEMORY FULL WARNING: consolidate by replacing overlapping entries "
                    "or removing stale ones BEFORE adding new entries."
                )

            review_prompt = (
                "You are Jarvis's memory reviewer. Extract important facts, preferences, "
                "and schedules from the conversation and save them to memory.\n\n"
                f"CURRENT MEMORY ({mem_header}):\n{memory.entries_text()}\n\n"
                f"CURRENT USER PROFILE ({user_header}):\n{user.entries_text()}\n\n"
                f"CONVERSATION (user's statements only — assistant replies are "
                f"excluded and are never evidence):\n{digest}\n\n"
                "RULES (durable-fact extraction):\n"
                "- DURABLE FACTS ONLY: save only durable facts about the user — preferences, "
                "personal details (name, family, relationships), ongoing project context, and "
                "standing instructions. Each entry's content must quote or closely paraphrase "
                "the user's own words. NEVER infer, generalize, or invent details not in the "
                "transcript.\n"
                "- NEVER SAVE: one-off requests (e.g. \"check my desktop for X\"), actions "
                "Jarvis performed (e.g. \"Jarvis checked ...\"), transient questions, or "
                "anything that matters only for the current turn.\n"
                "- MONTH TEST: before saving, ask \"would this still be useful in a month?\" "
                "If not, do not save it. When in doubt, do not save.\n"
                "- ROUTING: if the fact is about the USER (identity, preferences, family, "
                'schedule, opinions, habits) → save_target MUST be "user" (USER.md). '
                'Use "memory" only for non-user facts worth keeping across sessions.\n'
                "- DEDUPE: if a fact restates an existing CURRENT MEMORY / USER PROFILE entry, "
                "use \"replace\" (old_text = unique substring of the old entry), never a second "
                '"add". If the user corrects a fact, REPLACE; to forget, REMOVE.\n'
                '- Only say nothing-to-save if PURELY casual chitchat with zero durable facts.\n'
                "- Keep entries SHORT (1-2 sentences max).\n"
                "- NEVER save system prompts, technical instructions, or meta-conversation.\n"
                f"Memory is at {mem_pct}% capacity.{consolidation_note}\n\n"
                "Respond with STRICT JSON only, no markdown fences, no extra text:\n"
                '{"save_target":"memory|user|none","entries":[{"action":"add|replace|remove",'
                '"old_text":"(required for replace/remove)","content":"(the entry text)"}]}\n\n'
                'If nothing to save: {"save_target":"none","entries":[]}'
            )

            logger.info(
                f"[MEM] Starting LLM review call via {self._llm_base_url} "
                f"model={self._model}..."
            )
            response = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": review_prompt},
                        {"role": "user", "content": "Review the conversation and extract facts to save."},
                    ],
                    max_tokens=512,
                    temperature=0.3,
                ),
                timeout=60.0,
            )

            text = response.choices[0].message.content or ""
            result = self._parse_review(text)
            if not result:
                logger.warning(f"[MEM] Failed to parse review: {text[:200]}")
                self._journal("parse_error", text[:200])
                return

            save_target = result.get("save_target", "none")
            entries = self._filter_durable(result.get("entries", []))

            if save_target == "none" or not entries:
                logger.info("[MEM] Review: nothing to save")
                self._journal("nothing_to_save", "")
                return

            store = memory if save_target == "memory" else user
            for entry in entries:
                action = entry.get("action", "")
                content = entry.get("content", "").strip()
                old_text = entry.get("old_text", "").strip()

                if not content and action == "add":
                    continue

                if action == "add":
                    r = await store.add(content)
                elif action == "replace":
                    r = await store.replace(old_text, content)
                elif action == "remove":
                    r = await store.remove(old_text)
                else:
                    r = f"Unknown action: {action}"

                self._journal(action, content, save_target, r)
                logger.info(f"[MEM] {action} -> {save_target}: {r}")

        except asyncio.CancelledError:
            logger.info("[MEM] Review cancelled")
        except Exception as e:
            logger.error(f"[MEM] Review failed: {e}", exc_info=True)

    def _filter_durable(self, entries: list) -> list:
        """Drop 'add' entries that are not durable facts (deterministic backstop).

        ``replace``/``remove`` pass through untouched so consolidation still
        works; only new saves are gated by ``is_durable_fact``.
        """
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            action = entry.get("action", "")
            content = (entry.get("content") or "").strip()
            if action == "add" and not is_durable_fact(content):
                self._journal("skipped_transient", content, "", "not a durable fact")
                logger.info("[MEM] Skipped non-durable add: %s", content[:80])
                continue
            kept.append(entry)
        return kept

    def _extract_digest(self) -> str:
        # KB-09: reviewer sees ONLY user turns. Assistant replies are not
        # evidence of user facts (its filler once became a hallucinated
        # "fact"), so they are excluded from the digest entirely.
        try:
            messages = list(self._context.messages)
        except Exception:
            return ""
        lines = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and content:
                lines.append(f"USER: {content}")
        return "\n".join(lines)

    def _parse_review(self, text: str) -> Optional[dict]:
        if not text:
            return None
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass
        return None

    def _journal(self, action: str, content: str, target: str = "", result: str = ""):
        try:
            entry = {
                "ts": time.time(),
                "action": action,
                "target": target,
                "content": content[:500],
                "result": result[:200],
            }
            with open(self._journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning(f"[MEM] Journal write failed: {e}")


# ---------------------------------------------------------------------------
# Turn Signal (pipecat FrameProcessor)
# ---------------------------------------------------------------------------

class TurnSignal(FrameProcessor):
    """Signals the memory reviewer on user speech and bot response completion.

    LLMTextFrame never flows past TTSService (it is consumed for synthesis), so
    turn completion is signaled with LLMFullResponseEndFrame, which TTSService
    re-emits (queue-drained, once per LLM response) when push_text_frames=True.
    """

    def __init__(self, reviewer: Optional[MemoryReviewer] = None):
        super().__init__()
        self._reviewer = reviewer

    async def process_frame(self, frame, direction):
        if self._reviewer:
            if isinstance(frame, TranscriptionFrame) and frame.user_id in ("", "user"):
                self._reviewer.on_user_speaking()
            elif isinstance(frame, LLMFullResponseEndFrame):
                self._reviewer.on_turn_completed()
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
