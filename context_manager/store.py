"""ContextStore — SQLite-backed per-session conversation store.

Schema is intentionally a compatible subset of Hermes's `messages` table so a
HermesMemoryBackend can mirror rows without lossy translation.

Session key is opaque text — callers (e.g. agent-dispatcher) compose it from
`(chat_id, message_thread_id)` for topic-aware DM fallback.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional, Union


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    user_id       TEXT,
    title         TEXT,
    metadata      TEXT,
    started_at    REAL NOT NULL,
    ended_at      REAL,
    message_count INTEGER NOT NULL DEFAULT 0,
    summary       TEXT,
    summary_updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(id),
    role         TEXT NOT NULL,
    content      TEXT,
    tool_name    TEXT,
    tool_calls   TEXT,
    tool_call_id TEXT,
    timestamp    REAL NOT NULL,
    metadata     TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version(version) VALUES (1);
"""


@dataclass
class Message:
    role: str
    content: Optional[str] = None
    tool_name: Optional[str] = None
    tool_calls: Optional[Any] = None
    tool_call_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    metadata: Optional[dict] = None
    id: Optional[int] = None

    def to_openai(self) -> dict:
        """Render this message in OpenAI chat-completions schema."""
        msg: dict = {"role": self.role}
        if self.content is not None:
            msg["content"] = self.content
        if self.tool_calls:
            msg["tool_calls"] = (
                json.loads(self.tool_calls)
                if isinstance(self.tool_calls, str)
                else self.tool_calls
            )
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        if self.tool_name and self.role == "tool":
            msg["name"] = self.tool_name
        return msg


