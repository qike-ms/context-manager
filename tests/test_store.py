import os
import tempfile

import pytest

from context_manager import ContextStore, NoopMemoryBackend, HermesMemoryBackend


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
    store.set_summary(sid, "we talked about cats")
    assert store.get_summary(sid) == "we talked about cats"


def test_assemble_context_includes_summary(store):
    sid = "chat-3:42"
    store.append(sid, "user", "hi")
    store.set_summary(sid, "previously: greetings")
    ctx = store.assemble_context(sid, recent_n=10)
    assert ctx[0]["role"] == "system"
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
