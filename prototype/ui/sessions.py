"""Session persistence store for Jarvis (M2b).

Event-sourced session log (dsh-adoption #1-2): ``messages`` is the append-only
source of truth; the FTS5 index is derived from it for cross-session recall
(hermes-adoption #17-18, memory-design @1.5 / @2.2). English-only unicode61
tokenizer. Pure stdlib module — no pipecat imports so it runs standalone and
can be wired into the bot loop later.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id      TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    meta    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    time       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    prefix='2 3',
    tokenize='unicode61',
    content='messages',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class SessionStore:
    """SQLite-backed session log + FTS5 search over message text."""

    def __init__(self, db_path=DEFAULT_DB):
        self._db_path = Path(db_path)
        self._conn = None

    def _ensure(self):
        if self._conn is not None:
            return self._conn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        return conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def new_session(self, meta=None) -> str:
        """Create a session row and return its id."""
        conn = self._ensure()
        session_id = uuid.uuid4().hex
        if meta is not None and not isinstance(meta, str):
            meta = json.dumps(meta)
        conn.execute(
            "INSERT INTO sessions (id, created, meta) VALUES (?, ?, ?)",
            (session_id, _now(), meta or "{}"),
        )
        conn.commit()
        return session_id

    def append(self, session_id: str, role: str, text: str):
        """Append a message to a session. ``seq`` is assigned in order."""
        conn = self._ensure()
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO messages (session_id, seq, role, text, time) VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, role, text, _now()),
        )
        conn.commit()
        return seq

    def search(self, query: str, limit: int = 10):
        """Full-text search over messages. Returns the best matching messages
        (lowest bm25 rank first) with their session refs."""
        conn = self._ensure()
        query = (query or "").strip()
        if not query:
            return []
        phrase = '"' + query.replace('"', '""') + '"'
        rows = conn.execute(
            """
            SELECT m.id            AS message_id,
                   m.session_id    AS session_id,
                   m.seq           AS seq,
                   m.role          AS role,
                   m.text          AS text,
                   m.time          AS time,
                   f.rank          AS rank
            FROM messages_fts f
            JOIN messages m ON m.id = f.rowid
            WHERE messages_fts MATCH ?
            ORDER BY f.rank ASC
            LIMIT ?
            """,
            (phrase, max(1, min(int(limit), 50))),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_sessions(self, limit: int = 50):
        """Most recently active sessions first, with message counts and a title
        derived from the first user message of each session."""
        conn = self._ensure()
        rows = conn.execute(
            """
            SELECT s.id    AS session_id,
                   s.created AS created,
                   s.meta    AS meta,
                   (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count,
                   (SELECT MAX(m.time) FROM messages m WHERE m.session_id = s.id) AS last_time,
                   (SELECT m.text FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user'
                     ORDER BY m.seq ASC LIMIT 1) AS first_user_text
            FROM sessions s
            ORDER BY (SELECT MAX(m.time) FROM messages m WHERE m.session_id = s.id) DESC, s.created DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [dict(r) for r in rows]

    def messages(self, session_id: str, limit: int = 500):
        """Full message log for one session, oldest first."""
        conn = self._ensure()
        rows = conn.execute(
            """
            SELECT id, session_id, seq, role, text, time
            FROM messages
            WHERE session_id = ?
            ORDER BY seq ASC
            LIMIT ?
            """,
            (session_id, max(1, min(int(limit), 2000))),
        ).fetchall()
        return [dict(r) for r in rows]

    def session_title(self, session_id: str, fallback: str = "Chat") -> str:
        """First user message of a session, truncated, or the fallback."""
        conn = self._ensure()
        row = conn.execute(
            """
            SELECT text FROM messages
            WHERE session_id = ? AND role = 'user'
            ORDER BY seq ASC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if not row or not (row[0] or "").strip():
            return fallback
        title = row[0].strip().replace("\n", " ")
        return title[:60] + ("…" if len(title) > 60 else "")