"""OpenAI-style patch parser and fuzzy apply_patch implementation.

This module reads "*** Begin Patch / *** End Patch" edit scripts (the format
used by Claude Code's apply_patch and related file-editing agents) and applies
them to in-memory text using a 4-pass fuzzy matcher:

  pass 1  exact       ``line == line``
  pass 2  trimEnd     ``line.rstrip() == line.rstrip()``
  pass 3  trim        ``line.strip() == line.strip()``
pass 4  normalized  NFKC-fold both sides then strip (so NBSP collapses onto
                       a plain space); an explicit compatibility fold is
                       layered on top because stdlib NFKC does not map curly
                       quotes or en/em dashes onto their ASCII forms as one
                       might expect

Hunks are located left-to-right, the first pass that matches the whole window
wins, and a file keeps whatever pass matched its first hunk for all of its
remaining hunks, so later hunks stay aligned with the earlier fuzziness.

The core (``parse_patch`` / ``apply_hunks``) never touches the filesystem.
``apply_patch_text`` is the thin adapter that routes reads, writes and deletes
through caller-provided callbacks, which is what lets this core be unit-tested
with an in-memory dict and integrated with real I/O elsewhere.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

BEGIN_PATCH = "*** Begin Patch"
END_PATCH = "*** End Patch"
END_OF_FILE = "*** End of File"
ADD_FILE = "*** Add File:"
UPDATE_FILE = "*** Update File:"
DELETE_FILE = "*** Delete File:"


class PatchError(Exception):
    """Raised for malformed patch text or a hunk that cannot be located."""


@dataclass
class Hunk:
    """One ``@@`` block of an update.

    ``old_lines`` are the lines that must exist in the file (context +
    removed), ``new_lines`` the replacement lines (context + added). Both hold
    bare line contents with no leading " ", "-" or "+" prefix. An empty
    ``old_lines`` is a pure insertion (append at the end of the file); an
    empty ``new_lines`` is a pure deletion of ``old_lines``.
    """

    old_lines: list[str]
    new_lines: list[str]


@dataclass
class FilePatch:
    """One file operation parsed from a patch script."""

    op: str  # "add" | "update" | "delete"
    path: str
    hunks: list[Hunk]  # empty for add/delete
    content: list[str]  # "add" only: full new-file lines, no "+" prefix


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _remainder(line: str, directive: str) -> str:
    """Path text after ``*** <directive>:``; raises if missing."""
    prefix = f"{directive}:"
    path = line[len(prefix):].strip()
    if not path:
        raise PatchError(f"{prefix} is missing a path")
    return path


def _read_add_content(lines: list[str], i: int, path: str) -> tuple[list[str], int]:
    """Consume ``+``-prefixed lines for an Add File block; returns (content, i)."""
    content: list[str] = []
    n = len(lines)
    while i < n:
        line = lines[i]
        if line == "":
            i += 1
            continue
        if line.startswith("***"):
            break
        if line.startswith("+"):
            content.append(line[1:])
            i += 1
            continue
        raise PatchError(
            f"Malformed Add File {path}: expected '+'-prefixed line, got {line!r}"
        )
    if not content:
        raise PatchError(f"Add File {path} has no content")
    return content, i


def _read_hunk_body(lines: list[str], i: int) -> tuple[list[str], list[str], int]:
    """Consume context/removed/added lines until the next ``@@`` or ``***``.

    Returns ``(old_lines, new_lines, next_index)``. Bare blank lines are
    ignored (a genuine empty content line must be written with a prefix,
    e.g. a lone ``-`` or a single space).
    """
    old: list[str] = []
    new: list[str] = []
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("@@") or line.startswith("***"):
            break
        if line == "":
            i += 1
            continue
        prefix = line[0]
        if prefix in (" ", "-", "+"):
            if prefix in (" ", "-"):
                old.append(line[1:])
            if prefix in (" ", "+"):
                new.append(line[1:])
            i += 1
            continue
        raise PatchError(f"Malformed hunk line: {line!r}")
    return old, new, i


def _read_update_hunks(
    lines: list[str], i: int, path: str
) -> tuple[list[Hunk], int]:
    """Consume ``@@`` hunks until a ``***`` directive; returns (hunks, i)."""
    hunks: list[Hunk] = []
    n = len(lines)
    while i < n:
        line = lines[i]
        if line == "":
            i += 1
            continue
        if line == END_OF_FILE:
            i += 1
            continue
        if line.startswith("***"):
            break
        if line.startswith("@@"):
            i += 1
            old, new, i = _read_hunk_body(lines, i)
            if not old and not new:
                raise PatchError(f"Empty hunk in Update File {path}")
            hunks.append(Hunk(old_lines=old, new_lines=new))
            continue
        raise PatchError(f"Malformed line in Update File {path}: {line!r}")
    if not hunks:
        raise PatchError(f"Update File {path} has no hunks")
    return hunks, i


def parse_patch(text: str) -> list[FilePatch]:
    """Parse an ``*** Begin Patch ... *** End Patch`` script.

    Returns a list of :class:`FilePatch` in the order they appear. Raises
    :class:`PatchError` on malformed input: missing Begin/End directives,
    unknown directives, an Add File with no content, an Update File with no
    hunks, or unrecognized lines.
    """
    lines = text.splitlines()
    n = len(lines)

    i = 0
    while i < n and lines[i] == "":
        i += 1
    if i >= n or lines[i] != BEGIN_PATCH:
        raise PatchError("Patch must start with '*** Begin Patch'")
    i += 1

    patches: list[FilePatch] = []
    seen_end = False
    while i < n:
        line = lines[i]
        if line == "":
            i += 1
            continue
        if not line.startswith("***"):
            raise PatchError(f"Expected a directive, got {line!r}")
        if line == END_PATCH:
            if seen_end:
                raise PatchError("Duplicate '*** End Patch'")
            seen_end = True
            i += 1
            continue
        if line == END_OF_FILE:
            i += 1
            continue
        if line.startswith(ADD_FILE):
            path = _remainder(line, ADD_FILE)
            i += 1
            content, i = _read_add_content(lines, i, path)
            patches.append(FilePatch(op="add", path=path, hunks=[], content=content))
            continue
        if line.startswith(UPDATE_FILE):
            path = _remainder(line, UPDATE_FILE)
            i += 1
            hunks, i = _read_update_hunks(lines, i, path)
            patches.append(FilePatch(op="update", path=path, hunks=hunks, content=[]))
            continue
        if line.startswith(DELETE_FILE):
            path = _remainder(line, DELETE_FILE)
            i += 1
            patches.append(FilePatch(op="delete", path=path, hunks=[], content=[]))
            continue
        raise PatchError(f"Unknown directive: {line!r}")
    if not seen_end:
        raise PatchError("Missing '*** End Patch'")
    return patches


# ---------------------------------------------------------------------------
# The 4-pass fuzzy matcher
# ---------------------------------------------------------------------------

def _cmp_exact(a: str, b: str) -> bool:
    return a == b


def _cmp_trim_end(a: str, b: str) -> bool:
    return a.rstrip() == b.rstrip()


def _cmp_trim(a: str, b: str) -> bool:
    return a.strip() == b.strip()


_CURLY_DQUOTE = "\u201c\u201d"
_ASCII_DQUOTE = '""'
_CURLY_SQUOTE = "\u2018\u2019"
_ASCII_SQUOTE = "''"
_DASHES = "\u2013\u2014\u2015\u2212"
_ASCII_HYPHEN = "----"

# stdlib UnicodeData has no decomposition for curly double quotes or en/em
# dashes, so bare NFKC leaves them alone. Map them explicitly (in NFKC order)
# onto what the fold is documented to mean.
_QUOTE_DASH_TRANS = str.maketrans(
    _CURLY_DQUOTE + _CURLY_SQUOTE + _DASHES,
    _ASCII_DQUOTE + _ASCII_SQUOTE + _ASCII_HYPHEN,
)


def _normalize_for_match(line: str) -> str:
    """NFKC-fold *line*, collapse smart quotes/dashes, and strip."""
    return unicodedata.normalize("NFKC", line).translate(_QUOTE_DASH_TRANS).strip()


def _cmp_normalized(a: str, b: str) -> bool:
    return _normalize_for_match(a) == _normalize_for_match(b)


_STRATEGIES: tuple[Callable[[str, str], bool], ...] = (
    _cmp_exact,
    _cmp_trim_end,
    _cmp_trim,
    _cmp_normalized,
)


def _find_window(
    lines: list[str], old_lines: list[str], strategy: int, start_lo: int
) -> int | None:
    """Leftmost index, >= ``start_lo``, where every old line matches."""
    n = len(lines)
    m = len(old_lines)
    compare = _STRATEGIES[strategy]
    for i in range(start_lo, n - m + 1):
        for j in range(m):
            if not compare(lines[i + j], old_lines[j]):
                break
        else:
            return i
    return None


def apply_hunks(
    original: str, hunks: list[Hunk], *, path: str | None = None
) -> str:
    """Apply *hunks* to *original* file text, returning the new text.

    Each hunk is located with the 4-pass fuzzy matcher described in the module
    docstring; the first pass that matches the whole window wins and becomes
    the locked pass for the remaining hunks of this file. Later hunks search
    the updated text and only look at/after the previous hunk's end, so the
    hunks stay in order. Pure insertions (empty ``old_lines``) append at the
    end.

    Raises :class:`PatchError` if a hunk cannot be located. Line endings are
    normalized to ``"\\n"`` and a trailing newline is preserved when *original*
    had one.
    """
    if not hunks:
        return original

    lines = original.splitlines()
    had_trailing = original.endswith("\n")
    strategy: int | None = None
    search_start = 0

    for hunk in hunks:
        if not hunk.old_lines:
            lines.extend(hunk.new_lines)
            search_start = len(lines)
            continue
        window = len(hunk.old_lines)
        pass_range = range(4) if strategy is None else (strategy,)
        start: int | None = None
        found_by: int | None = None
        for s in pass_range:
            pos = _find_window(lines, hunk.old_lines, s, search_start)
            if pos is not None:
                start, found_by = pos, s
                break
        if start is None:
            where = path or "<file>"
            raise PatchError(
                f"Could not locate hunk in {where}: first old line "
                f"{hunk.old_lines[0]!r}"
            )
        if strategy is None:
            strategy = found_by
        lines[start:start + window] = list(hunk.new_lines)
        search_start = start + len(hunk.new_lines)

    result = "\n".join(lines)
    if had_trailing:
        result += "\n"
    return result


# ---------------------------------------------------------------------------
# Callback adapter
# ---------------------------------------------------------------------------

def _join_lines_with_trailing_newline(content: list[str]) -> str:
    return "\n".join(content) + "\n"


def _summary(counts: dict[str, int]) -> str:
    parts = []
    if counts["update"]:
        parts.append(f"{counts['update']} updated")
    if counts["add"]:
        parts.append(f"{counts['add']} added")
    if counts["delete"]:
        parts.append(f"{counts['delete']} deleted")
    if not parts:
        return "Applied: 0 files changed"
    return "Applied: " + ", ".join(parts)


def apply_patch_text(
    text: str,
    read_file: Callable[[str], str],
    write_file: Callable[[str, str], None],
    delete_file: Callable[[str], None],
) -> str:
    """Parse *text* and apply it through the given callbacks.

    ``read_file(path)`` returns the current file text (raise if missing),
    ``write_file(path, content)`` stores it, ``delete_file(path)`` removes it.
    Returns a one-line summary such as ``"Applied: 2 updated, 1 added"``.
    Raises :class:`PatchError` on malformed patches or hunks that cannot be
    located.
    """
    patches = parse_patch(text)
    counts = {"add": 0, "update": 0, "delete": 0}
    for fp in patches:
        if fp.op == "add":
            write_file(fp.path, _join_lines_with_trailing_newline(fp.content))
        elif fp.op == "update":
            try:
                original = read_file(fp.path)
            except Exception as exc:  # noqa: BLE001 - surface as PatchError
                raise PatchError(f"Could not read {fp.path!r}: {exc}") from exc
            updated = apply_hunks(original, fp.hunks, path=fp.path)
            write_file(fp.path, updated)
        elif fp.op == "delete":
            delete_file(fp.path)
        else:
            raise PatchError(f"Unknown operation {fp.op!r} for {fp.path!r}")
        counts[fp.op] += 1
    return _summary(counts)