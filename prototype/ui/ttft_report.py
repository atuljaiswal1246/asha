#!/usr/bin/env python3
"""Pipecat latency log parser and TTFT reporter.

Parses /tmp/jarvis-dev-ws.log (or any pipecat log) and reports per-processor
latency metrics.  Stdlib only — no third-party deps.

Usage:
    python ttft_report.py /tmp/jarvis-dev-ws.log
    python ttft_report.py /tmp/jarvis-dev-ws.log --turns-only
"""

from __future__ import annotations

import argparse
import re
import statistics
from datetime import datetime, timezone

BASELINE = {
    "ttfb_ms": 559,
    "n": 15,
    "date": "2026-09-02",
    "model": "llama-server Qwen3.5-4B-Q4_K_M (local, enable_thinking=false)",
    "model_token": "qwen3.5-4b-q4_k_m",
    "metric": "transcript-final -> first LLM text chunk",
    "source": "notes/phase0-baseline.md",
    "prompt_tokens": "small (4096-token context window)",
}
REGRESSION_THRESHOLD_S = 0.5

# Back-compat alias
BASELINE_TTFB_MS = BASELINE["ttfb_ms"]

# --- Log line patterns -------------------------------------------------------

_TSTAMP_RE = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})"

_TTFB_RE = re.compile(
    _TSTAMP_RE + r".+:stop_ttfb_metrics:\d+ - (\S+) TTFB: ([\d.]+)s"
)
_TTFAT_RE = re.compile(
    _TSTAMP_RE + r".+:stop_ttfat_metrics:\d+ - (\S+) TTFAT: ([\d.]+)s(?: \(([\d.]+)s thinking\))?"
)
_TTFA_RE = re.compile(
    _TSTAMP_RE + r".+:process_ttfa_metrics:\d+ - (\S+) TTFA: ([\d.]+)s"
)
_LLM_USAGE_RE = re.compile(
    _TSTAMP_RE
    + r".+:start_llm_usage_metrics:\d+ - (\S+) prompt tokens: (\d+), completion tokens: (\d+)"
    + r"(?:, cache read input tokens: (\d+))?"
    + r"(?:, reasoning tokens: (\d+))?"
)

_GENERATION_RE = re.compile(r"Generating chat from context \[")
_MODEL_USED_RE = re.compile(r"\[LLM\] using (\S+) \(([^)]+)\)")
_GREETING_DONE_RE = re.compile(r"ready emitted \(greeting finished\)")


def service_of(processor: str) -> str:
    """Classify a pipecat processor name: LLM, STT, TTS or other.

    Latency only means something per service - an STT first-byte is not an LLM
    first-byte, and mixing them is how a 1.5s reply hid behind 0.6s transcripts.
    """
    for token in ("LLM", "STT", "TTS"):
        if token in processor:
            return token
    return "other"


def _parse_ts(ts: str) -> float:
    """Return epoch seconds from a pipecat timestamp string."""
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _find_greeting_line_indices(lines: list[str]) -> set[int]:
    """Return line indices that belong to connect-time greeting windows.

    Anchored on the "ready emitted (greeting finished)" marker, not on the
    greeting prompt text: the greeting stays in the conversation context, so
    every later turn's logged context still *contains* that prompt. Matching it
    swallowed the following turns' LLM metrics (they were reported as greeting
    and then excluded). Everything from the connect's generation line up to its
    ready marker is greeting; nothing after the marker ever is.
    """
    generation_indices = [i for i, line in enumerate(lines) if _GENERATION_RE.search(line)]
    ready_indices = [i for i, line in enumerate(lines) if _GREETING_DONE_RE.search(line)]

    exclude: set[int] = set()
    for ri in ready_indices:
        before = [gi for gi in generation_indices if gi < ri]
        if not before:
            continue
        for j in range(before[-1], ri + 1):
            exclude.add(j)
    return exclude