class ContextStore:
    """SQLite-backed conversation store.

    Thread-safe via a single internal lock; assumes a small number of writers
    (typical dispatcher load is one message per chat at a time).
    """

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        """Idempotently add soft-delete columns used by pop_last_n / rewind."""
        with self._lock:
            cols = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            for name, decl in (
                ("dropped_at", "REAL"),
                ("dropped_by", "TEXT"),
                ("drop_batch_id", "TEXT"),
            ):
                if name not in cols:
                    self._conn.execute(
                        f"ALTER TABLE messages ADD COLUMN {name} {decl}"
                    )

    # ---------- sessions ----------
    def ensure_session(
        self,
        session_id: str,
        source: str = "dispatcher",
        user_id: Optional[str] = None,
        title: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO sessions
                   (id, source, user_id, title, metadata, started_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    source,
                    user_id,
                    title,
                    json.dumps(metadata) if metadata else None,
                    time.time(),
                ),
            )

    def list_sessions(self, source: Optional[str] = None) -> List[dict]:
        with self._lock:
            if source:
                rows = self._conn.execute(
                    "SELECT id, source, user_id, title, started_at, message_count "
                    "FROM sessions WHERE source = ? ORDER BY started_at DESC",
                    (source,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, source, user_id, title, started_at, message_count "
                    "FROM sessions ORDER BY started_at DESC"
                ).fetchall()
        return [
            {
                "id": r[0],
                "source": r[1],
                "user_id": r[2],
                "title": r[3],
                "started_at": r[4],
                "message_count": r[5],
            }
            for r in rows
        ]

    # ---------- messages ----------
    def append(
        self,
        session_id: str,
        role: str,
        content: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_calls: Optional[Any] = None,
        tool_call_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        """Append a message; auto-creates the session if missing. Returns row id.

        INSERT(messages) + UPDATE(sessions.message_count) are wrapped in a
        single transaction to keep the counter in sync with reality.
        """
        tool_calls_json = (
            json.dumps(tool_calls)
            if tool_calls is not None and not isinstance(tool_calls, str)
            else tool_calls
        )
        metadata_json = json.dumps(metadata) if metadata else None
        with self._lock:
            # ensure_session inline + INSERT + UPDATE under one BEGIN.
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    """INSERT OR IGNORE INTO sessions
                       (id, source, started_at) VALUES (?, ?, ?)""",
                    (session_id, "dispatcher", time.time()),
                )
                cur = self._conn.execute(
                    """INSERT INTO messages
                       (session_id, role, content, tool_name, tool_calls,
                        tool_call_id, timestamp, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        role,
                        content,
                        tool_name,
                        tool_calls_json,
                        tool_call_id,
                        time.time(),
                        metadata_json,
                    ),
                )
                self._conn.execute(
                    "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                    (session_id,),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return int(cur.lastrowid or 0)

    def pop_last_n(self, session_id: str, n: int) -> int:
        """Soft-delete the last `n` non-dropped messages for `session_id`.

        Marks rows with ``dropped_at=now``, ``dropped_by='rewind'`` and a fresh
        shared ``drop_batch_id``. Also decrements ``sessions.message_count`` by
        the number of rows actually flipped, all in a single transaction.

        Returns the number of rows soft-deleted (``0`` if nothing to pop).
        """
        if n <= 0:
            return 0
        batch_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                rows = self._conn.execute(
                    """SELECT id FROM messages
                       WHERE session_id = ? AND dropped_at IS NULL
                       ORDER BY id DESC LIMIT ?""",
                    (session_id, n),
                ).fetchall()
                if not rows:
                    self._conn.execute("COMMIT")
                    return 0
                ids = [r[0] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"""UPDATE messages
                        SET dropped_at = ?, dropped_by = 'rewind', drop_batch_id = ?
                        WHERE id IN ({placeholders})""",
                    (now, batch_id, *ids),
                )
                flipped = len(ids)
                self._conn.execute(
                    """UPDATE sessions
                       SET message_count = MAX(0, message_count - ?)
                       WHERE id = ?""",
                    (flipped, session_id),
                )
                self._conn.execute("COMMIT")
                return flipped
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def reset(self, session_id: str, *, reason: Optional[str] = None) -> int:
        """Soft-delete ALL live (non-dropped) messages for session_id.

        Marks rows with dropped_at=now, dropped_by='reset', shared drop_batch_id.
        Zeros sessions.message_count and clears any cached summary. Appends an
        entry to sessions.metadata['reset_history'] (capped at 10 entries).
        Auto-creates the session row if missing (mirrors append()).
        Idempotent: a second call with nothing live returns 0.

        Concurrency: within-process serialized by self._lock. Cross-process
        contention surfaces as sqlite3.OperationalError (BUSY); caller retries.

        Returns count of rows soft-deleted.
        """
        batch_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO sessions (id, source, started_at) "
                    "VALUES (?, ?, ?)",
                    (session_id, "dispatcher", now),
                )
                cur = self._conn.execute(
                    "UPDATE messages SET dropped_at=?, dropped_by='reset', "
                    "drop_batch_id=? WHERE session_id=? AND dropped_at IS NULL",
                    (now, batch_id, session_id),
                )
                flipped = cur.rowcount or 0
                if flipped:
                    self._conn.execute(
                        "UPDATE sessions SET message_count = 0, "
                        "summary = NULL, summary_updated_at = NULL WHERE id = ?",
                        (session_id,),
                    )
                if flipped or reason is not None:
                    row = self._conn.execute(
                        "SELECT metadata FROM sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                    meta: dict = {}
                    if row and row[0]:
                        try:
                            meta = json.loads(row[0]) or {}
                        except Exception:
                            meta = {}
                    history = meta.get("reset_history") or []
                    history.append({
                        "at": now,
                        "batch_id": batch_id,
                        "reason": reason,
                        "count": flipped,
                    })
                    meta["reset_history"] = history[-10:]
                    self._conn.execute(
                        "UPDATE sessions SET metadata = ? WHERE id = ?",
                        (json.dumps(meta), session_id),
                    )
                self._conn.execute("COMMIT")
                return flipped
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_recent(self, session_id: str, limit: int = 50) -> List[Message]:
        """Return up to `limit` most recent messages in chronological order."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, role, content, tool_name, tool_calls, tool_call_id,
                          timestamp, metadata
                   FROM messages WHERE session_id = ? AND dropped_at IS NULL
                   ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        msgs = [
            Message(
                id=r[0],
                role=r[1],
                content=r[2],
                tool_name=r[3],
                tool_calls=r[4],
                tool_call_id=r[5],
                timestamp=r[6],
                metadata=json.loads(r[7]) if r[7] else None,
            )
            for r in rows
        ]
        msgs.reverse()
        return msgs

    def get_all(self, session_id: str) -> List[Message]:
        return self.get_recent(session_id, limit=10**9)

    def get_metadata(self, session_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if not row or not row[0]:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def set_metadata(self, session_id: str, metadata: dict) -> None:
        self.ensure_session(session_id)
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET metadata = ? WHERE id = ?",
                (json.dumps(metadata), session_id),
            )

    def update_metadata(self, session_id: str, **patch: Any) -> dict:
        """Atomic shallow-merge of `patch` into existing metadata. Returns full new metadata."""
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO sessions
                   (id, source, started_at) VALUES (?, ?, ?)""",
                (session_id, "dispatcher", time.time()),
            )
            row = self._conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            cur: dict = {}
            if row and row[0]:
                try:
                    cur = json.loads(row[0]) or {}
                except Exception:
                    cur = {}
            cur.update(patch)
            self._conn.execute(
                "UPDATE sessions SET metadata = ? WHERE id = ?",
                (json.dumps(cur), session_id),
            )
            return cur

    def get_summary(self, session_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT summary FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return row[0] if row else None

    def set_summary(self, session_id: str, summary: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET summary = ?, summary_updated_at = ? WHERE id = ?",
                (summary, time.time(), session_id),
            )

    def get_full_for_compaction(self, session_id: str) -> List[Message]:
        """Snapshot for the Compactor to score/summarize. Same as get_all today."""
        return self.get_all(session_id)

    def assemble_context(
        self,
        session_id: str,
        recent_n: int = 30,
        include_summary: bool = True,
    ) -> List[dict]:
        """Build a ready-to-send OpenAI-style message list.

        Strategy: optional summary prepended as a system note, then last N turns.
        """
        out: List[dict] = []
        if include_summary:
            s = self.get_summary(session_id)
            if s:
                out.append({"role": "system", "content": f"[conversation summary]\n{s}"})
        out.extend(m.to_openai() for m in self.get_recent(session_id, recent_n))
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "ContextStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
