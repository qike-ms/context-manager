"""Hermetic tests for the listing & drop API (design §8)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from context_manager import ContextStore, MessageView, TokenUsage
from context_manager.windows import get_window


SID = "s1"


def _store(tmp_path):
    return ContextStore(tmp_path / "ctx.db")


def _seed_mixed(s, session_id=SID):
    ids = []
    ids.append(s.append(session_id, "system", content="you are helpful"))
    ids.append(s.append(session_id, "user", content="hello"))
    ids.append(s.append(session_id, "assistant", content="hi back"))
    ids.append(
        s.append(
            session_id,
            "assistant",
            tool_calls=[{"id": "c1", "function": {"name": "search", "arguments": '{"q":"x"}'}}],
        )
    )
    ids.append(
        s.append(session_id, "tool", content="result1", tool_name="search", tool_call_id="c1")
    )
    ids.append(s.append(session_id, "user", content="thanks"))
    return ids


# -------- iter_messages --------


def test_iter_messages_all(tmp_path):
    s = _store(tmp_path)
    _seed_mixed(s)
    rows = s.iter_messages(SID, kind="all", offset=0, limit=20)
    assert len(rows) == 6
    assert all(isinstance(r, MessageView) for r in rows)
    assert [r.id for r in rows] == sorted(r.id for r in rows)
    # Pagination
    page1 = s.iter_messages(SID, limit=2)
    page2 = s.iter_messages(SID, offset=2, limit=2)
    assert [r.id for r in page1] == [rows[0].id, rows[1].id]
    assert [r.id for r in page2] == [rows[2].id, rows[3].id]


def test_iter_messages_kind_filter(tmp_path):
    s = _store(tmp_path)
    _seed_mixed(s)
    tool_rows = s.iter_messages(SID, kind="tool")
    text_rows = s.iter_messages(SID, kind="text")
    kinds = {r.kind for r in tool_rows}
    assert "tool_result" in kinds and "tool_call" in kinds
    # System row lands in text bucket
    assert any(r.role == "system" for r in text_rows)
    assert all(r.role != "tool" and r.tool_name is None for r in text_rows)


def test_iter_messages_empty_session(tmp_path):
    s = _store(tmp_path)
    assert s.iter_messages("nope") == []


def test_iter_messages_offset_past_end(tmp_path):
    s = _store(tmp_path)
    _seed_mixed(s)
    assert s.iter_messages(SID, offset=999) == []


# -------- token_usage --------


def test_token_usage_known_model(tmp_path):
    s = _store(tmp_path)
    _seed_mixed(s)
    s.set_model(SID, "opus-4.7")
    u = s.token_usage(SID)
    assert isinstance(u, TokenUsage)
    assert u.window_size == 200_000
    assert u.window_pct is not None
    assert u.calibrated is True
    assert u.missing_estimates == 0


def test_token_usage_uncalibrated(tmp_path):
    s = _store(tmp_path)
    _seed_mixed(s)
    # Force one estimate to NULL
    s._conn.execute(
        "UPDATE messages SET token_estimate = NULL WHERE session_id = ? LIMIT 1",
        (SID,),
    ) if False else s._conn.execute(  # SQLite usually lacks UPDATE...LIMIT
        "UPDATE messages SET token_estimate = NULL WHERE id = (SELECT MIN(id) FROM messages WHERE session_id=?)",
        (SID,),
    )
    s.set_model(SID, "opus-4.7")
    u = s.token_usage(SID)
    assert u.missing_estimates >= 1
    assert u.calibrated is False
    assert u.window_pct is not None  # still computed


def test_token_usage_unknown_model(tmp_path):
    s = _store(tmp_path)
    _seed_mixed(s)
    u = s.token_usage(SID, model="totally-fake-model")
    assert u.window_pct is None
    assert u.window_size == 128_000  # default


def test_token_usage_fallback_to_session_model(tmp_path):
    s = _store(tmp_path)
    _seed_mixed(s)
    s.set_model(SID, "gpt-4o")
    u = s.token_usage(SID)  # no explicit model arg
    assert u.window_size == 128_000
    assert u.window_pct is not None


# -------- drop_messages --------


def test_drop_messages_basic(tmp_path):
    s = _store(tmp_path)
    ids = _seed_mixed(s)
    before = s._conn.execute(
        "SELECT message_count FROM sessions WHERE id=?", (SID,)
    ).fetchone()[0]
    n = s.drop_messages(SID, [ids[0], ids[1]])
    after = s._conn.execute(
        "SELECT message_count FROM sessions WHERE id=?", (SID,)
    ).fetchone()[0]
    assert n == 2
    assert after == before - 2
    assert all(r.id not in (ids[0], ids[1]) for r in s.iter_messages(SID))


def test_drop_messages_unknown_id(tmp_path):
    s = _store(tmp_path)
    ids = _seed_mixed(s)
    n = s.drop_messages(SID, [ids[0], 999_999])
    assert n == 1


def test_drop_messages_empty_list(tmp_path):
    s = _store(tmp_path)
    _seed_mixed(s)
    assert s.drop_messages(SID, []) == 0


def test_drop_messages_cross_session_isolation(tmp_path):
    s = _store(tmp_path)
    a_ids = _seed_mixed(s, session_id="A")
    b_ids = _seed_mixed(s, session_id="B")
    n = s.drop_messages("A", b_ids)  # all belong to B → 0
    assert n == 0
    assert len(s.iter_messages("B", limit=100)) == len(b_ids)


def test_drop_messages_coexist_with_soft_dropped(tmp_path):
    s = _store(tmp_path)
    ids = _seed_mixed(s)
    # Soft-drop last 2 via pop_last_n
    s.pop_last_n(SID, 2)
    mc_after_pop = s._conn.execute(
        "SELECT message_count FROM sessions WHERE id=?", (SID,)
    ).fetchone()[0]
    # Hard-delete one of those soft-dropped rows
    n = s.drop_messages(SID, [ids[-1]])
    assert n == 1  # physically removed
    mc_after_drop = s._conn.execute(
        "SELECT message_count FROM sessions WHERE id=?", (SID,)
    ).fetchone()[0]
    assert mc_after_drop == mc_after_pop  # no double-decrement


def test_drop_messages_decrements_message_count(tmp_path):
    s = _store(tmp_path)
    ids = _seed_mixed(s)
    s.drop_messages(SID, [ids[0]])
    mc = s._conn.execute(
        "SELECT message_count FROM sessions WHERE id=?", (SID,)
    ).fetchone()[0]
    assert mc == len(ids) - 1


def test_hard_drop_clears_cached_summary(tmp_path):
    s = _store(tmp_path)
    ids = _seed_mixed(s)
    s.set_summary(SID, "summary mentioning hello", through_message_id=ids[0])
    before_revision = s._conn.execute(
        "SELECT summary_revision FROM sessions WHERE id=?", (SID,)
    ).fetchone()[0]

    assert s.get_summary(SID) == "summary mentioning hello"
    assert s.drop_messages(SID, [ids[1]]) == 1

    row = s._conn.execute(
        "SELECT summary, summary_envelope, summary_updated_at, "
        "summary_through_message_id, summary_revision "
        "FROM sessions WHERE id=?",
        (SID,),
    ).fetchone()
    assert row[:4] == (None, None, None, None)
    assert row[4] == before_revision + 1
    events = s.iter_events(SID)
    assert events[-2].event_type == "summary_invalidated"
    assert events[-2].metadata["prior_watermark"] == ids[0]
    assert events[-2].metadata["prior_revision"] == before_revision
    assert all(
        "summary mentioning hello" not in (m.get("content") or "")
        for m in s.assemble_context(SID)
    )


def test_hard_drop_of_soft_deleted_row_clears_cached_summary(tmp_path):
    s = _store(tmp_path)
    ids = _seed_mixed(s)
    s.set_summary(SID, "summary mentioning thanks")
    s.pop_last_n(SID, 1)

    assert s.drop_messages(SID, [ids[-1]]) == 1

    row = s._conn.execute(
        "SELECT summary, summary_updated_at FROM sessions WHERE id=?", (SID,)
    ).fetchone()
    assert row == (None, None)
    assert all(
        "summary mentioning thanks" not in (m.get("content") or "")
        for m in s.assemble_context(SID)
    )


# -------- drop_by_tool --------


def test_drop_by_tool_match(tmp_path):
    s = _store(tmp_path)
    _seed_mixed(s)
    n = s.drop_by_tool(SID, "search")
    assert n == 1
    assert all(r.tool_name != "search" for r in s.iter_messages(SID, limit=100))


def test_drop_by_tool_no_match(tmp_path):
    s = _store(tmp_path)
    _seed_mixed(s)
    assert s.drop_by_tool(SID, "nosuch") == 0


def test_drop_by_tool_leaves_call_emission_row(tmp_path):
    s = _store(tmp_path)
    ids = _seed_mixed(s)
    s.drop_by_tool(SID, "search")
    # The assistant tool_call emission row (tool_name NULL) should remain
    rows = s.iter_messages(SID, kind="tool", limit=100)
    assert any(r.kind == "tool_call" for r in rows)


def test_drop_by_tool_decrements_message_count(tmp_path):
    s = _store(tmp_path)
    ids = _seed_mixed(s)
    before = s._conn.execute(
        "SELECT message_count FROM sessions WHERE id=?", (SID,)
    ).fetchone()[0]
    n = s.drop_by_tool(SID, "search")
    after = s._conn.execute(
        "SELECT message_count FROM sessions WHERE id=?", (SID,)
    ).fetchone()[0]
    assert after == before - n


# -------- drop_range --------


def test_drop_range_inclusive(tmp_path):
    s = _store(tmp_path)
    ids = _seed_mixed(s)
    n = s.drop_range(SID, ids[1], ids[3])
    assert n == 3
    remaining = {r.id for r in s.iter_messages(SID, limit=100)}
    assert ids[1] not in remaining and ids[3] not in remaining
    assert ids[0] in remaining and ids[4] in remaining


def test_drop_range_inverted(tmp_path):
    s = _store(tmp_path)
    ids = _seed_mixed(s)
    n = s.drop_range(SID, ids[3], ids[1])
    assert n == 0
    assert len(s.iter_messages(SID, limit=100)) == len(ids)


def test_drop_range_decrements_message_count(tmp_path):
    s = _store(tmp_path)
    ids = _seed_mixed(s)
    before = s._conn.execute(
        "SELECT message_count FROM sessions WHERE id=?", (SID,)
    ).fetchone()[0]
    n = s.drop_range(SID, ids[0], ids[2])
    after = s._conn.execute(
        "SELECT message_count FROM sessions WHERE id=?", (SID,)
    ).fetchone()[0]
    assert after == before - n


# -------- schema migration --------


def test_schema_migration_idempotent(tmp_path):
    """A v1 DB (no token_estimate / model / dropped_*) migrates cleanly."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT, title TEXT, metadata TEXT,
            started_at REAL NOT NULL, ended_at REAL,
            message_count INTEGER NOT NULL DEFAULT 0,
            summary TEXT, summary_updated_at REAL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_name TEXT, tool_calls TEXT, tool_call_id TEXT,
            timestamp REAL NOT NULL, metadata TEXT
        );
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version VALUES (1);
        INSERT INTO sessions (id, source, started_at, message_count, summary)
        VALUES ('s1', 'x', 0, 1, 'legacy summary');
        INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s1', 'user', 'old row', 0);
        """
    )
    conn.commit()
    conn.close()
    # Open → migrates
    s = ContextStore(db)
    tables = {
        r[0]
        for r in s._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    cols_m = {r[1] for r in s._conn.execute("PRAGMA table_info(messages)").fetchall()}
    cols_s = {r[1] for r in s._conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "context_events" in tables
    assert "token_estimate" in cols_m
    assert {"dropped_at", "dropped_by", "drop_batch_id"} <= cols_m
    assert "model" in cols_s
    assert "summary_envelope" in cols_s
    assert s.get_summary("s1") == "legacy summary"
    assert s.get_summary_envelope("s1") is None
    # Backfilled for the legacy row
    est = s._conn.execute("SELECT token_estimate FROM messages").fetchone()[0]
    assert est is not None and est >= 0
    # Re-open is no-op (no exception)
    s.close()
    ContextStore(db).close()


# -------- windows --------


def test_windows_prefix_tiebreak():
    size, known = get_window("sonnet-4.5-20260301")
    assert known is True
    assert size == 1_000_000  # sonnet-4.5, not sonnet-4
    assert get_window("claude-sonnet-4-5-20260301") == (1_000_000, True)
    assert get_window("claude_sonnet_4_5_20260301") == (1_000_000, True)
    assert get_window("claude-sonnet-4-7") == (128_000, False)
    assert get_window("opus-4.7") == (200_000, True)
    assert get_window("claude-opus-4-7-20260514") == (200_000, True)
    assert get_window(None) == (128_000, False)
    assert get_window("totally-unknown-xyz") == (128_000, False)