def parse_log(path: str) -> list[dict]:
    """Parse a pipecat log file and return one record per processor turn.

    Metrics from the same processor within TURN_WINDOW_S seconds are merged
    into a single record.  Each record:
        {timestamp, processor, ttfb_s, ttft_s, thinking_s, ttfa_s,
         tokens_in, tokens_out, cache_read, is_greeting}
    """
    TURN_WINDOW_S = 5.0

    with open(path) as f:
        lines = f.readlines()

    greeting_lines = _find_greeting_line_indices(lines)

    # Phase 1: extract raw events
    events: list[dict] = []
    for i, line in enumerate(lines):
        is_greeting = i in greeting_lines

        m = _TTFB_RE.search(line)
        if m:
            events.append({
                "ts": _parse_ts(m.group(1)),
                "ts_str": m.group(1),
                "processor": m.group(2),
                "kind": "ttfb",
                "value": float(m.group(3)),
                "is_greeting": is_greeting,
            })
            continue

        m = _TTFAT_RE.search(line)
        if m:
            events.append({
                "ts": _parse_ts(m.group(1)),
                "ts_str": m.group(1),
                "processor": m.group(2),
                "kind": "ttft",
                "value": float(m.group(3)),
                "thinking": float(m.group(4)) if m.group(4) else None,
                "is_greeting": is_greeting,
            })
            continue

        m = _TTFA_RE.search(line)
        if m:
            events.append({
                "ts": _parse_ts(m.group(1)),
                "ts_str": m.group(1),
                "processor": m.group(2),
                "kind": "ttfa",
                "value": float(m.group(3)),
                "is_greeting": is_greeting,
            })
            continue

        m = _LLM_USAGE_RE.search(line)
        if m:
            events.append({
                "ts": _parse_ts(m.group(1)),
                "ts_str": m.group(1),
                "processor": m.group(2),
                "kind": "usage",
                "tokens_in": int(m.group(3)),
                "tokens_out": int(m.group(4)),
                "cache_read": int(m.group(5)) if m.group(5) else 0,
                "reasoning": int(m.group(6)) if m.group(6) else 0,
                "is_greeting": is_greeting,
            })
            continue

    # Phase 2: group events by processor within time windows
    events.sort(key=lambda e: (e["processor"], e["ts"]))
    records: list[dict] = []

    for proc, group in _group_by(events, lambda e: e["processor"]):
        current: dict | None = None
        for ev in group:
            if current is None or (ev["ts"] - current["_ts"]) > TURN_WINDOW_S:
                current = {
                    "timestamp": ev["ts_str"],
                    "processor": proc,
                    "service": service_of(proc),
                    "ttfb_s": None,
                    "ttft_s": None,
                    "thinking_s": None,
                    "ttfa_s": None,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "cache_read": 0,
                    "reasoning_tokens": 0,
                    "is_greeting": ev["is_greeting"],
                    "_ts": ev["ts"],
                }
                records.append(current)

            if ev["kind"] == "ttfb":
                current["ttfb_s"] = ev["value"]
            elif ev["kind"] == "ttft":
                current["ttft_s"] = ev["value"]
                current["thinking_s"] = ev.get("thinking")
            elif ev["kind"] == "ttfa":
                current["ttfa_s"] = ev["value"]
            elif ev["kind"] == "usage":
                current["tokens_in"] = ev["tokens_in"]
                current["tokens_out"] = ev["tokens_out"]
                current["cache_read"] = ev["cache_read"]
                current["reasoning_tokens"] = ev.get("reasoning", 0)

    # Preserve original log order
    records.sort(key=lambda r: r.pop("_ts"))
    return records


def _group_by(items, key_fn):
    """Yield (key, group) pairs from a pre-sorted list."""
    from itertools import groupby
    for k, g in groupby(items, key=key_fn):
        yield k, list(g)


def _percentile(data: list[float], p: float) -> float:
    """Return the p-th percentile (0-100) of *data*."""
    if not data:
        return 0.0
    k = (len(data) - 1) * p / 100.0
    f = int(k)
    c = f + 1
    if c >= len(data):
        return data[f]
    return data[f] + (data[c] - data[f]) * (k - f)


def stats(records: list[dict]) -> dict:
    """Compute count, min, median, p90, max for each numeric metric."""
    metrics = ["ttfb_s", "ttft_s", "thinking_s", "ttfa_s",
               "tokens_in", "tokens_out", "cache_read", "reasoning_tokens"]
    result: dict[str, dict] = {}
    for m in metrics:
        values = [r[m] for r in records if r[m] is not None]
        if not values:
            result[m] = {"count": 0, "min": None, "median": None, "p90": None, "max": None}
            continue
        s = sorted(values)
        result[m] = {
            "count": len(s),
            "min": s[0],
            "median": statistics.median(s),
            "p90": _percentile(s, 90),
            "max": s[-1],
        }
    return result


_ROWS = [("ttfb_s", "TTFB (s)"), ("ttft_s", "TTFAT (s)"), ("thinking_s", "thinking (s)"),
         ("ttfa_s", "TTFA (s)"), ("tokens_in", "Tokens in"), ("tokens_out", "Tokens out"),
         ("cache_read", "Cache read"), ("reasoning_tokens", "Reasoning")]


def _service_table(records: list[dict]) -> list[str]:
    """Per-service metric rows. Latency is only meaningful within a service."""
    out: list[str] = []
    for service in ("LLM", "STT", "TTS", "other"):
        recs = [r for r in records if r["service"] == service]
        if not recs:
            continue
        s = stats(recs)
        out.append(service)
        hdr = f"  {'Metric':<12} {'Count':>5} {'Min':>8} {'Median':>8} {'P90':>8} {'Max':>8}"
        out.append(hdr)
        out.append("  " + "-" * (len(hdr) - 2))
        for key, label in _ROWS:
            if service != "LLM" and key in ("tokens_in", "tokens_out", "cache_read", "reasoning_tokens"):
                continue
            d = s[key]
            if d["count"] == 0:
                continue
            out.append(
                f"  {label:<12} {d['count']:>5} {d['min']:>8.3f} {d['median']:>8.3f} "
                f"{d['p90']:>8.3f} {d['max']:>8.3f}"
            )
        out.append("")
    return out


