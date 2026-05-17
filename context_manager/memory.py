"""MemoryBackend adapters — pluggable long-term memory.

Hermes is ONE adapter, not a hard requirement. Dispatcher must function with
NoopMemoryBackend if Hermes isn't installed.

Adapters planned: HermesMemoryBackend (today), SqliteMemoryBackend,
Mem0MemoryBackend, HonchoMemoryBackend, SupermemoryMemoryBackend.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, List, Optional, Sequence, Union

from .store import Message

log = logging.getLogger(__name__)


class MemoryBackend(ABC):
    """Pluggable long-term memory adapter."""

    @abstractmethod
    def remember(
        self,
        session_key: str,
        messages: Sequence[Message],
        tags: Optional[dict] = None,
    ) -> None:
        """Persist `messages` to long-term memory under `session_key`."""

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> List[dict]:
        """Return top-K relevant past turns from any session/topic."""

    def close(self) -> None:  # optional
        pass


class NoopMemoryBackend(MemoryBackend):
    """Default backend — does nothing. Always safe."""

    def remember(self, session_key, messages, tags=None):  # noqa: D401
        return

    def search(self, query: str, limit: int = 10) -> List[dict]:
        return []


class HermesMemoryBackend(MemoryBackend):
    """Write turns into Hermes's SQLite session store.

    Inspects only the tables it touches: `sessions` and `messages`.
    Schema reference: hermes-agent/hermes_state.py (SessionDB.append_message).

    NOTE: We use a *separate sqlite connection* to ~/.hermes/state.db. Hermes
    uses WAL mode, so concurrent readers/writers from multiple processes are
    supported. We do NOT touch FTS — the AFTER INSERT triggers on `messages`
    take care of FTS index maintenance.
    """

    def __init__(
        self,
        db_path: Union[str, Path] = "~/.hermes/state.db",
        source: str = "agent-dispatcher",
    ):
        self.db_path = Path(str(db_path)).expanduser()
        self.source = source
        self._lock = threading.Lock()
        if not self.db_path.exists():
            log.warning("Hermes state.db not found at %s; backend will create one on first write", self.db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensured: set = set()

    # ---------- write ----------
    def _ensure_session(self, session_key: str, tags: Optional[dict]) -> None:
        if session_key in self._ensured:
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM sessions WHERE id = ?", (session_key,)
            ).fetchone()
            if row is None:
                # Insert with minimal required columns. Hermes's schema has many
                # nullable columns; we set source + started_at + title only.
                title = None
                if tags:
                    title = tags.get("topic_name") or tags.get("title")
                try:
                    self._conn.execute(
                        """INSERT INTO sessions (id, source, started_at, title)
                           VALUES (?, ?, ?, ?)""",
                        (session_key, self.source, time.time(), title),
                    )
                except sqlite3.IntegrityError:
                    # title unique constraint collision — retry without title
                    self._conn.execute(
                        """INSERT OR IGNORE INTO sessions (id, source, started_at)
                           VALUES (?, ?, ?)""",
                        (session_key, self.source, time.time()),
                    )
            self._ensured.add(session_key)

    def remember(
        self,
        session_key: str,
        messages: Sequence[Message],
        tags: Optional[dict] = None,
    ) -> None:
        if not messages:
            return
        self._ensure_session(session_key, tags)
        with self._lock:
            for m in messages:
                tool_calls_json = (
                    json.dumps(m.tool_calls)
                    if m.tool_calls is not None and not isinstance(m.tool_calls, str)
                    else m.tool_calls
                )
                self._conn.execute(
                    """INSERT INTO messages
                       (session_id, role, content, tool_call_id, tool_calls,
                        tool_name, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_key,
                        m.role,
                        m.content,
                        m.tool_call_id,
                        tool_calls_json,
                        m.tool_name,
                        m.timestamp,
                    ),
                )
            self._conn.execute(
                """UPDATE sessions
                   SET message_count = message_count + ?
                   WHERE id = ?""",
                (len(messages), session_key),
            )

    # ---------- read ----------
    def search(self, query: str, limit: int = 10) -> List[dict]:
        """FTS5 search across Hermes messages. Returns list of {session_id, role, content, timestamp}."""
        with self._lock:
            try:
                rows = self._conn.execute(
                    """SELECT m.session_id, m.role, m.content, m.timestamp
                       FROM messages_fts f
                       JOIN messages m ON m.id = f.rowid
                       WHERE messages_fts MATCH ?
                       ORDER BY m.timestamp DESC
                       LIMIT ?""",
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError as e:
                log.warning("HermesMemoryBackend.search failed: %s", e)
                return []
        return [
            {"session_id": r[0], "role": r[1], "content": r[2], "timestamp": r[3]}
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class MemorySearch:
    """Thin facade over a MemoryBackend for the dispatcher to query."""

    def __init__(self, backend: MemoryBackend):
        self.backend = backend

    def query(self, q: str, limit: int = 10) -> List[dict]:
        return self.backend.search(q, limit=limit)
