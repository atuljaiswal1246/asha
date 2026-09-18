"""Neha's recall layer (M2b-lite, roadmap #3).

Two recall sources, one answer:
  - knowledge: FTS5 index over prototype/data/knowledge/*.md (source-of-truth library)
  - sessions:  thin wrapper over SessionStore (event-sourced log, hermes #17-18)
``recall()`` merges both, knowledge first, each hit tagged ``source``.

Pure stdlib (sqlite3/re/pathlib) — no pipecat imports; standalone testable and
integrated from the bot loop later without touching server.py.
"""

import re
import sqlite3
from pathlib import Path

from sessions import SessionStore

ROOT = Path(__file__).resolve().parent
DEFAULT_KNOWLEDGE_DIR = ROOT.parent / "data" / "knowledge"
DEFAULT_INDEX_DB = ROOT.parent / "data" / "knowledge.db"

_KNOWN_MOD = 12  # snippet window length in tokens

_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_docs (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    key   TEXT NOT NULL UNIQUE,
    path  TEXT NOT NULL,
    title TEXT NOT NULL,
    text  TEXT NOT NULL,
    mtime REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    text,
    prefix='2 3',
    tokenize='unicode61',
    content='knowledge_docs',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge_docs BEGIN
    INSERT INTO knowledge_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge_docs BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge_docs BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO knowledge_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _fts_terms(query: str) -> list[str]:
    """Split a query into safe FTS5 term tokens (unicode61 separators handled)."""
    return _WORD_RE.findall(query or "")


class Recaller:
    """Combined knowledge + session recall for Neha."""

    def __init__(
        self,
        knowledge_dir=DEFAULT_KNOWLEDGE_DIR,
        index_db=DEFAULT_INDEX_DB,
        session_store=None,
    ):
        self._knowledge_dir = Path(knowledge_dir)
        self._index_db = Path(index_db)
        self._session_store = session_store if session_store is not None else SessionStore()
        self._conn = None

    def _ensure(self):
        if self._conn is not None:
            return self._conn
        self._index_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._index_db))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        return conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._session_store is not None:
            self._session_store.close()

    # ---- knowledge corpus ---------------------------------------------------

    def _build_docs(self):
        if not self._knowledge_dir.exists():
            return []
        docs = []
        for path in sorted(self._knowledge_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            title = path.stem
            docs.append((str(path.relative_to(self._knowledge_dir)), title, text, path.stat().st_mtime))
        return docs

    def index(self):
        """Reindex knowledge docs incrementally (by path + mtime)."""
        conn = self._ensure()
        current = self._build_docs()
        known = {
            row["key"]: row["mtime"]
            for row in conn.execute("SELECT key, mtime FROM knowledge_docs").fetchall()
        }
        for rel, title, text, mtime in current:
            if mtime == known.get(rel):
                continue
            conn.execute(
                """
                INSERT INTO knowledge_docs (key, path, title, text, mtime) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    path=excluded.path, title=excluded.title, text=excluded.text, mtime=excluded.mtime
                """,
                (rel, rel, title, text, mtime),
            )
        for rel in known:
            if all(rel != entry[0] for entry in current):
                conn.execute("DELETE FROM knowledge_docs WHERE key = ?", (rel,))
        conn.commit()
        return len(current)

    def search_knowledge(self, query: str, limit: int = 10):
        """Ranked knowledge hits (best bm25 first): file + plain-text snippet."""
        conn = self._ensure()
        query = (query or "").strip()
        terms = _fts_terms(query)
        if not terms:
            return []
        self.index()
        match_all = " OR ".join('"%s"' % t for t in terms)
        rows = conn.execute(
            """
            SELECT d.path          AS file,
                   d.title         AS title,
                   d.text          AS text,
                   f.rank          AS rank,
                   snippet(knowledge_fts, 0, '[[', ']]', ' … ', %d) AS raw_snippet
            FROM knowledge_fts f
            JOIN knowledge_docs d ON d.id = f.rowid
            WHERE knowledge_fts MATCH ?
            ORDER BY f.rank ASC
            LIMIT ?
            """ % _KNOWN_MOD,
            (match_all, max(1, min(int(limit), 50))),
        ).fetchall()
        return [
            {
                "source": "knowledge",
                "file": r["file"],
                "title": r["title"],
                "text": r["text"],
                "snippet": r["raw_snippet"].replace("[[", "").replace("]]", ""),
                "rank": r["rank"],
            }
            for r in rows
        ]

    # ---- session history ----------------------------------------------------

    def search_sessions(self, query: str, limit: int = 10):
        """Session-log hits with session refs (wraps SessionStore.search)."""
        hits = self._session_store.search(query, limit)
        for h in hits:
            h["source"] = "session"
            h["snippet"] = h["text"]
        return hits

    # ---- combined ------------------------------------------------------------

    def recall(self, query: str, limit: int = 10):
        """Knowledge first (source-of-truth), then session history — each hit
        tagged ``source``."""
        limit = max(1, min(int(limit), 50))
        hits = self.search_knowledge(query, limit)
        for h in self._session_store.search(query, limit):
            if len(hits) >= limit:
                break
            h["source"] = "session"
            h["snippet"] = h["text"]
            hits.append(h)
        return hits