def models_used(path: str) -> list[str]:
    """Return the LLM models that served turns in this log, in order."""
    seen: list[str] = []
    with open(path) as f:
        for line in f:
            m = _MODEL_USED_RE.search(line)
            if m and m.group(2) not in seen:
                seen.append(m.group(2))
    return seen


def report(path: str, turns_only: bool = False, assume_comparable: bool = False) -> str:
    """Generate a compact report table with baseline comparison.

    If *turns_only* is True, exclude connect-time greetings from the verdict
    and say which numbers were excluded.
    """
    records = parse_log(path)
    greeting_count = sum(1 for r in records if r["is_greeting"])

    if turns_only:
        turn_records = [r for r in records if not r["is_greeting"]]
        excluded = greeting_count
    else:
        turn_records = records
        excluded = 0

    lines: list[str] = []
    lines.append(f"=== TTFT Report: {path} ===")
    lines.append(f"Total records: {len(records)}  |  Turns: {len(turn_records)}  |  Excluded (greeting): {excluded}")
    lines.append("")
    lines.extend(_service_table(turn_records))

    # Baseline comparison - LLM TTFB only, and it must be the LLM's own number:
    # an STT first-byte was previously counted here and reported as 'no data'.
    llm_ttfb = [r["ttfb_s"] for r in turn_records
                if r["service"] == "LLM" and r["ttfb_s"] is not None]
    stt_rows = [r for r in turn_records if r["service"] == "STT"]
    tts_rows = [r for r in turn_records if r["service"] == "TTS"]

    def _med(rows, key):
        vals = [r[key] for r in rows if r[key] is not None]
        return statistics.median(vals) if vals else None

    used_models = models_used(path)
    prompt_med = _med([r for r in turn_records if r["service"] == "LLM"], "tokens_in")
    lines.append(f"Conditions: metric=LLM TTFB (pipecat first byte)  |  "
                 f"model={', '.join(used_models) if used_models else 'unknown'}  |  "
                 f"prompt tokens median={prompt_med:.0f}" if prompt_med else
                 f"Conditions: metric=LLM TTFB (pipecat first byte)  |  "
                 f"model={', '.join(used_models) if used_models else 'unknown'}")
    lines.append(f"Baseline: {BASELINE['ttfb_ms']} ms  |  model={BASELINE['model']}  |  "
                 f"metric={BASELINE['metric']}  |  n={BASELINE['n']} ({BASELINE['date']}, "
                 f"{BASELINE['source']})")

    def _norm(s: str) -> str:
        return s.lower().replace("_", "-")

    model_matches = bool(used_models) and any(
        _norm(BASELINE["model_token"]) in _norm(m) for m in used_models
    )
    comparable = assume_comparable or model_matches

    if not llm_ttfb:
        lines.append("Verdict: NO DATA - no LLM TTFB records in this log")
    elif not comparable:
        lines.append(f"Measured median LLM TTFB: {statistics.median(llm_ttfb) * 1000:.0f} ms  "
                     f"(n={len(llm_ttfb)})")
        lines.append("Verdict: NOT COMPARABLE - the baseline was measured on a different model. "
                     "A delta against it would be meaningless; re-baseline on this stack "
                     "(see notes/ttft-baseline.md) or pass --assume-comparable to force it.")
    else:
        median_ttfb_ms = statistics.median(llm_ttfb) * 1000
        delta_ms = median_ttfb_ms - BASELINE["ttfb_ms"]
        if delta_ms >= REGRESSION_THRESHOLD_S * 1000:
            verdict = f"REGRESSION >= {REGRESSION_THRESHOLD_S} s (delta: +{delta_ms:.0f} ms)"
        elif delta_ms < 0:
            verdict = f"within budget (delta: {delta_ms:+.0f} ms - faster than baseline)"
        else:
            verdict = f"within budget (delta: +{delta_ms:.0f} ms)"
        lines.append(f"Measured median LLM TTFB: {median_ttfb_ms:.0f} ms  (n={len(llm_ttfb)})")
        lines.append(f"Verdict: {verdict}")

    stt_med = _med(stt_rows, "ttfb_s")
    tts_med = _med(tts_rows, "ttfb_s")
    if stt_med is not None or tts_med is not None:
        parts = []
        if stt_med is not None:
            parts.append(f"STT first-byte {stt_med * 1000:.0f} ms")
        if tts_med is not None:
            parts.append(f"TTS first-byte {tts_med * 1000:.0f} ms")
        lines.append("Context (not the verdict): " + ", ".join(parts))

    if turns_only and excluded > 0:
        lines.append("")
        lines.append(f"Note: {excluded} greeting record(s) excluded from verdict (--turns-only)")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipecat TTFT report")
    parser.add_argument("log", help="Path to pipecat log file")
    parser.add_argument(
        "--turns-only",
        action="store_true",
        help="Exclude connect-time greetings from verdict",
    )
    parser.add_argument(
        "--assume-comparable",
        action="store_true",
        help="Compare against the baseline even though it was measured on a different model",
    )
    args = parser.parse_args()
    print(report(args.log, turns_only=args.turns_only,
                 assume_comparable=args.assume_comparable))


if __name__ == "__main__":
    main()
