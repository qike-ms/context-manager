"""Tests for DCP engine pure functions.

All tests are hermetic (no I/O, no DB).  Fixtures use plain dicts in
OpenAI chat-completions format with _ctx_id set from synthetic row ids.
"""

import json

import pytest

from context_manager.dcp.config import DCPProtectionsConfig
from context_manager.dcp.engine import (
    apply_message_compress,
    apply_placeholders,
    apply_range_compress,
    dedupe_tool_calls,
    maybe_inject_nudge,
    purge_errored_inputs,
)
from context_manager.dcp.placeholders import Placeholder


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _msg(id, role, content=None, tool_calls=None, tool_call_id=None, tool_name=None):
    d = {"_ctx_id": id, "role": role}
    if content is not None:
        d["content"] = content
    if tool_calls is not None:
        d["tool_calls"] = tool_calls
    if tool_call_id is not None:
        d["tool_call_id"] = tool_call_id
    if tool_name is not None:
        d["tool_name"] = tool_name
    return d


def _tool_call(name, args, call_id):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _default_protections(**kw):
    return DCPProtectionsConfig(**kw)


def _no_protections():
    return DCPProtectionsConfig(tool_names=[], file_globs=[], protect_user_messages=False)


# ── apply_range_compress ──────────────────────────────────────────────────────

def test_range_compress_basic():
    messages = [
        _msg(1, "user", "hello"),
        _msg(2, "assistant", "world"),
        _msg(3, "user", "follow-up"),
    ]
    result = apply_range_compress(
        messages, "1", "2", "SUMMARY",
        protections=_no_protections(), existing_placeholders=[]
    )
    assert result.error is None
    # placeholder inserted, original 1+2 gone
    roles = [m["role"] for m in result.messages]
    assert roles == ["system", "user"]
    assert result.messages[0]["_dcp_placeholder"]
    assert "SUMMARY" in result.messages[0]["content"]
    assert result.compressed_ids == ["1", "2"]


def test_range_compress_protected_tool_appended_to_summary():
    tc = _tool_call("skill", {"arg": 1}, "cid1")
    messages = [
        _msg(1, "user", "start"),
        _msg(2, "assistant", None, tool_calls=[tc]),
        _msg(3, "tool", "skill output", tool_call_id="cid1", tool_name="skill"),
        _msg(4, "user", "continue"),
    ]
    protections = DCPProtectionsConfig(tool_names=["skill"])
    result = apply_range_compress(
        messages, "1", "3", "BASE",
        protections=protections, existing_placeholders=[]
    )
    assert result.error is None
    ph_content = result.messages[0]["content"]
    # Protected content appended as block
    assert "protected" in ph_content
    assert "skill output" in ph_content
    # Ids 2 and 3 are not in compressed_ids (they are protected / tool-call pair)
    # id 1 (user) is not protected by default, so it is in compressed_ids
    assert "1" in result.compressed_ids


def test_range_compress_invalid_range():
    messages = [_msg(1, "user", "x")]
    result = apply_range_compress(
        messages, "5", "2", "S",
        protections=_no_protections(), existing_placeholders=[]
    )
    assert result.error is not None
    assert result.error.code == "invalid_range"
    assert result.messages == messages  # unchanged


def test_range_compress_partial_overlap_rejected():
    from context_manager.dcp.placeholders import Placeholder
    existing = [
        Placeholder(
            id=1, session_id="s", kind="range",
            span_start="3", span_end="7",
            msg_ids=None, summary="old", active=True,
            nested_in_id=None, created_at=0.0, deactivated_at=None,
        )
    ]
    messages = [_msg(i, "user", f"m{i}") for i in range(1, 11)]
    # New range [2,5] partially overlaps [3,7]
    result = apply_range_compress(
        messages, "2", "5", "NEW",
        protections=_no_protections(), existing_placeholders=existing,
    )
    assert result.error is not None
    assert result.error.code == "partial_overlap"


