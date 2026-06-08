"""Integration tests for DCPMiddleware (in-memory SQLite)."""

import json
import sqlite3

import pytest

from context_manager.dcp.config import DCPConfig, DCPProtectionsConfig
from context_manager.dcp.middleware import DCPMiddleware, tag_ctx_ids
from context_manager.store import ContextStore, Message


def _make_msg(id, role, content=None):
    m = Message(id=id, role=role, content=content)
    return m


def _tag(msgs):
    return tag_ctx_ids(msgs)


@pytest.fixture
def db(tmp_path):
    s = ContextStore(tmp_path / "ctx.db")
    yield s
    s.close()


@pytest.fixture
def conn(db):
    return db._conn


@pytest.fixture
def middleware(conn):
    cfg = DCPConfig(enabled=True)
    return DCPMiddleware(conn, cfg)


def test_build_outbound_noop_when_disabled(conn):
    cfg = DCPConfig(enabled=False)
    mw = DCPMiddleware(conn, cfg)
    messages = [{"role": "user", "content": "hello", "_ctx_id": 1}]
    out = mw.build_outbound("s1", messages, fill_ratio=0.9)
    # Private keys stripped, content unchanged
    assert out[0]["content"] == "hello"
    assert "_ctx_id" not in out[0]


def test_build_outbound_strips_private_keys(middleware):
    messages = [{"role": "user", "content": "hi", "_ctx_id": 1, "_dcp_nudge": True}]
    out = middleware.build_outbound("s1", messages, fill_ratio=0.0)
    assert "_ctx_id" not in out[0]
    assert "_dcp_nudge" not in out[0]


def test_handle_compress_range_success(middleware):
    messages = [
        {"role": "user", "content": "a", "_ctx_id": 1},
        {"role": "assistant", "content": "b", "_ctx_id": 2},
        {"role": "user", "content": "c", "_ctx_id": 3},
    ]
    args = {
        "mode": "range",
        "start_message_id": "1",
        "end_message_id": "2",
        "summary": "Goal: test. Progress: done.",
    }
    result = middleware.handle_compress("s1", messages, args)
    assert result.error is None
    assert result.placeholder is not None
    assert "placeholder" in result.tool_result_text
    assert middleware.active_placeholder_count("s1") == 1


def test_handle_compress_updates_turn_state(middleware):
    messages = [{"role": "user", "content": "x", "_ctx_id": 1}]
    args = {"mode": "range", "start_message_id": "1", "end_message_id": "1",
            "summary": "S"}
    middleware.note_user_turn("s1")
    middleware.note_user_turn("s1")
    result = middleware.handle_compress("s1", messages, args)
    assert result.error is None
    # After compress, last_compress_turn should be current turn (2)
    state = middleware._get_state("s1")
    assert state["last_compress_turn"] == state["turn"]


def test_build_outbound_applies_active_placeholders(middleware):
    messages = [
        {"role": "user", "content": "a", "_ctx_id": 1},
        {"role": "assistant", "content": "b", "_ctx_id": 2},
        {"role": "user", "content": "c", "_ctx_id": 3},
    ]
    args = {
        "mode": "range",
        "start_message_id": "1",
        "end_message_id": "2",
        "summary": "SUMMARY TEXT",
    }
    middleware.handle_compress("s1", messages, args)
    out = middleware.build_outbound("s1", messages, fill_ratio=0.0)
    # Original messages 1,2 should be replaced by placeholder
    non_placeholder = [m for m in out if not m.get("_dcp_placeholder") and "_ctx_id" not in m]
    placeholder = [m for m in out if "SUMMARY TEXT" in m.get("content", "")]
    assert len(placeholder) == 1


def test_deactivate_placeholder_reverts(middleware):
    messages = [{"role": "user", "content": "x", "_ctx_id": 1}]
    args = {"mode": "range", "start_message_id": "1", "end_message_id": "1",
            "summary": "S"}
    result = middleware.handle_compress("s1", messages, args)
    ph_id = result.placeholder.id
    middleware.deactivate_placeholder("s1", ph_id)
    assert middleware.active_placeholder_count("s1") == 0


def test_handle_compress_invalid_mode(middleware):
    messages = [{"role": "user", "content": "x", "_ctx_id": 1}]
    args = {"mode": "unknown", "summary": "S"}
    result = middleware.handle_compress("s1", messages, args)
    assert result.error == "unknown_mode"
    assert result.placeholder is None


def test_handle_compress_empty_summary_rejected(middleware):
    messages = [{"role": "user", "content": "x", "_ctx_id": 1}]
    args = {"mode": "range", "start_message_id": "1", "end_message_id": "1",
            "summary": ""}
    result = middleware.handle_compress("s1", messages, args)
    assert result.error == "empty_summary"


