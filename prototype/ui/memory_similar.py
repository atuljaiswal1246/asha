"""Memory similarity for dedupe — embeddings OPTIONAL and OFF by default.

``memory.py`` ships a tiny lexical near-duplicate check
(``_near_duplicate``) that compares token sets. This module generalises that
idea without touching the memory code:

  * ``similar(a, b)`` — 0..1, dependency-free token-set Jaccard/F1 blend by
    default (comparable to ``memory.py``'s behaviour).
  * ``near_duplicate(text, corpus, threshold=0.75)`` — pure, non-mutating
    scan mirroring the 0.75 threshold semantics.
  * ``register_backend`` / ``set_backend`` / ``embed`` — a seam where a real
    embedding model can be plugged in later. With no backend active the
    lexical backend is used.

IMPORTANT: this file is inert — nothing imports it yet. To opt in later, a
caller imports ``memory_similar`` and (optionally) registers an embedding
backend; the default path needs nothing but the standard library. No
embedding library is imported and no network call is ever made here.
"""

from __future__ import annotations

import math
import re
from typing import Callable, Iterable, Mapping, Optional, Sequence

DEFAULT_THRESHOLD = 0.75

_TOKEN_RE = re.compile(r"[a-z0-9]+")

EmbeddingBackend = Callable[[str], "Sequence[float] | Mapping[str, float]"]

_EMBED_BACKENDS: dict[str, EmbeddingBackend] = {}
_active_backend: Optional[str] = None


def _tokens(text: str) -> set[str]:
    """Normalised word set: lowercased, punctuation stripped."""
    return set(_TOKEN_RE.findall((text or "").lower()))


def lexical_embed(text: str) -> dict[str, float]:
    """Built-in, dependency-free embedding: one weight per normalised token."""
    return {token: 1.0 for token in _tokens(text)}


# The built-in backend is always available under the name "lexical".
_EMBED_BACKENDS["lexical"] = lexical_embed


def register_backend(name: str, callable_: EmbeddingBackend) -> None:
    """Register an embedding backend under ``name`` (callable: text → vector)."""
    if not name:
        raise ValueError("backend name must be non-empty")
    if not callable(callable_):
        raise TypeError("backend must be callable")
    _EMBED_BACKENDS[name] = callable_


def set_backend(name: Optional[str]) -> Optional[str]:
    """Activate a registered backend (``None`` → built-in lexical path).

    Returns the previously active backend name (or ``None``). Raises
    ``KeyError`` if ``name`` is unknown.
    """
    global _active_backend
    if name is None or name == "lexical":
        previous = _active_backend
        _active_backend = None
        return previous
    if name not in _EMBED_BACKENDS:
        raise KeyError(f"unknown embedding backend: {name!r}")
    previous = _active_backend
    _active_backend = name
    return previous


def active_backend() -> Optional[str]:
    return _active_backend


def embed(text: str):
    """Embed ``text`` with the active backend, else the built-in lexical one.

    Returns whatever the backend returns: a numeric sequence or a mapping.
    A real backend would do e.g. ``register_backend("mini", model.encode)``
    then ``set_backend("mini")``; ``similar`` then compares those vectors.
    """
    if _active_backend is None:
        return lexical_embed(text)
    return _EMBED_BACKENDS[_active_backend](text)


def _cosine(left, right) -> float:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        shared = set(left) & set(right)
        dot = sum(float(left[k]) * float(right[k]) for k in shared)
        left_norm = math.sqrt(sum(float(v) * float(v) for v in left.values()))
        right_norm = math.sqrt(sum(float(v) * float(v) for v in right.values()))
    else:
        left_seq = [float(x) for x in left]
        right_seq = [float(x) for x in right]
        if len(left_seq) != len(right_seq):
            raise ValueError("embedding vectors have different dimensions")
        dot = sum(a * b for a, b in zip(left_seq, right_seq))
        left_norm = math.sqrt(sum(a * a for a in left_seq))
        right_norm = math.sqrt(sum(b * b for b in right_seq))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


def _lexical_similar(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    intersection = len(ta & tb)
    if intersection == 0:
        return 0.0
    union = len(ta | tb)
    jaccard = intersection / union
    precision = intersection / len(ta)
    recall = intersection / len(tb)
    f1 = 2 * precision * recall / (precision + recall)
    return (jaccard + f1) / 2.0


def similar(a: str, b: str) -> float:
    """Similarity in 0..1 between two texts.

    Default (no backend active): token-set Jaccard/F1 blend. With a backend
    active: cosine similarity of the two embeddings.
    """
    if _active_backend is not None:
        return _cosine(embed(a), embed(b))
    return _lexical_similar(a, b)


def near_duplicate(
    text: str,
    corpus: Iterable[str],
    threshold: float = DEFAULT_THRESHOLD,
) -> Optional[tuple[int, float]]:
    """First ``(index, score)`` in ``corpus`` at or above ``threshold``.

    Pure and non-mutating — unlike ``memory.py``'s store method, it never
    writes anything. Mirrors the default 0.75 threshold used there.
    """
    for index, candidate in enumerate(corpus):
        score = similar(text, candidate)
        if score >= threshold:
            return index, score
    return None
