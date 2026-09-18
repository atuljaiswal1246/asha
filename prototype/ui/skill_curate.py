"""Skill curation — the brain's authority over which Agent Skills may be used.

The supervisor (DeepSeek V4 Flash, the brain) curates Agent Skills (SKILL.md):
it decides which are good and lets them into coding briefs WITHOUT user
approval (coding stays apply-gated, but skill SELECTION is not). These
guardrails are the curator's authority to hold and are NEVER relaxed.
Fail-closed: a skill that fails ANY guardrail is never injected.

Guardrails (5):
  1. NO SECRETS        — must not instruct reading/sending secret material
                         (.env contents, api keys, tokens, credentials…).
  2. NO DESTRUCTIVE    — no rm -rf, git push --force / -f, DROP TABLE, etc.
  3. PERSONAL-DATA GATE— must not harvest/sell personal or PII data.
  4. VOICE-FIT         — content must be appropriate for the user's assistant
                         (never adult/gambling/darkweb/malware/exploit/…).
  5. VERIFIED-ONLY     — only skills with trusted provenance (frontmatter
                         ``verified: true`` or a name on the curated
                         allowlist) may be used.

Safety: the curator NEVER executes anything. It does a quick text sanity read
of the SKILL.md body (what load_skill returns) and returns
``{"verdict": accept|reject, "reason": …, "skill": name}``. The curated set
is tracked in memory and persisted best-effort to ``skills/.curated.json``.
"""

import datetime
import json
import logging
import os
import re

logger = logging.getLogger("asha.skill_curate")

# prototype/skills/ — one directory above this module (ui/).
_SKILLS_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills"
)
_CURATED_FILE = os.path.join(_SKILLS_ROOT, ".curated.json")

# ---- Guardrail 1 — secret-reading / exfiltration ----
# A reject needs BOTH a secret noun AND an exfiltration action somewhere in the
# body (e.g. "read .env and print the API keys"). Review instructions like
# "flag hardcoded api keys" do NOT trigger (their verbs are not in the action
# list), so security-review skills stay usable.
_SECRET_NOUNS = re.compile(
    r"(?i)(\.env\b|api[_ -]?keys?|secrets?|access[_ -]?tokens?|bearer[_ -]?tokens?|"
    r"credentials?|private[_ -]?keys?|id_rsa|passwords?|tokens?|webhook[_ -]?urls?|"
    r"supervisor[_ -]?api[_ -]?keys?|opencode[_ -]?api[_ -]?keys?)"
)
_SECRET_ACTIONS = re.compile(
    r"(?i)\b(print|echo|send|upload|post|exfiltrat|steal|dump|leak|transmit|"
    r"publish|phish|scrape|extract)\b"
)

# ---- Guardrail 2 — destructive defaults ----
_DESTRUCTIVE = re.compile(
    r"(?i)\b(rm\s+(-rf|-fr|-Rf)?|rmdir|git\s+push\s+(--force|-f)|force\s+push|"
    r"git\s+reset\s+--hard|git\s+clean\s+(-f|-fd|-ffd)|drop\s+table|truncate\s+"
    r"table|mkfs\.|dd\s+.*of=/dev|shutdown|reboot|fdisk|fork\s+bomb|"
    r"chmod\s+-R\s+777\s+/|format\s+[a-zA-Z]:?\\|:\(\s*\{\s*:)\b"
)

# ---- Guardrail 3 — personal-data gate ----
_PII_VERB = re.compile(
    r"(?i)\b(harvest|scrape|collect|extract|exfiltrat|sell|doxx|stalk|spy|"
    r"surveil|swat)\b"
)
_PII_NOUN = re.compile(
    r"(?i)\b(personal|private|pii|email|phone|ssn|social[\s-]?security|"
    r"financial|bank|credit[\s-]?card|health|medical|biometric|location)\b"
)

# ---- Guardrail 4 — voice-fit (appropriate for the assistant) ----
_HARMFUL = re.compile(
    r"(?i)\b(nsfw|adult\s*content|porn|gambl|casino|dark\s*web|deep\s*web|"
    r"malware|ransomware|exploit\s*kits?|ddos|booter|phishing|spam|crack|keygen|"
    r"fake\s*id|passport\s*scan|weapon|guns?\s*build|drugs?|synth\s*lab)\b"
)

# ---- Guardrail 5 — verified-only ----
# The curated starter set (prototype/skills/) + any skill the brain has
# already verified. Everything else is unverified and rejected fail-closed.
_VERIFIED_NAMES = frozenset(
    {"hello-world", "code-review", "debugging", "writing-plans", "sql-query-writing"}
)

