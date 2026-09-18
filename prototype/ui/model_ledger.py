"""The model efficiency ledger: append-only evidence for the promotion rule.

Every delegated run appends one JSON line to ``<data_dir>/model-ledger.jsonl``
(``agent_runner`` writes it when a run settles). The ledger itself records only
what the runner knows for certain -- the agent, the model, the brief title, the
workdir, the start/finish times, the exit code and the run directory. It does
**not** record a verdict: that belongs to the supervisor, which journals it
separately in ``<data_dir>/agent-verify/journal.jsonl``.

So the verdict is a **read-only join at report time**: :func:`summary` streams
the journal, indexes it by agent id, and matches the journal entry for a run by
time window (a run's journal line is written *after* it settles). The supervisor
module is fenced (KB-08) and is never touched for this.

Design rules:

* **Append-only.** :func:`record` only ever appends one line; nothing rewrites or
  prunes the file. A report over a >5 MB file still streams it line by line.
* **Fail-open.** A ledger write must never break or delay a delegation: every
  error in :func:`record` is swallowed, logged, and reported as ``None``. The
  caller in ``agent_runner`` wraps the call again as a second safety net.
* **No invented data.** The run log carries no token-usage lines today, so the
  ``tokens`` field is left ``null`` -- it is a placeholder, not a measurement.

Shape of one row (``schema_version`` on every row)::

    schema_version, ts, agent_id, model, title, workdir,
    started, finished, duration, exit_code, note, run_dir, tokens

Pure stdlib and importable standalone. CLI::

    python model_ledger.py report [--json]
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1
LEDGER_NAME = "model-ledger.jsonl"
JOURNAL_REL = ("agent-verify", "journal.jsonl")

# A run may be verified well after it settles (the supervisor checks agents in
# turn), so the join looks forward from ``finished`` by this window. When more
# than one journal line matches, the one closest to ``finished`` wins.
DEFAULT_MATCH_WINDOW = 3600.0
# Clock skew allowance backwards from ``started`` when matching a journal line.
SKEW = 5.0

TABLE_COLUMNS = ("model", "runs", "verified", "needs_fix", "other",
                 "verify%", "median", "first seen", "last seen")


def _data_dir() -> Path:
    """Same data-dir resolution as ``agent_runner._data_dir()``."""
    try:
        import jarvis_paths

        return Path(jarvis_paths.data_dir())
    except Exception:  # noqa: BLE001 - standalone use
        return Path(__file__).resolve().parents[1] / "data"


def ledger_path(data_dir: str | Path | None = None) -> Path:
    """``<data_dir>/model-ledger.jsonl`` (data_dir defaults to the runtime dir)."""
    base = Path(data_dir) if data_dir else _data_dir()
    return base / LEDGER_NAME


def journal_path(data_dir: str | Path | None = None) -> Path:
    """``<data_dir>/agent-verify/journal.jsonl`` (the supervisor's journal)."""
    base = Path(data_dir) if data_dir else _data_dir()
    return base.joinpath(*JOURNAL_REL)


def record(*, agent_id: str = "", model: str = "", title: str = "",
           workdir: str = "", started: float | None = None,
           finished: float | None = None, exit_code: int | None = None,
           note: str = "", run_dir: str = "", tokens=None,
           path: str | Path | None = None,
           now: float | None = None) -> dict | None:
    """Append one settled run to the ledger; fail-open, never raises.

    Returns the row that was written, or ``None`` when the write failed (the
    failure is logged and swallowed -- a ledger problem must never break a
    delegation). ``duration`` is derived from ``started``/``finished``. The
    ``tokens`` field stays ``None`` because the run log has no usage lines.
    """
    try:
        duration = None
        if started is not None and finished is not None:
            duration = round(float(finished) - float(started), 3)
        row = {
            "schema_version": SCHEMA_VERSION,
            "ts": float(now if now is not None else time.time()),
            "agent_id": str(agent_id or ""),
            "model": str(model or ""),
            "title": str(title or ""),
            "workdir": str(workdir or ""),
            "started": float(started) if started is not None else None,
            "finished": float(finished) if finished is not None else None,
            "duration": duration,
            "exit_code": int(exit_code) if exit_code is not None else None,
            "note": str(note or ""),
            "run_dir": str(run_dir or ""),
            # The run log carries no token-usage lines today: leave it null,
            # do not invent numbers.
            "tokens": None if tokens is None else tokens,
        }
        target = Path(path) if path else ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return row
    except Exception:  # noqa: BLE001 - fail-open by design
        LOGGER.exception("model ledger: could not append to %s",
                         path or "the default ledger")
        return None


def load(path: str | Path | None = None):
    """Yield ledger rows oldest-first, streaming the file line by line.

    Blank lines and malformed JSON are skipped; a missing file yields nothing.
    This is a generator, so a >5 MB ledger is never held in memory at once.
    """
    target = Path(path) if path else ledger_path()
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def _load_journal_index(path: Path) -> dict[str, list[tuple[float, str]]]:
    """Index the supervisor's journal by agent id: ``{id: [(ts, verdict)]}``."""
    index: dict[str, list[tuple[float, str]]] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue
                aid = row.get("agent_id")
                verdict = row.get("verdict")
                ts = row.get("timestamp")
                if not aid or verdict is None:
                    continue
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    continue
                index.setdefault(str(aid), []).append((ts, str(verdict)))
    except OSError:
        pass
    return index


def _match_verdict(row: dict,
                   index: dict[str, list[tuple[float, str]]],
                   window: float) -> str | None:
    """The journal verdict for one settled row, or ``None``.

    Matches on agent id plus the time window ``[started - SKEW,
    finished + window]``; the candidate closest to ``finished`` wins so two runs
    of the same agent in quick succession each get their own verdict.
    """
    started = row.get("started")
    finished = row.get("finished")
    if finished is None:
        return None
    try:
        finished = float(finished)
        low = (float(started) - SKEW) if started is not None else None
    except (TypeError, ValueError):
        return None
    best: str | None = None
    best_delta = None
    for ts, verdict in index.get(str(row.get("agent_id") or ""), ()):
        if low is not None and ts < low:
            continue
        if ts > finished + window:
            continue
        delta = abs(ts - finished)
        if best_delta is None or delta < best_delta:
            best, best_delta = verdict, delta
    return best


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return None


def summary(rows=None, *, path: str | Path | None = None,
            journal: str | Path | None = None,
            data_dir: str | Path | None = None,
            window: float = DEFAULT_MATCH_WINDOW) -> list[dict]:
    """Per-model aggregates, joined read-only with the supervisor's verdicts.

    Returns one dict per model with ``runs``, ``verdicts`` (counts by verdict),
    ``checked`` (runs with a journal verdict), ``verify_rate`` (verified over
    checked), ``median_duration`` (settled runs, seconds), and ``first_seen`` /
    ``last_seen`` (local ISO strings). Sorted by runs desc, then model.
    """
    index = _load_journal_index(Path(journal) if journal
                                else journal_path(data_dir))

    if rows is not None:
        source = rows
    elif path is not None:
        source = load(path)
    else:
        source = load(ledger_path(data_dir))
    per_model: dict[str, dict] = {}
    for row in source:
        model = str(row.get("model") or "(unknown)")
        agg = per_model.setdefault(model, {
            "model": model,
            "runs": 0,
            "verdicts": {},
            "durations": [],
            "first": None,
            "last": None,
        })
        agg["runs"] += 1
        verdict = _match_verdict(row, index, window)
        if verdict:
            agg["verdicts"][verdict] = agg["verdicts"].get(verdict, 0) + 1
        duration = row.get("duration")
        if duration is not None:
            try:
                agg["durations"].append(float(duration))
            except (TypeError, ValueError):
                pass
        seen = row.get("started")
        if seen is None:
            seen = row.get("finished")
        if seen is None:
            seen = row.get("ts")
        try:
            seen = float(seen)
        except (TypeError, ValueError):
            seen = None
        if seen is not None:
            if agg["first"] is None or seen < agg["first"]:
                agg["first"] = seen
            if agg["last"] is None or seen > agg["last"]:
                agg["last"] = seen

    out: list[dict] = []
    for agg in per_model.values():
        checked = sum(agg["verdicts"].values())
        verified = agg["verdicts"].get("verified", 0)
        durations = agg["durations"]
        out.append({
            "model": agg["model"],
            "runs": agg["runs"],
            "verdicts": agg["verdicts"],
            "checked": checked,
            "verify_rate": (verified / checked) if checked else None,
            "median_duration": (statistics.median(durations)
                                if durations else None),
            "first_seen": _iso(agg["first"]),
            "last_seen": _iso(agg["last"]),
        })
    out.sort(key=lambda r: (-r["runs"], r["model"]))
    return out


def _fmt_duration(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):.1f}s"