def test_range_compress_nested_is_ok():
    """A new range that fully contains an existing one is allowed."""
    existing = [
        Placeholder(
            id=1, session_id="s", kind="range",
            span_start="3", span_end="5",
            msg_ids=None, summary="inner", active=True,
            nested_in_id=None, created_at=0.0, deactivated_at=None,
        )
    ]
    messages = [_msg(i, "user", f"m{i}") for i in range(1, 9)]
    result = apply_range_compress(
        messages, "2", "7", "OUTER",
        protections=_no_protections(), existing_placeholders=existing,
    )
    assert result.error is None


# ── apply_message_compress ────────────────────────────────────────────────────

def test_message_compress_replaces_named_ids():
    messages = [_msg(i, "user", f"m{i}") for i in range(1, 6)]
    result = apply_message_compress(
        messages, ["2", "4"], "SUMMARY", protections=_no_protections()
    )
    assert result.error is None
    ids_remaining = [m.get("_ctx_id") for m in result.messages if not m.get("_dcp_placeholder")]
    assert 2 not in ids_remaining
    assert 4 not in ids_remaining
    assert 1 in ids_remaining
    assert 3 in ids_remaining
    assert 5 in ids_remaining


def test_message_compress_missing_ids_returns_error():
    messages = [_msg(1, "user", "x")]
    result = apply_message_compress(
        messages, ["99"], "S", protections=_no_protections()
    )
    assert result.error is not None
    assert "99" in result.error.detail


# ── apply_placeholders (middleware) ───────────────────────────────────────────

def test_apply_placeholders_substitutes_range():
    messages = [_msg(i, "user", f"m{i}") for i in range(1, 6)]
    ph = Placeholder(
        id=1, session_id="s", kind="range",
        span_start="2", span_end="4",
        msg_ids=None, summary="PLACEHOLDER TEXT", active=True,
        nested_in_id=None, created_at=0.0, deactivated_at=None,
    )
    out = apply_placeholders(messages, [ph])
    assert len(out) == 3  # 1, placeholder, 5
    assert out[0]["_ctx_id"] == 1
    assert out[1]["_dcp_placeholder"]
    assert "PLACEHOLDER TEXT" in out[1]["content"]
    assert out[2]["_ctx_id"] == 5


def test_apply_placeholders_noop_when_empty():
    messages = [_msg(i, "user", f"m{i}") for i in range(1, 4)]
    out = apply_placeholders(messages, [])
    assert out == messages


def test_apply_placeholders_idempotent():
    messages = [_msg(i, "user", f"m{i}") for i in range(1, 4)]
    ph = Placeholder(
        id=1, session_id="s", kind="range",
        span_start="1", span_end="2",
        msg_ids=None, summary="S", active=True,
        nested_in_id=None, created_at=0.0, deactivated_at=None,
    )
    once = apply_placeholders(messages, [ph])
    twice = apply_placeholders(once, [ph])
    # Second pass: _ctx_id absent on placeholder message → won't be covered again
    assert len(once) == len(twice)


# ── dedupe_tool_calls ─────────────────────────────────────────────────────────

def test_dedupe_replaces_earlier_duplicate():
    tc = _tool_call("bash", {"cmd": "ls"}, "cid1")
    tc2 = _tool_call("bash", {"cmd": "ls"}, "cid2")  # same args
    messages = [
        _msg(1, "user", "start"),
        _msg(2, "assistant", None, tool_calls=[tc]),
        _msg(3, "user", "again"),
        _msg(4, "assistant", None, tool_calls=[tc2]),
    ]
    out = dedupe_tool_calls(messages, protections=_no_protections())
    # Message 2 should have its args replaced with a dedup stub
    calls_2 = out[1].get("tool_calls") or []
    args_2 = json.loads(calls_2[0]["function"]["arguments"])
    assert "_deduped" in args_2
    # Message 4 (the last one) should be unchanged
    calls_4 = out[3].get("tool_calls") or []
    args_4 = json.loads(calls_4[0]["function"]["arguments"])
    assert "cmd" in args_4


