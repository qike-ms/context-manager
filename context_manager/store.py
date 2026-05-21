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
from typing import Any, Iterable, List, Literal, Optional, Tuple, Union

from .windows import get_window as _get_window


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


@dataclass
class MessageView:
    """Lightweight per-row projection returned by ``iter_messages``."""

    id: int
    role: str
    kind: str  # 'tool_call' | 'tool_result' | 'text'
    tool_name: Optional[str]
    tool_args_preview: Optional[str]
    text_preview: Optional[str]
    token_estimate: Optional[int]


@dataclass
class TokenUsage:
    """Summary of a session's token footprint."""

    active_tokens: int
    total_seen: Optional[int]
    window_size: int
    window_pct: Optional[float]
    calibrated: bool
    missing_estimates: int


def _classify_kind(role: str, tool_calls: Optional[str], tool_call_id: Optional[str]) -> str:
    if role == "tool" or tool_call_id is not None:
        return "tool_result"
    if tool_calls is not None:
        return "tool_call"
    return "text"


def _safe_preview(text: Optional[str], n: int) -> Optional[str]:
    if text is None:
        return None
    if len(text) <= n:
        return text
    return text[:n]


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
        """Idempotently add columns. v1→v2 adds token_estimate + sessions.model."""
        with self._lock:
            msg_cols = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            for name, decl in (
                ("dropped_at", "REAL"),
                ("dropped_by", "TEXT"),
                ("drop_batch_id", "TEXT"),
                ("token_estimate", "INTEGER"),
            ):
                if name not in msg_cols:
                    self._conn.execute(
                        f"ALTER TABLE messages ADD COLUMN {name} {decl}"
                    )
            sess_cols = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "model" not in sess_cols:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN model TEXT")
            # Backfill token_estimate for existing rows that have content.
            self._backfill_token_estimates()
            # Bump schema_version to 2.
            cur_ver = self._conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()
            if not cur_ver or (cur_ver[0] or 0) < 2:
                self._conn.execute(
                    "INSERT OR IGNORE INTO schema_version(version) VALUES (2)"
                )

    def _backfill_token_estimates(self) -> None:
        """Populate token_estimate for any pre-existing rows where it's NULL.

        Uses the spike4 token_estimator with backend='default'. Idempotent —
        rows already estimated are left alone.
        """
        try:
            from .token_estimator import estimate_tokens
        except Exception:
            return
        rows = self._conn.execute(
            "SELECT id, content, tool_calls FROM messages WHERE token_estimate IS NULL"
        ).fetchall()
        for rid, content, tool_calls in rows:
            text = content or ""
            if tool_calls:
                text = (text + "\n" + tool_calls) if text else tool_calls
            try:
                est = estimate_tokens(text, backend="default", include_overhead=False)
            except Exception:
                est = max(1, len(text) // 4) if text else 0
            self._conn.execute(
                "UPDATE messages SET token_estimate = ? WHERE id = ?",
                (int(est), rid),
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
        # Compute token_estimate at append time (best-effort).
        try:
            from .token_estimator import estimate_tokens
            est_text = content or ""
            if tool_calls_json:
                est_text = (est_text + "\n" + tool_calls_json) if est_text else tool_calls_json
            token_estimate = int(
                estimate_tokens(est_text, backend="default", include_overhead=False)
            )
        except Exception:
            token_estimate = None
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
                        tool_call_id, timestamp, metadata, token_estimate)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        role,
                        content,
                        tool_name,
                        tool_calls_json,
                        tool_call_id,
                        time.time(),
                        metadata_json,
                        token_estimate,
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

    # ---------- listing & drop API (see docs/design/listing-and-drop-api.md) ----------

    def iter_messages(
        self,
        session_id: str,
        kind: Literal["all", "tool", "text"] = "all",
        offset: int = 0,
        limit: int = 20,
    ) -> List[MessageView]:
        """Return a page of MessageViews for a session, oldest-first."""
        if kind not in ("all", "tool", "text"):
            raise ValueError(f"unknown kind={kind!r}")
        if kind == "tool":
            where = (
                "session_id = ? AND dropped_at IS NULL AND ("
                "role = 'tool' OR tool_calls IS NOT NULL "
                "OR tool_name IS NOT NULL OR tool_call_id IS NOT NULL)"
            )
        elif kind == "text":
            where = (
                "session_id = ? AND dropped_at IS NULL AND NOT ("
                "role = 'tool' OR tool_calls IS NOT NULL "
                "OR tool_name IS NOT NULL OR tool_call_id IS NOT NULL)"
            )
        else:
            where = "session_id = ? AND dropped_at IS NULL"
        sql = (
            "SELECT id, role, content, tool_name, tool_calls, tool_call_id, "
            "token_estimate FROM messages "
            f"WHERE {where} ORDER BY id ASC LIMIT ? OFFSET ?"
        )
        with self._lock:
            rows = self._conn.execute(
                sql, (session_id, int(limit), int(offset))
            ).fetchall()
        out: List[MessageView] = []
        for rid, role, content, tname, tcalls, tcid, tok in rows:
            args_preview: Optional[str] = None
            if tcalls:
                try:
                    parsed = json.loads(tcalls) if isinstance(tcalls, str) else tcalls
                    # Try common shape [{function:{arguments: "..."}}]
                    args = None
                    if isinstance(parsed, list) and parsed:
                        first = parsed[0]
                        if isinstance(first, dict):
                            fn = first.get("function") or {}
                            args = fn.get("arguments") if isinstance(fn, dict) else None
                    args_preview = _safe_preview(
                        args if isinstance(args, str) else json.dumps(parsed), 80
                    )
                except Exception:
                    args_preview = _safe_preview(str(tcalls), 80)
            out.append(
                MessageView(
                    id=rid,
                    role=role,
                    kind=_classify_kind(role, tcalls, tcid),
                    tool_name=tname,
                    tool_args_preview=args_preview,
                    text_preview=_safe_preview(content, 120),
                    token_estimate=tok,
                )
            )
        return out

    def token_usage(
        self,
        session_id: str,
        model: Optional[str] = None,
    ) -> TokenUsage:
        """Summarize the session's current token footprint. See design §2.1."""
        with self._lock:
            row_live = self._conn.execute(
                "SELECT COALESCE(SUM(token_estimate), 0), "
                "SUM(CASE WHEN token_estimate IS NULL THEN 1 ELSE 0 END), "
                "COUNT(*) "
                "FROM messages WHERE session_id = ? AND dropped_at IS NULL",
                (session_id,),
            ).fetchone()
            row_all = self._conn.execute(
                "SELECT COALESCE(SUM(token_estimate), 0), COUNT(*) "
                "FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            sess_model_row = self._conn.execute(
                "SELECT model FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        active_tokens = int(row_live[0] or 0)
        missing = int(row_live[1] or 0)
        live_count = int(row_live[2] or 0)
        total_seen_sum = int(row_all[0] or 0)
        total_rows = int(row_all[1] or 0)
        total_seen: Optional[int] = total_seen_sum if total_rows > 0 else None
        resolved_model = model or (sess_model_row[0] if sess_model_row else None)
        window_size, known = _get_window(resolved_model)
        window_pct = (active_tokens / window_size) if known and window_size else None
        calibrated = live_count > 0 and missing == 0
        return TokenUsage(
            active_tokens=active_tokens,
            total_seen=total_seen,
            window_size=window_size,
            window_pct=window_pct,
            calibrated=calibrated,
            missing_estimates=missing,
        )

    def _hard_delete(self, session_id: str, where_clause: str, params: Tuple) -> int:
        """Common hard-delete path. Returns rows actually deleted.

        Decrements sessions.message_count by the count of LIVE rows
        (dropped_at IS NULL) that matched. Soft-dropped matches are also
        physically removed but do NOT decrement the counter (already
        subtracted by pop_last_n).
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                live_n = self._conn.execute(
                    f"SELECT COUNT(*) FROM messages WHERE session_id = ? "
                    f"AND dropped_at IS NULL AND ({where_clause})",
                    (session_id, *params),
                ).fetchone()[0]
                cur = self._conn.execute(
                    f"DELETE FROM messages WHERE session_id = ? AND ({where_clause})",
                    (session_id, *params),
                )
                deleted = int(cur.rowcount or 0)
                if live_n:
                    self._conn.execute(
                        "UPDATE sessions "
                        "SET message_count = MAX(0, message_count - ?), "
                        "summary = NULL, summary_updated_at = NULL "
                        "WHERE id = ?",
                        (int(live_n), session_id),
                    )
                self._conn.execute("COMMIT")
                return deleted
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def drop_messages(self, session_id: str, msg_ids: List[int]) -> int:
        """Hard DELETE the named rows. See design §2.1."""
        if not msg_ids:
            return 0
        ids = [int(i) for i in msg_ids]
        placeholders = ",".join("?" for _ in ids)
        return self._hard_delete(
            session_id, f"id IN ({placeholders})", tuple(ids)
        )

    def drop_by_tool(self, session_id: str, tool_name: str) -> int:
        """Hard DELETE every row in the session whose tool_name == tool_name."""
        return self._hard_delete(
            session_id, "tool_name = ?", (tool_name,)
        )

    def drop_range(self, session_id: str, from_id: int, to_id: int) -> int:
        """Hard DELETE every row in [from_id, to_id] INCLUSIVE in the session."""
        if from_id > to_id:
            return 0
        return self._hard_delete(
            session_id, "id BETWEEN ? AND ?", (int(from_id), int(to_id))
        )

    def set_model(self, session_id: str, model: Optional[str]) -> None:
        """Set the model id associated with a session (used by token_usage)."""
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO sessions
                   (id, source, started_at) VALUES (?, ?, ?)""",
                (session_id, "dispatcher", time.time()),
            )
            self._conn.execute(
                "UPDATE sessions SET model = ? WHERE id = ?",
                (model, session_id),
            )

    def __enter__(self) -> "ContextStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
