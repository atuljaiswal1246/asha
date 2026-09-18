"""Skill store (E1-E3): Hermes-style reusable skills.

Each skill is a directory ``<root>/<name>/SKILL.md`` with a small frontmatter
block::

    ---
    name: add-endpoint
    description: Add a FastAPI endpoint plus its test
    created_by: asha
    uses: 0
    ---

    <markdown body — the how-to>

The short ``description`` (budgeted to 60 chars) is what gets injected into the
system prompt as an index; the full body is fetched on demand. A slow curator
archives (never deletes) skills that have gone unused.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

MAX_DESCRIPTION = 60
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _parse(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER.match(text or "")
    if not m:
        return {}, (text or "")
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    return meta, text[m.end():]


def _render(meta: dict, body: str) -> str:
    lines = ["---"]
    for k in ("name", "description", "created_by", "uses", "saved",
              "last_used", "pinned"):
        if k in meta:
            lines.append(f"{k}: {meta[k]}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + body.strip() + "\n"


def _is_pinned(meta: dict) -> bool:
    return str(meta.get("pinned", "")).strip().lower() in ("true", "yes", "1")


def _days_since(date_str: str, now: float) -> float:
    try:
        return (now - time.mktime(time.strptime(date_str, "%Y-%m-%d"))) / 86400
    except Exception:
        return 0.0


class SkillStore:
    """Filesystem-backed skill library with an index, curator, and nudges."""

    def __init__(self, root: str | Path):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    # ── read ─────────────────────────────────────────────────────────────────
    def _dir(self, name: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", (name or "").strip().lower())
        return self._root / safe

    def list(self) -> list[dict]:
        out: list[dict] = []
        for d in sorted(self._root.iterdir()) if self._root.exists() else []:
            if not d.is_dir() or d.name.startswith("_"):
                continue
            f = d / "SKILL.md"
            if not f.is_file():
                continue
            meta, body = _parse(f.read_text(encoding="utf-8", errors="replace"))
            out.append({
                "name": meta.get("name") or d.name,
                "description": meta.get("description", "")[:MAX_DESCRIPTION],
                "created_by": meta.get("created_by", ""),
                "uses": int(meta.get("uses", "0") or 0),
                "last_used": meta.get("last_used", ""),
                "pinned": _is_pinned(meta),
                "body_chars": len(body),
            })
        return out

    def get(self, name: str) -> str | None:
        f = self._dir(name) / "SKILL.md"
        if not f.is_file():
            return None
        meta, body = _parse(f.read_text(encoding="utf-8", errors="replace"))
        try:
            meta["uses"] = str(int(meta.get("uses", "0") or 0) + 1)
            meta["last_used"] = time.strftime("%Y-%m-%d")
            f.write_text(_render(meta, body), encoding="utf-8")
        except Exception:
            pass
        return f.read_text(encoding="utf-8")

    # ── write ────────────────────────────────────────────────────────────────
    def save(self, name: str, description: str, body: str,
             created_by: str = "asha", pinned: bool = False) -> str:
        name = (name or "").strip()
        if not name:
            raise ValueError("skill name is required")
        if not (body or "").strip():
            raise ValueError("skill body is required")
        desc = " ".join((description or "").split())[:MAX_DESCRIPTION]
        meta = {"name": name, "description": desc, "created_by": created_by,
                "uses": "0", "saved": time.strftime("%Y-%m-%d")}
        if pinned:
            meta["pinned"] = "true"
        d = self._dir(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(_render(meta, body), encoding="utf-8")
        return str(d / "SKILL.md")

    def pin(self, name: str, pinned: bool = True) -> bool:
        """Pin/unpin a skill so the curator never archives it."""
        f = self._dir(name) / "SKILL.md"
        if not f.is_file():
            return False
        meta, body = _parse(f.read_text(encoding="utf-8", errors="replace"))
        if pinned:
            meta["pinned"] = "true"
        else:
            meta.pop("pinned", None)
        f.write_text(_render(meta, body), encoding="utf-8")
        return True

    def archive(self, name: str) -> bool:
        """Move a skill to ``_archive/`` (never delete)."""
        d = self._dir(name)
        if not d.is_dir():
            return False
        dest = self._root / "_archive" / d.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.with_name(dest.name + f"-{int(time.time())}")
        d.rename(dest)
        return True

    # ── prompt + curation ────────────────────────────────────────────────────
    def index(self, max_chars: int = 1200) -> str:
        skills = self.list()
        if not skills:
            return ""
        lines = ["SKILLS you have learned (call skill_get for the full steps):"]
        for s in skills:
            line = f"- {s['name']}: {s['description']}"
            if sum(len(x) + 1 for x in lines) + len(line) > max_chars:
                break
            lines.append(line)
        return "\n".join(lines)

    def curator(self, unused_days: int = 30, archive_days: int = 90) -> list[str]:
        """Archive (never delete) unpinned skills that are stale: either never
        used and older than ``unused_days``, or last used more than
        ``archive_days`` ago. Pinned skills are always kept."""
        now = time.time()
        archived: list[str] = []
        for d in list(self._root.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            f = d / "SKILL.md"
            if not f.is_file():
                continue
            meta, _ = _parse(f.read_text(encoding="utf-8", errors="replace"))
            if _is_pinned(meta):
                continue
            uses = int(meta.get("uses", "0") or 0)
            if uses == 0:
                age_days = (now - f.stat().st_mtime) / 86400
                stale = age_days >= unused_days
            else:
                last = meta.get("last_used") or meta.get("saved") or ""
                stale = _days_since(last, now) >= archive_days
            if stale and self.archive(meta.get("name") or d.name):
                archived.append(d.name)
        return archived


class MemoryNudger:
    """Turn/iteration counter that says when to review for a save (E3)."""

    def __init__(self, every: int = 20):
        self._every = max(1, every)
        self._count = 0

    def tick(self) -> bool:
        self._count += 1
        return self._count % self._every == 0

    @property
    def count(self) -> int:
        return self._count