def test_dedupe_leaves_different_args_alone():
    tc_a = _tool_call("bash", {"cmd": "ls"}, "c1")
    tc_b = _tool_call("bash", {"cmd": "pwd"}, "c2")
    messages = [
        _msg(1, "assistant", None, tool_calls=[tc_a]),
        _msg(2, "assistant", None, tool_calls=[tc_b]),
    ]
    out = dedupe_tool_calls(messages, protections=_no_protections())
    for msg in out:
        calls = msg.get("tool_calls") or []
        for c in calls:
            args = json.loads(c["function"]["arguments"])
            assert "_deduped" not in args


def test_dedupe_protected_tool_exempt():
    tc = _tool_call("skill", {"x": 1}, "c1")
    tc2 = _tool_call("skill", {"x": 1}, "c2")
    messages = [
        _msg(1, "assistant", None, tool_calls=[tc]),
        _msg(2, "assistant", None, tool_calls=[tc2]),
    ]
    protections = DCPProtectionsConfig(tool_names=["skill"])
    out = dedupe_tool_calls(messages, protections=protections)
    for msg in out:
        calls = msg.get("tool_calls") or []
        for c in calls:
            args = json.loads(c["function"]["arguments"])
            assert "_deduped" not in args


# ── purge_errored_inputs ──────────────────────────────────────────────────────

def _error_tool_result(call_id, content="Error: something broke"):
    return {
        "_ctx_id": None,
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
        "metadata": {"is_error": True},
    }


def test_purge_replaces_old_error_args():
    tc = _tool_call("bash", {"cmd": "bad"}, "c1")
    messages = [
        _msg(1, "user", "run"),
        _msg(2, "assistant", None, tool_calls=[tc]),
        _error_tool_result("c1"),
        _msg(None, "user", "t3"),
        _msg(None, "user", "t4"),
        _msg(None, "user", "t5"),  # turn 5 — threshold 4 met
    ]
    out = purge_errored_inputs(
        messages,
        turn_threshold=4,
        protections=_no_protections(),
        now_turn=5,
    )
    calls = out[1].get("tool_calls") or []
    args = json.loads(calls[0]["function"]["arguments"])
    assert "_purged" in args


def test_purge_leaves_recent_error_alone():
    tc = _tool_call("bash", {"cmd": "bad"}, "c1")
    messages = [
        _msg(1, "user", "run"),
        _msg(2, "assistant", None, tool_calls=[tc]),
        _error_tool_result("c1"),
    ]
    out = purge_errored_inputs(
        messages,
        turn_threshold=4,
        protections=_no_protections(),
        now_turn=1,  # only 1 turn old
    )
    calls = out[1].get("tool_calls") or []
    args = json.loads(calls[0]["function"]["arguments"])
    assert "cmd" in args  # unchanged


# ── maybe_inject_nudge ────────────────────────────────────────────────────────

def test_nudge_injected_when_full_and_cooldown_passed():
    messages = [_msg(1, "user", "x")]
    out = maybe_inject_nudge(
        messages,
        fill_ratio=0.75,
        turns_since_compress=15,
        cooldown_turns=10,
        repeat_every_turns=5,
        fill_threshold=0.65,
    )
    assert len(out) == 2
    assert out[-1].get("_dcp_nudge")


def test_nudge_not_injected_below_threshold():
    messages = [_msg(1, "user", "x")]
    out = maybe_inject_nudge(
        messages,
        fill_ratio=0.50,
        turns_since_compress=20,
        cooldown_turns=10,
        repeat_every_turns=5,
        fill_threshold=0.65,
    )
    assert len(out) == 1


def test_nudge_not_injected_within_cooldown():
    messages = [_msg(1, "user", "x")]
    out = maybe_inject_nudge(
        messages,
        fill_ratio=0.80,
        turns_since_compress=5,  # inside 10-turn cooldown
        cooldown_turns=10,
        repeat_every_turns=5,
        fill_threshold=0.65,
    )
    assert len(out) == 1
