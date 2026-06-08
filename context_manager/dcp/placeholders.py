"""PlaceholderStore — SQLite table colocated in the ContextStore DB.

The store is immutable: placeholders are additive records that rewrite
the outbound payload at request-build time, never the stored messages.
Deactivating a placeholder reverts to verbatim for that span on the
next outbound build.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dcp_placeholders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- 'range' | 'message'
    span_start      TEXT,                   -- range mode: store message id
    span_end        TEXT,                   -- range mode: store message id
    msg_ids_json    TEXT,                   -- message mode: JSON list of ids
    summary         TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    nested_in_id    INTEGER,                -- FK → dcp_placeholders.id
    created_at      REAL NOT NULL,
    deactivated_at  REAL
);
CREATE INDEX IF NOT EXISTS ix_dcp_ph_session_active
    ON dcp_placeholders(session_id, active);
"""

_NESTED_DELIMITER_TPL = "<!--ctxmgr:nested:{uid} {pos}-->"


@dataclass
class Placeholder:
    id: Optional[int]
    session_id: str
    kind: str           # 'range' | 'message'
    span_start: Optional[str]
    span_end: Optional[str]
    msg_ids: Optional[List[str]]    # message mode
    summary: str
    active: bool
    nested_in_id: Optional[int]
    created_at: float
    deactivated_at: Optional[float]

    def covers(self, msg_id: str) -> bool:
        """Return True if msg_id falls within this placeholder's span."""
        if not self.active:
            return False
        if self.kind == "range" and self.span_start and self.span_end:
            # IDs are store rowids (integers stored as text).
            try:
                return int(self.span_start) <= int(msg_id) <= int(self.span_end)
            except (ValueError, TypeError):
                return self.span_start <= msg_id <= self.span_end
        if self.kind == "message" and self.msg_ids:
            return msg_id in self.msg_ids
        return False


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


class PlaceholderStore:
    """Manages DCP placeholder records in the ContextStore SQLite DB."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        _ensure_schema(conn)

    # ---- write ----

    def add_range(
        self,
        session_id: str,
        span_start: str,
        span_end: str,
        summary: str,
        nested_in_id: Optional[int] = None,
    ) -> Placeholder:
        now = time.time()
        cur = self._conn.execute(
            """INSERT INTO dcp_placeholders
               (session_id, kind, span_start, span_end, summary,
                active, nested_in_id, created_at)
               VALUES (?, 'range', ?, ?, ?, 1, ?, ?)""",
            (session_id, span_start, span_end, summary, nested_in_id, now),
        )
        self._conn.commit()
        row_id = cur.lastrowid
        return Placeholder(
            id=row_id,
            session_id=session_id,
            kind="range",
            span_start=span_start,
            span_end=span_end,
            msg_ids=None,
            summary=summary,
            active=True,
            nested_in_id=nested_in_id,
            created_at=now,
            deactivated_at=None,
        )

    def add_message(
        self,
        session_id: str,
        msg_ids: List[str],
        summary: str,
        nested_in_id: Optional[int] = None,
    ) -> Placeholder:
        now = time.time()
        cur = self._conn.execute(
            """INSERT INTO dcp_placeholders
               (session_id, kind, msg_ids_json, summary,
                active, nested_in_id, created_at)
               VALUES (?, 'message', ?, ?, 1, ?, ?)""",
            (session_id, json.dumps(msg_ids), summary, nested_in_id, now),
        )
        self._conn.commit()
        row_id = cur.lastrowid
        return Placeholder(
            id=row_id,
            session_id=session_id,
            kind="message",
            span_start=None,
            span_end=None,
            msg_ids=msg_ids,
            summary=summary,
            active=True,
            nested_in_id=nested_in_id,
            created_at=now,
            deactivated_at=None,
        )

    def deactivate(self, placeholder_id: int) -> None:
        self._conn.execute(
            "UPDATE dcp_placeholders SET active=0, deactivated_at=? WHERE id=?",
            (time.time(), placeholder_id),
        )
        self._conn.commit()

    def reactivate(self, placeholder_id: int) -> None:
        self._conn.execute(
            "UPDATE dcp_placeholders SET active=1, deactivated_at=NULL WHERE id=?",
            (placeholder_id,),
        )
        self._conn.commit()

    # ---- read ----

    def active_for(self, session_id: str) -> List[Placeholder]:
        """Return all active placeholders for a session, oldest-first."""
        rows = self._conn.execute(
            """SELECT id, kind, span_start, span_end, msg_ids_json, summary,
                      nested_in_id, created_at
               FROM dcp_placeholders
               WHERE session_id = ? AND active = 1
               ORDER BY id ASC""",
            (session_id,),
        ).fetchall()
        return [_row_to_placeholder(row, session_id, active=True) for row in rows]

    def history_for(self, session_id: str, limit: int = 100) -> List[Placeholder]:
        """Return all placeholders (active and inactive), newest-first."""
        rows = self._conn.execute(
            """SELECT id, kind, span_start, span_end, msg_ids_json, summary,
                      nested_in_id, created_at, deactivated_at, active
               FROM dcp_placeholders
               WHERE session_id = ?
               ORDER BY id DESC
               LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        return [_row_to_placeholder_full(row, session_id) for row in rows]

    def count_active(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM dcp_placeholders WHERE session_id=? AND active=1",
            (session_id,),
        ).fetchone()
        return int(row[0]) if row else 0


def _row_to_placeholder(row, session_id: str, active: bool = True) -> Placeholder:
    ph_id, kind, span_start, span_end, msg_ids_json, summary, nested_in_id, created_at = row
    msg_ids = json.loads(msg_ids_json) if msg_ids_json else None
    return Placeholder(
        id=ph_id,
        session_id=session_id,
        kind=kind,
        span_start=span_start,
        span_end=span_end,
        msg_ids=msg_ids,
        summary=summary,
        active=active,
        nested_in_id=nested_in_id,
        created_at=created_at,
        deactivated_at=None,
    )


def _row_to_placeholder_full(row, session_id: str) -> Placeholder:
    (
        ph_id, kind, span_start, span_end, msg_ids_json, summary,
        nested_in_id, created_at, deactivated_at, active,
    ) = row
    msg_ids = json.loads(msg_ids_json) if msg_ids_json else None
    return Placeholder(
        id=ph_id,
        session_id=session_id,
        kind=kind,
        span_start=span_start,
        span_end=span_end,
        msg_ids=msg_ids,
        summary=summary,
        active=bool(active),
        nested_in_id=nested_in_id,
        created_at=created_at,
        deactivated_at=deactivated_at,
    )