_FRONT_META_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n?", re.S)
_FRONT_FIELD_RE = re.compile(r"(?m)^([A-Za-z_][\w-]*):\s*(.*)$")

_curated = {}


def _parse_frontmatter(text):
    """Read YAML-ish frontmatter of a SKILL.md (parse only, never execute).

    Returns {field: value, …} plus the body after the closing ``---``. Missing
    frontmatter returns {} (the whole text is treated as the body).
    """
    if not text:
        return {}
    m = _FRONT_META_RE.match(text)
    if not m:
        return {"_body": text.strip()}
    meta = {}
    for key, val in _FRONT_FIELD_RE.findall(m.group(1)):
        meta[key.strip().lower().replace(" ", "_")] = val.strip().strip('"\'')
    meta["_body"] = text[m.end():].strip()
    return meta


def _unpack(skill):
    """Normalize a candidate skill -> (name, description, body_text, meta).

    Accepts either a dict ``{"name", "description", "body"|"text", "frontmatter"(dict, optional),
    "verified"(bool, optional)}`` or an object with those as attributes (e.g. the
    ``skills.Skill`` the runtime returns). ``body`` is the SKILL.md body WITHOUT
    frontmatter; its trusted metadata arrives via ``frontmatter`` when the caller
    has it (the runtime strips and parses the YAML itself).
    """
    if isinstance(skill, dict):
        raw_body = (
            skill.get("body")
            or skill.get("text")
            or skill.get("content")
            or skill.get("instructions")
            or ""
        )
        name0 = skill.get("name")
        desc0 = skill.get("description")
        fm0 = skill.get("frontmatter")
        verified0 = skill.get("verified")
    else:
        raw_body = getattr(skill, "body", None) or ""
        name0 = getattr(skill, "name", None)
        desc0 = getattr(skill, "description", None)
        fm0 = getattr(skill, "frontmatter", None)
        verified0 = None
    meta = _parse_frontmatter(raw_body) if isinstance(raw_body, str) else {}
    if isinstance(fm0, dict):
        for key, val in fm0.items():
            if key in ("_body",):
                continue
            meta.setdefault(str(key).lower(), val)
    if verified0 is not None:
        meta["verified"] = bool(verified0)
    name = str(name0 or meta.get("name") or "").strip()
    desc = str(desc0 or meta.get("description") or "").strip()
    body = str(meta.get("_body") or raw_body or "").strip()
    return name, desc, body, meta


def _is_verified(name, meta):
    verified = meta.get("verified")
    if isinstance(verified, bool):
        return verified
    return str(verified or "").strip().lower() in ("true", "yes", "1") or name in _VERIFIED_NAMES


def _reject(name, reason):
    return {"verdict": "reject", "reason": reason, "skill": name}


def _track(name, verdict, reason):
    _curated[name] = {
        "verdict": verdict,
        "reason": reason,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        with open(_CURATED_FILE, "w") as fh:
            json.dump(_curated, fh, indent=2)
    except OSError:
        pass  # in-memory still valid; persistence is best-effort


def curated_skills():
    """Read-only snapshot of the tracked curated set {name -> record}."""
    return dict(_curated)


def is_curated(name):
    """True when the named skill is in the tracked curated set."""
    return name in _curated


def curate_skill(skill):
    """Run all 5 guardrails on a candidate skill. Fail-closed.

    skill: dict or runtime Skill — {"name", "description", "body"} plus a
    trusted ``frontmatter``/``verified`` channel when available.
    Returns {"verdict": accept|reject, "reason": str, "skill": name}.
    """
    packed = _unpack(skill)
    if not any(packed):
        return _reject("", "malformed skill (expected a dict)")
    name, _desc, body, meta = packed
    if not name or not body:
        return _reject("", "missing name and/or body (malformed SKILL.md)")
    checks = []
    if _SECRET_NOUNS.search(body) and _SECRET_ACTIONS.search(body):
        checks.append("no-secrets guardrail: reads/sends secret material")
    if _DESTRUCTIVE.search(body):
        checks.append("no-destructive guardrail: rm -rf / force-push / data-destroying")
    if _PII_VERB.search(body) and _PII_NOUN.search(body):
        checks.append("personal-data gate: harvests/collects personal data")
    if _HARMFUL.search(body):
        checks.append("voice-fit: content inappropriate for the assistant")
    if not _is_verified(name, meta):
        checks.append("verified-only: no trusted provenance (verified not set)")
    if checks:
        _track(name, "reject", "; ".join(checks))
        return _reject(name, "; ".join(checks))
    _track(name, "accept", "all guardrails passed")
    return {"verdict": "accept", "reason": "all guardrails passed", "skill": name}