def _fmt_rate(value) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.0f}%"


def render_report(rows: list[dict], *, as_json: bool = False) -> str:
    """Render :func:`summary` output as a JSON document or an aligned table."""
    if as_json:
        return json.dumps({"schema_version": SCHEMA_VERSION, "models": rows},
                          indent=2, default=str)
    if not rows:
        return "no runs recorded"

    table: list[list[str]] = []
    for r in rows:
        verdicts = r.get("verdicts") or {}
        runs = r.get("runs", 0)
        verified = verdicts.get("verified", 0)
        needs_fix = verdicts.get("needs_fix", 0)
        # Reconciles with ``runs``: everything not verified/needs_fix, i.e.
        # inconclusive/reported verdicts plus runs the supervisor has not
        # checked yet.
        other = max(0, runs - verified - needs_fix)
        table.append([
            str(r.get("model") or ""),
            str(r.get("runs", 0)),
            str(verified),
            str(needs_fix),
            str(other),
            _fmt_rate(r.get("verify_rate")),
            _fmt_duration(r.get("median_duration")),
            str(r.get("first_seen") or "-"),
            str(r.get("last_seen") or "-"),
        ])

    widths = [len(c) for c in TABLE_COLUMNS]
    for line in table:
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))
    # Right-align the numeric columns, left-align model and the dates.
    right = {1, 2, 3, 4, 5, 6}

    def row_text(cells: list[str]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            parts.append(cell.rjust(widths[i]) if i in right
                         else cell.ljust(widths[i]))
        return "  ".join(parts).rstrip()

    out = [row_text(list(TABLE_COLUMNS))]
    out.append("  ".join("-" * w for w in widths))
    out.extend(row_text(line) for line in table)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="model_ledger",
                                     description="Model efficiency ledger")
    sub = parser.add_subparsers(dest="command")
    report_cmd = sub.add_parser("report", help="print the per-model summary")
    report_cmd.add_argument("--json", action="store_true",
                            help="emit JSON instead of a table")
    report_cmd.add_argument("--data-dir", default="",
                            help="data dir (defaults to the runtime dir)")
    report_cmd.add_argument("--window", type=float,
                            default=DEFAULT_MATCH_WINDOW,
                            help="journal match window in seconds")
    args = parser.parse_args(argv)

    if args.command != "report":
        parser.print_help()
        return 1
    rows = summary(data_dir=(args.data_dir or None), window=args.window)
    print(render_report(rows, as_json=args.json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