def test_nudge_appended_when_fill_high(middleware):
    # turns_since_compress starts at turn - (-999).
    # We need turns_since >= cooldown (10) and (turns_since - cooldown) % repeat (5) == 0.
    # Simplest: call note_user_turn once → turn=1, turns_since = 1-(-999) = 1000.
    # (1000 - 10) % 5 = 990 % 5 = 0 ✓
    middleware.note_user_turn("s1")
    messages = [{"role": "user", "content": "x", "_ctx_id": 1}]
    out = middleware.build_outbound("s1", messages, fill_ratio=0.80)
    nudge = [m for m in out if m.get("role") == "system" and "DCP" in m.get("content", "")]
    assert len(nudge) == 1


def test_nudge_not_appended_when_fill_low(middleware):
    messages = [{"role": "user", "content": "x", "_ctx_id": 1}]
    out = middleware.build_outbound("s1", messages, fill_ratio=0.30)
    nudge = [m for m in out if m.get("role") == "system" and "DCP" in m.get("content", "")]
    assert len(nudge) == 0


def test_tag_ctx_ids_from_store_messages():
    msgs = [
        Message(id=42, role="user", content="hello"),
        Message(id=43, role="assistant", content="world"),
    ]
    tagged = tag_ctx_ids(msgs)
    assert tagged[0]["_ctx_id"] == 42
    assert tagged[1]["_ctx_id"] == 43
    assert tagged[0]["role"] == "user"


# ── render_ctx_ids: inline [#N] marker so model can call compress with ids ────

def test_render_ctx_ids_prefixes_message_content_by_default(middleware):
    """Default config.render_ctx_ids=True must prepend [#N] to each message."""
    messages = [
        {"role": "user",      "content": "hello", "_ctx_id": 42},
        {"role": "assistant", "content": "world", "_ctx_id": 43},
    ]
    out = middleware.build_outbound("s1", messages, fill_ratio=0.0)
    assert out[0]["content"] == "[#42] hello"
    assert out[1]["content"] == "[#43] world"
    # Still strips the private key after rendering.
    assert "_ctx_id" not in out[0]
    assert "_ctx_id" not in out[1]


def test_render_ctx_ids_disabled_leaves_content_untouched(conn):
    cfg = DCPConfig(enabled=True, render_ctx_ids=False)
    mw = DCPMiddleware(conn, cfg)
    messages = [{"role": "user", "content": "hello", "_ctx_id": 42}]
    out = mw.build_outbound("s1", messages, fill_ratio=0.0)
    assert out[0]["content"] == "hello"
    assert "_ctx_id" not in out[0]


def test_render_ctx_ids_skips_placeholder_and_nudge(middleware):
    """Placeholder + nudge system messages have no _ctx_id; they must pass through verbatim."""
    middleware.note_user_turn("s1")  # turn=1
    messages = [
        {"role": "user",      "content": "a", "_ctx_id": 1},
        {"role": "assistant", "content": "b", "_ctx_id": 2},
    ]
    # Build a range placeholder first (this sets last_compress_turn=1).
    middleware.handle_compress(
        "s1", messages,
        {"mode": "range", "start_message_id": "1", "end_message_id": "2",
         "summary": "SUMMARY"},
    )
    # Advance turns so cooldown (10) elapses → turn=11, turns_since=10.
    for _ in range(10):
        middleware.note_user_turn("s1")
    # Now build outbound at high fill so nudge appends too.
    out = middleware.build_outbound("s1", messages, fill_ratio=0.99)
    # Placeholder content begins with "[DCP placeholder" — must NOT be re-prefixed.
    placeholders = [m for m in out if m.get("content", "").startswith("[DCP placeholder")]
    assert len(placeholders) == 1
    assert not placeholders[0]["content"].startswith("[#")  # no double prefix
    # Nudge content starts with "[context-manager DCP]" — also no re-prefix.
    nudges = [m for m in out if "context-manager DCP" in m.get("content", "")]
    assert len(nudges) == 1
    assert not nudges[0]["content"].startswith("[#")


def test_render_ctx_ids_handles_non_string_content_safely(middleware):
    """Multimodal/list content must be skipped, not corrupted."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "x"}], "_ctx_id": 7},
    ]
    out = middleware.build_outbound("s1", messages, fill_ratio=0.0)
    # Content untouched; key still stripped.
    assert out[0]["content"] == [{"type": "text", "text": "x"}]
    assert "_ctx_id" not in out[0]


def test_nudge_text_mentions_id_prefix_format():
    """Regression: nudge must explain the [#N] prefix so the model knows what to pass."""
    from context_manager.dcp.engine import _NUDGE_TEXT
    assert "[#N]" in _NUDGE_TEXT
    assert "start_message_id" in _NUDGE_TEXT
    assert "end_message_id" in _NUDGE_TEXT
