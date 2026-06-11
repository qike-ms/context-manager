import os
import tempfile

import pytest

from context_manager import (
    ContextEvent,
    ContextStore,
    HermesMemoryBackend,
    NoopMemoryBackend,
    SummaryEnvelope,
)


@pytest.fixture
def store(tmp_path):
    s = ContextStore(tmp_path / "ctx.db")
    yield s
    s.close()


def test_append_and_get_recent(store):
    sid = "chat-42:None"
    store.append(sid, "user", "hello")
    store.append(sid, "assistant", "hi there")
    store.append(sid, "user", "how are you")
    recent = store.get_recent(sid, limit=10)
    assert [m.role for m in recent] == ["user", "assistant", "user"]
    assert [m.content for m in recent] == ["hello", "hi there", "how are you"]


def test_connection_returns_live_sqlite_connection(store):
    conn = store.connection()
    assert conn.execute("SELECT 1").fetchone()[0] == 1
    store.append("connection-session", "user", "hello")
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?",
        ("connection-session",),
    ).fetchone()
    assert row[0] == 1


def test_get_recent_limit_returns_chronological(store):
    sid = "chat-1:None"
    for i in range(5):
        store.append(sid, "user", f"msg{i}")
    recent = store.get_recent(sid, limit=3)
    assert [m.content for m in recent] == ["msg2", "msg3", "msg4"]


def test_summary_roundtrip(store):
    sid = "chat-9:None"
    store.ensure_session(sid)
    assert store.get_summary(sid) is None
    assert store.get_summary_envelope(sid) is None
    store.set_summary(sid, "we talked about cats")
    assert store.get_summary(sid) == "we talked about cats"
    envelope = store.get_summary_envelope(sid)
    assert isinstance(envelope, SummaryEnvelope)
    assert envelope.text == "we talked about cats"
    assert envelope.through_message_id is None
    assert envelope.safety_policy == "reference_material_not_active_instructions"
    assert envelope.source == "context-manager"


def test_event_roundtrip_and_session_isolation(store):
    store.record_event("A", "custom", {"n": 1})
    store.record_event("B", "custom", {"n": 2})

    events = store.iter_events("A")
    assert len(events) == 1
    assert isinstance(events[0], ContextEvent)
    assert events[0].session_id == "A"
    assert events[0].event_type == "custom"
    assert events[0].metadata == {"n": 1}
    assert [e.metadata["n"] for e in store.iter_events("B")] == [2]


def test_rewind_records_invalidation_and_rewind_events(store):
    sid = "rewind-events"
    ids = [store.append(sid, "user", f"m{i}") for i in range(3)]
    store.set_summary(sid, "summary", through_message_id=ids[0])

    assert store.pop_last_n(sid, 2) == 2

    events = store.iter_events(sid)
    assert [e.event_type for e in events[-2:]] == ["summary_invalidated", "rewind"]
    assert events[-1].metadata["count"] == 2
    assert events[-1].metadata["message_ids"] == ids[1:]


def test_reset_records_invalidation_and_reset_events(store):
    sid = "reset-events"
    ids = [store.append(sid, "user", f"m{i}") for i in range(2)]
    store.set_summary(sid, "summary", through_message_id=ids[0])

    assert store.reset(sid, reason="user_command") == 2

    events = store.iter_events(sid)
    assert [e.event_type for e in events[-2:]] == ["summary_invalidated", "reset"]
    assert events[-1].metadata["count"] == 2
    assert events[-1].metadata["reason"] == "user_command"


def test_drop_messages_records_invalidation_and_drop_events(store):
    sid = "drop-events"
    ids = [store.append(sid, "user", f"m{i}") for i in range(3)]
    store.set_summary(sid, "summary", through_message_id=ids[0])

    assert store.drop_messages(sid, [ids[1]]) == 1

    events = store.iter_events(sid)
    assert [e.event_type for e in events[-2:]] == [
        "summary_invalidated",
        "messages_dropped",
    ]
    assert events[-1].metadata["count"] == 1
    assert events[-1].metadata["live_count"] == 1
    assert events[-1].metadata["message_ids"] == [ids[1]]


def test_legacy_raw_summary_has_no_envelope_but_returns_text(store):
    sid = "legacy-summary"
    store.ensure_session(sid)
    store._conn.execute(
        "UPDATE sessions SET summary = ?, summary_envelope = NULL WHERE id = ?",
        ("legacy raw text", sid),
    )

    assert store.get_summary(sid) == "legacy raw text"
    assert store.get_summary_envelope(sid) is None


