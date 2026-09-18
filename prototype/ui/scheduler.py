"""Cron/automation scheduler (G2).

Jobs live in ``cron.json``; each runs either on a 5-field cron expression
(``minute hour dom month dow``) or on a fixed interval (``every`` seconds).
The pure scheduling math is testable without threads; ``run_due`` takes a
dispatch callback so the server can wire it to the worker/orchestrator path.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("dom", 1, 31),
    ("month", 1, 12),
    ("dow", 0, 6),
)


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    """Parse one cron field: ``*``, ``a``, ``a-b``, ``*/n``, ``a-b/n``, lists."""
    values: set[int] = set()
    for part in (field or "*").split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = max(1, int(s))
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        values.update(range(start, end + 1, step))
    return values


def cron_matches(expr: str, dt: datetime) -> bool:
    fields = (expr or "").split()
    if len(fields) != 5:
        return False
    vals = [
        getattr(dt, "minute"), getattr(dt, "hour"), dt.day, dt.month,
        (dt.weekday() + 1) % 7,  # cron dow: 0=Sunday
    ]
    try:
        for (name, lo, hi), field, v in zip(_FIELDS, fields, vals):
            if v not in _parse_field(field, lo, hi):
                return False
    except ValueError:
        return False
    return True


def cron_next(expr: str, base: datetime | None = None,
              max_days: int = 366) -> datetime | None:
    """Next minute (strictly after ``base``) matching ``expr``, or None."""
    dt = (base or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = dt + timedelta(days=max_days)
    while dt <= limit:
        if cron_matches(expr, dt):
            return dt
        dt += timedelta(minutes=1)
    return None


class Scheduler:
    """Load/save jobs and compute which are due."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._jobs: list[dict] = []
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            jobs = data.get("jobs") if isinstance(data, dict) else None
        except Exception:
            jobs = None
        self._jobs = [self._clean(j) for j in (jobs or []) if isinstance(j, dict)]
        self._jobs = [j for j in self._jobs if j]

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"jobs": self._jobs}, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            pass

    @staticmethod
    def _clean(job: dict) -> dict | None:
        name = str(job.get("name", "")).strip()
        request = str(job.get("request", "")).strip()
        if not name or not request:
            return None
        out = {
            "name": name,
            "request": request,
            "project": str(job.get("project", "")).strip(),
            "enabled": bool(job.get("enabled", True)),
            "last_run": float(job.get("last_run", 0) or 0),
        }
        if job.get("cron"):
            expr = str(job["cron"]).strip()
            if len(expr.split()) != 5:
                return None
            out["cron"] = expr
        elif job.get("every"):
            out["every"] = max(1, int(job["every"]))
        else:
            return None
        return out

    def jobs(self) -> list[dict]:
        return [dict(j) for j in self._jobs]

    def add(self, job: dict) -> list[dict]:
        clean = self._clean(job)
        if not clean:
            raise ValueError("job needs name, request, and cron or every")
        if not clean.get("last_run"):
            clean["last_run"] = time.time()  # first fire after one interval
        self._jobs = [j for j in self._jobs if j["name"] != clean["name"]]
        self._jobs.append(clean)
        self.save()
        return self.jobs()

    def remove(self, name: str) -> list[dict]:
        self._jobs = [j for j in self._jobs if j["name"] != name]
        self.save()
        return self.jobs()

    def due(self, now: datetime | None = None) -> list[dict]:
        """Jobs whose next fire time has passed; advances ``last_run``."""
        now = now or datetime.now()
        now_ts = now.timestamp()
        out: list[dict] = []
        changed = False
        for j in self._jobs:
            if not j.get("enabled"):
                continue
            if "every" in j:
                nxt = j.get("last_run", 0) + j["every"]
                if now_ts >= nxt:
                    out.append(dict(j))
                    j["last_run"] = now_ts
                    changed = True
            else:
                base = (datetime.fromtimestamp(j["last_run"]) if j.get("last_run")
                        else now - timedelta(minutes=1))
                nxt = cron_next(j["cron"], base)
                if nxt and nxt <= now:
                    out.append(dict(j))
                    j["last_run"] = now_ts
                    changed = True
        if changed:
            self.save()
        return out

    def run_due(self, dispatch, now: datetime | None = None) -> list[dict]:
        """Run each due job through ``dispatch(job)``; returns what ran."""
        ran: list[dict] = []
        for job in self.due(now):
            try:
                dispatch(job)
                job["ok"] = True
            except Exception as e:  # noqa: BLE001 - keep the scheduler alive
                job["ok"] = False
                job["error"] = str(e)
            ran.append(job)
        return ran