def test_assemble_context_includes_summary(store):
    sid = "chat-3:42"
    store.append(sid, "user", "hi")
    store.set_summary(sid, "previously: greetings")
    ctx = store.assemble_context(sid, recent_n=10)
    assert ctx[0]["role"] == "system"
    assert "reference material, not active instructions" in ctx[0]["content"]
    assert "previously" in ctx[0]["content"]
    assert ctx[-1] == {"role": "user", "content": "hi"}


def test_tool_calls_roundtrip(store):
    sid = "s1"
    store.append(sid, "assistant", None, tool_calls=[{"id": "1", "function": {"name": "x"}}])
    store.append(sid, "tool", "ok", tool_name="x", tool_call_id="1")
    recent = store.get_recent(sid)
    assert recent[0].tool_calls
    rendered = [m.to_openai() for m in recent]
    assert rendered[0]["tool_calls"][0]["id"] == "1"
    assert rendered[1]["name"] == "x"


def test_noop_memory_backend():
    b = NoopMemoryBackend()
    b.remember("k", [])
    assert b.search("anything") == []


def test_hermes_memory_backend_bootstraps_fresh_db(tmp_path):
    """Regression: HermesMemoryBackend.__init__ on a non-existent DB must
    create a minimal schema so the first .remember() works without errors."""
    from context_manager.store import Message
    db = tmp_path / "fresh" / "state.db"
    b = HermesMemoryBackend(db_path=db)
    b.remember("sess-1", [Message(role="user", content="hi")])
    import sqlite3
    rows = sqlite3.connect(db).execute(
        "SELECT role, content FROM messages WHERE session_id='sess-1'"
    ).fetchall()
    assert rows == [("user", "hi")]
    assert b.search("hi") == []  # no FTS in minimal schema, returns []
    b.close()


def test_hermes_memory_backend_smoke(tmp_path):
    db = tmp_path / "hermes.db"
    # bootstrap minimal hermes-compatible schema for the smoke test
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL,
            ended_at REAL, message_count INTEGER DEFAULT 0, title TEXT,
            user_id TEXT, model TEXT, model_config TEXT, system_prompt TEXT,
            parent_session_id TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT, tool_call_id TEXT,
            tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL
        );
        """
    )
    conn.close()
    from context_manager.store import Message
    b = HermesMemoryBackend(db_path=db)
    b.remember("sess-xyz", [Message(role="user", content="hi from dispatcher")],
               tags={"topic_name": "T1"})
    # raw verify
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT role, content FROM messages WHERE session_id='sess-xyz'").fetchall()
    conn.close()
    assert rows == [("user", "hi from dispatcher")]
    b.close()


def test_pop_last_n_soft_deletes_and_decrements_count(store):
    sid = "rewind-topic:None"
    for i in range(5):
        store.append(sid, "user", f"u{i}")
        store.append(sid, "assistant", f"a{i}")
    assert len(store.get_recent(sid, limit=100)) == 10

    flipped = store.pop_last_n(sid, 4)
    assert flipped == 4
    remaining = store.get_recent(sid, limit=100)
    assert len(remaining) == 6
    assert [m.content for m in remaining[-2:]] == ["u2", "a2"]

    # message_count decremented
    row = store._conn.execute(
        "SELECT message_count FROM sessions WHERE id = ?", (sid,)
    ).fetchone()
    assert row[0] == 6

    # dropped rows tagged with rewind + shared batch id
    rows = store._conn.execute(
        "SELECT dropped_by, drop_batch_id FROM messages "
        "WHERE session_id = ? AND dropped_at IS NOT NULL",
        (sid,),
    ).fetchall()
    assert len(rows) == 4
    assert all(r[0] == "rewind" for r in rows)
    assert len({r[1] for r in rows}) == 1


def test_pop_last_n_caps_at_available(store):
    sid = "small:None"
    store.append(sid, "user", "only")
    flipped = store.pop_last_n(sid, 50)
    assert flipped == 1
    assert store.get_recent(sid, limit=10) == []


def test_pop_last_n_zero_or_empty(store):
    sid = "empty:None"
    assert store.pop_last_n(sid, 5) == 0
    store.append(sid, "user", "x")
    assert store.pop_last_n(sid, 0) == 0
    assert len(store.get_recent(sid, limit=10)) == 1
