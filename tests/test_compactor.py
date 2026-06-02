import asyncio

import pytest

from context_manager import Compactor, ContextStore, Message, TokenUsage
from context_manager.compactor import CompactorConfig, select_compaction_head_tail


@pytest.fixture
def store(tmp_path):
    s = ContextStore(tmp_path / "ctx.db")
    yield s
    s.close()


def seed(store, sid, n):
    return [store.append(sid, "user", f"msg{i}") for i in range(n)]


def msg(id, role, content=None, tokens=1, **kwargs):
    return Message(id=id, role=role, content=content, token_estimate=tokens, **kwargs)


def test_compactor_config_positional_args_keep_previous_semantics():
    cfg = CompactorConfig(True, 7, 0.5, 11, True)

    assert cfg.enabled is True
    assert cfg.keep_verbatim_n == 7
    assert cfg.idle_interval_sec == 0.5
    assert cfg.min_messages_to_summarize == 11
    assert cfg.delete_summarized is True
    assert cfg.keep_verbatim_tokens is None
    assert cfg.keep_verbatim_window_ratio is None


def test_token_budget_tail_selects_recent_complete_turns():
    delta = [
        msg(1, "user", "u1", tokens=8),
        msg(2, "assistant", "a1", tokens=8),
        msg(3, "user", "u2", tokens=7),
        msg(4, "assistant", "a2", tokens=7),
        msg(5, "user", "u3", tokens=6),
        msg(6, "assistant", "a3", tokens=6),
    ]

    selection = select_compaction_head_tail(
        delta,
        CompactorConfig(keep_verbatim_n=1, keep_verbatim_tokens=15),
        window_size=1000,
    )

    assert [m.id for m in selection.head] == [1, 2, 3, 4]
    assert [m.id for m in selection.tail] == [5, 6]
    assert selection.tail_tokens == 12
    assert selection.stopped_reason == "token_budget_exhausted"


def test_recent_user_correction_stays_verbatim_when_budget_permits():
    delta = [
        msg(1, "user", "large prior ask", tokens=40),
        msg(2, "assistant", "large prior answer", tokens=40),
        msg(3, "user", "correction", tokens=5),
    ]

    selection = select_compaction_head_tail(
        delta,
        CompactorConfig(keep_verbatim_n=1, keep_verbatim_tokens=10),
        window_size=1000,
    )

    assert [m.content for m in selection.tail] == ["correction"]
    assert selection.tail_tokens == 5


def test_missing_tail_token_estimate_fallback_preserves_turn_boundary():
    delta = [
        msg(1, "user", "u1", tokens=10),
        msg(2, "assistant", "a1", tokens=10),
        msg(3, "user", "u2", tokens=10),
        msg(4, "assistant", "a2", tokens=None),
    ]

    selection = select_compaction_head_tail(
        delta,
        CompactorConfig(keep_verbatim_n=1, keep_verbatim_tokens=100),
        window_size=1000,
    )

    assert selection.strategy == "count_fallback"
    assert selection.fallback_reason == "missing_token_estimate"
    assert [m.id for m in selection.head] == [1, 2]
    assert [m.id for m in selection.tail] == [3, 4]


def test_count_mode_preserves_recent_turn_boundary_when_possible():
    delta = [
        msg(1, "user", "u1"),
        msg(2, "assistant", "a1"),
        msg(3, "user", "u2"),
        msg(4, "assistant", "a2"),
    ]

    selection = select_compaction_head_tail(
        delta,
        CompactorConfig(keep_verbatim_n=1),
        window_size=1000,
    )

    assert selection.strategy == "count"
    assert [m.id for m in selection.head] == [1, 2]
    assert [m.id for m in selection.tail] == [3, 4]


def test_tool_call_result_pair_is_not_split_by_token_budget():
    delta = [
        msg(1, "user", "u1", tokens=5),
        msg(
            2,
            "assistant",
            tokens=30,
            tool_calls=[{"id": "call-1", "function": {"name": "lookup"}}],
        ),
        msg(3, "tool", "result", tokens=30, tool_call_id="call-1", tool_name="lookup"),
    ]

    too_small = select_compaction_head_tail(
        delta,
        CompactorConfig(keep_verbatim_n=1, keep_verbatim_tokens=35),
        window_size=1000,
    )
    assert too_small.tail == []
    assert too_small.stopped_reason == "token_budget_exhausted"

    enough = select_compaction_head_tail(
        delta,
        CompactorConfig(keep_verbatim_n=1, keep_verbatim_tokens=65),
        window_size=1000,
    )
    assert [m.id for m in enough.tail] == [1, 2, 3]


def test_incomplete_tool_history_is_dropped_from_verbatim_tail():
    delta = [
        msg(
            1,
            "assistant",
            tokens=10,
            tool_calls=[{"id": "call-1", "function": {"name": "lookup"}}],
        ),
    ]

    selection = select_compaction_head_tail(
        delta,
        CompactorConfig(keep_verbatim_n=1, keep_verbatim_tokens=100),
        window_size=1000,
    )

    assert selection.tail == []
    assert [m.id for m in selection.head] == [1]
    assert selection.stopped_reason == "incomplete_tool_history"


def test_keep_verbatim_n_does_not_leave_orphan_tool_result_tail():
    delta = [
        msg(
            1,
            "assistant",
            tokens=10,
            tool_calls=[{"id": "call-1", "function": {"name": "lookup"}}],
        ),
        msg(2, "tool", "result", tokens=10, tool_call_id="call-1", tool_name="lookup"),
    ]

    selection = select_compaction_head_tail(
        delta,
        CompactorConfig(keep_verbatim_n=1),
        window_size=1000,
    )

    assert [m.id for m in selection.tail] == [1, 2]
    assert selection.head == []
    assert selection.stopped_reason is None


def test_system_pinning_expands_tail_to_complete_tool_history():
    delta = [
        msg(1, "user", "u1", tokens=10),
        msg(
            2,
            "assistant",
            tokens=10,
            tool_calls=[{"id": "call-1", "function": {"name": "lookup"}}],
        ),
        msg(3, "system", "updated rules", tokens=5),
        msg(4, "tool", "result", tokens=10, tool_call_id="call-1", tool_name="lookup"),
        msg(5, "user", "recent correction", tokens=5),
    ]

    selection = select_compaction_head_tail(
        delta,
        CompactorConfig(keep_verbatim_n=1, keep_verbatim_tokens=5),
        window_size=1000,
    )

    assert selection.head == []
    assert [m.id for m in selection.tail] == [1, 2, 3, 4, 5]
    assert selection.stopped_reason == "pinned_system_message"
    assert selection.token_budget == 5
    assert selection.tail_tokens == 40


@pytest.mark.asyncio
async def test_disabled_compactor_does_not_call_summarize(store):
    called = False

    async def summarize(msgs, prior):
        nonlocal called
        called = True
        return "summary"

    c = Compactor(store, summarize, CompactorConfig(enabled=False, min_messages_to_summarize=1))
    await c.start()
    c.note_append("s")
    await c.stop()
    assert called is False


@pytest.mark.asyncio
async def test_below_delta_threshold_noops(store):
    sid = "s"
    seed(store, sid, 3)
    calls = []

    async def summarize(msgs, prior):
        calls.append(msgs)
        return "summary"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=4, keep_verbatim_n=1))
    await c._compact_one(sid)
    assert calls == []
    assert store.get_summary(sid) is None


@pytest.mark.asyncio
async def test_below_delta_threshold_records_skipped_event(store):
    sid = "s"
    seed(store, sid, 3)

    async def summarize(msgs, prior):
        return "summary"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=4, keep_verbatim_n=1))
    await c._compact_one(sid)

    events = store.iter_events(sid)
    assert [e.event_type for e in events] == ["compaction_skipped"]
    assert events[0].metadata["reason"] == "below_threshold"
    assert events[0].metadata["delta_count"] == 3
    assert events[0].metadata["min_messages_to_summarize"] == 4


@pytest.mark.asyncio
async def test_missing_summarize_fn_records_skipped_event(store):
    c = Compactor(store, None, CompactorConfig(enabled=True, min_messages_to_summarize=1))
    await c._compact_one("s")

    events = store.iter_events("s")
    assert [e.event_type for e in events] == ["compaction_skipped"]
    assert events[0].metadata["reason"] == "summarize_fn_missing"


@pytest.mark.asyncio
async def test_no_head_records_skipped_event(store):
    sid = "s"
    seed(store, sid, 5)

    async def summarize(msgs, prior):
        return "summary"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=5, keep_verbatim_n=5))
    await c._compact_one(sid)

    events = store.iter_events(sid)
    assert [e.event_type for e in events] == ["compaction_skipped"]
    assert events[0].metadata["reason"] == "no_head"
    assert events[0].metadata["keep_verbatim_n"] == 5


@pytest.mark.asyncio
async def test_missing_message_ids_records_skipped_event(store):
    sid = "s"
    store.get_compaction_delta = lambda session_id, prior, watermark: [
        Message(id=None, role="user", content="no id"),
        Message(id=2, role="user", content="tail"),
    ]

    async def summarize(msgs, prior):
        return "summary"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=2, keep_verbatim_n=1))
    await c._compact_one(sid)

    events = store.iter_events(sid)
    assert [e.event_type for e in events] == ["compaction_skipped"]
    assert events[0].metadata["reason"] == "missing_ids"
    assert events[0].metadata["head_count"] == 1


@pytest.mark.asyncio
async def test_compacts_head_and_stores_watermark(store):
    sid = "s"
    ids = seed(store, sid, 5)
    seen = {}

    async def summarize(msgs, prior):
        seen["contents"] = [m.content for m in msgs]
        seen["prior"] = prior
        return "summary v1"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=5, keep_verbatim_n=2))
    await c._compact_one(sid)

    assert seen == {"contents": ["msg0", "msg1", "msg2"], "prior": None}
    summary, watermark, revision = store.get_compaction_state(sid)
    assert summary == "summary v1"
    assert watermark == ids[2]
    assert revision == 1
    envelope = store.get_summary_envelope(sid)
    assert envelope is not None
    assert envelope.text == "summary v1"
    assert envelope.through_message_id == ids[2]
    assert envelope.safety_policy == "reference_material_not_active_instructions"
    assert envelope.source == "compactor"

    events = store.iter_events(sid)
    assert [e.event_type for e in events] == [
        "compaction_started",
        "compaction_completed",
    ]
    assert events[0].metadata["summarized_count"] == 3
    assert events[0].metadata["target_watermark"] == ids[2]
    assert events[1].metadata["watermark"] == ids[2]
    assert events[1].metadata["summarized_count"] == 3
    assert events[1].metadata["revision"] == 1


@pytest.mark.asyncio
async def test_window_ratio_budget_uses_token_usage_window_size(store):
    sid = "ratio"
    ids = seed(store, sid, 4)
    for id_, estimate in zip(ids, [60, 60, 40, 40]):
        store._conn.execute(
            "UPDATE messages SET token_estimate = ? WHERE id = ?",
            (estimate, id_),
        )
    store.token_usage = lambda session_id: TokenUsage(
        active_tokens=200,
        total_seen=200,
        window_size=1000,
        window_pct=0.2,
        calibrated=True,
        missing_estimates=0,
    )
    seen = {}

    async def summarize(msgs, prior):
        seen["ids"] = [m.id for m in msgs]
        return "summary"

    c = Compactor(
        store,
        summarize,
        CompactorConfig(
            enabled=True,
            min_messages_to_summarize=4,
            keep_verbatim_n=1,
            keep_verbatim_window_ratio=0.1,
        ),
    )
    await c._compact_one(sid)

    assert seen["ids"] == ids[:2]
    events = store.iter_events(sid)
    assert events[0].metadata["tail_token_budget"] == 100
    assert events[0].metadata["tail_tokens"] == 80
    assert events[0].metadata["tail_selection_strategy"] == "token_budget"


@pytest.mark.asyncio
async def test_system_messages_are_pinned_verbatim_under_token_budget(store):
    sid = "system"
    system_id = store.append(sid, "system", "system rules")
    user_id = store.append(sid, "user", "old ask")
    assistant_id = store.append(
        sid,
        "assistant",
        None,
        tool_calls=[{"id": "call-1", "function": {"name": "lookup"}}],
    )
    tool_id = store.append(
        sid,
        "tool",
        "tool result",
        tool_call_id="call-1",
        tool_name="lookup",
    )
    correction_id = store.append(sid, "user", "recent correction")
    for id_, estimate in [
        (system_id, 20),
        (user_id, 20),
        (assistant_id, 20),
        (tool_id, 20),
        (correction_id, 5),
    ]:
        store._conn.execute(
            "UPDATE messages SET token_estimate = ? WHERE id = ?",
            (estimate, id_),
        )
    selection = select_compaction_head_tail(
        store.get_compaction_delta(sid, None, None),
        CompactorConfig(keep_verbatim_n=1, keep_verbatim_tokens=10),
        window_size=1000,
    )

    assert selection.strategy == "token_budget"
    assert selection.stopped_reason == "pinned_system_message"
    assert selection.token_budget == 10
    assert selection.tail_tokens == 85
    assert selection.head == []
    assert [m.id for m in selection.tail] == [
        system_id,
        user_id,
        assistant_id,
        tool_id,
        correction_id,
    ]

    called = False

    async def summarize(msgs, prior):
        nonlocal called
        called = True
        return "summary"

    c = Compactor(
        store,
        summarize,
        CompactorConfig(
            enabled=True,
            min_messages_to_summarize=5,
            keep_verbatim_n=1,
            keep_verbatim_tokens=10,
        ),
    )
    await c._compact_one(sid)

    assert called is False
    assert store.get_summary(sid) is None
    events = store.iter_events(sid)
    assert events[0].event_type == "compaction_skipped"
    assert events[0].metadata["reason"] == "no_head"
    assert events[0].metadata["tail_selection_stopped_reason"] == "pinned_system_message"
    assert events[0].metadata["tail_token_budget"] == 10
    assert events[0].metadata["tail_tokens"] == 85


@pytest.mark.asyncio
async def test_compaction_started_is_recorded_before_summarize_callback(store):
    sid = "s"
    seed(store, sid, 5)
    seen = {}

    async def summarize(msgs, prior):
        seen["event_types"] = [e.event_type for e in store.iter_events(sid)]
        return "summary"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=5, keep_verbatim_n=2))
    await c._compact_one(sid)

    assert seen["event_types"] == ["compaction_started"]


@pytest.mark.asyncio
async def test_assemble_context_uses_summary_plus_rows_after_watermark(store):
    sid = "s"
    seed(store, sid, 5)

    async def summarize(msgs, prior):
        return "summary v1"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=5, keep_verbatim_n=2))
    await c._compact_one(sid)
    store.append(sid, "user", "new")

    ctx = store.assemble_context(sid, recent_n=2)
    assert ctx[0]["role"] == "system"
    assert "summary v1" in ctx[0]["content"]
    assert [m["content"] for m in ctx[1:]] == ["msg3", "msg4", "new"]


@pytest.mark.asyncio
async def test_second_compaction_only_summarizes_delta_after_watermark(store):
    sid = "s"
    seed(store, sid, 5)
    calls = []

    async def summarize(msgs, prior):
        calls.append(([m.content for m in msgs], prior))
        return f"summary {len(calls)}"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=5, keep_verbatim_n=2))
    await c._compact_one(sid)
    for i in range(5, 10):
        store.append(sid, "user", f"msg{i}")
    await c._compact_one(sid)

    assert calls[0] == (["msg0", "msg1", "msg2"], None)
    assert calls[1] == (["msg3", "msg4", "msg5", "msg6", "msg7"], "summary 1")


@pytest.mark.asyncio
async def test_append_during_summarize_is_preserved_after_watermark(store):
    sid = "s"
    seed(store, sid, 5)

    async def summarize(msgs, prior):
        store.append(sid, "user", "during")
        return "summary v1"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=5, keep_verbatim_n=2))
    await c._compact_one(sid)

    ctx = store.assemble_context(sid, recent_n=2)
    assert [m["content"] for m in ctx[1:]] == ["msg3", "msg4", "during"]


@pytest.mark.asyncio
async def test_concurrent_hard_drop_aborts_writeback(store):
    sid = "s"
    ids = seed(store, sid, 5)

    async def summarize(msgs, prior):
        store.drop_messages(sid, [ids[0]])
        return "stale summary"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=5, keep_verbatim_n=2))
    await c._compact_one(sid)
    assert store.get_summary(sid) is None
    event_types = [e.event_type for e in store.iter_events(sid)]
    assert event_types[-1] == "compaction_aborted_revision_changed"


@pytest.mark.asyncio
async def test_concurrent_soft_delete_aborts_writeback(store):
    sid = "s"
    seed(store, sid, 5)

    async def summarize(msgs, prior):
        store.pop_last_n(sid, 1)
        return "stale summary"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=5, keep_verbatim_n=2))
    await c._compact_one(sid)
    assert store.get_summary(sid) is None


@pytest.mark.asyncio
async def test_revision_conflict_aborts_and_preserves_prior_summary(store):
    sid = "s"
    seed(store, sid, 5)
    store.set_summary(sid, "prior")

    async def summarize(msgs, prior):
        store._conn.execute(
            "UPDATE sessions SET summary_revision = summary_revision + 1 WHERE id = ?",
            (sid,),
        )
        return "stale summary"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=5, keep_verbatim_n=2))
    await c._compact_one(sid)

    assert store.get_summary(sid) == "prior"
    assert [e.event_type for e in store.iter_events(sid)] == [
        "compaction_started",
        "compaction_aborted_revision_changed",
    ]


def test_summary_invalidation_clears_watermark(store):
    sid = "s"
    ids = seed(store, sid, 3)
    store.set_summary(sid, "summary", through_message_id=ids[1])
    store.drop_messages(sid, [ids[0]])
    summary, watermark, _ = store.get_compaction_state(sid)
    assert summary is None
    assert watermark is None


def test_reset_clears_summary_even_when_no_live_rows(store):
    sid = "s"
    ids = seed(store, sid, 1)
    store.set_summary(sid, "summary", through_message_id=ids[0])
    store.pop_last_n(sid, 1)
    # Recreate stale summary state with no live rows.
    store.set_summary(sid, "stale", through_message_id=ids[0])
    assert store.reset(sid) == 0
    summary, watermark, _ = store.get_compaction_state(sid)
    assert summary is None
    assert watermark is None


@pytest.mark.asyncio
async def test_empty_summary_keeps_prior(store):
    sid = "s"
    seed(store, sid, 5)
    store.set_summary(sid, "prior", through_message_id=None)

    async def summarize(msgs, prior):
        return "   "

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=5, keep_verbatim_n=2))
    await c._compact_one(sid)
    assert store.get_summary(sid) == "prior"
    events = store.iter_events(sid)
    assert [e.event_type for e in events] == [
        "compaction_started",
        "compaction_skipped",
    ]
    assert events[1].metadata["reason"] == "empty_summary"


@pytest.mark.asyncio
async def test_delete_summarized_keeps_summary_and_tail(store):
    sid = "s"
    seed(store, sid, 5)

    async def summarize(msgs, prior):
        return "summary v1"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=5, keep_verbatim_n=2, delete_summarized=True))
    await c._compact_one(sid)

    assert store.get_summary(sid) == "summary v1"
    assert [m.content for m in store.get_all(sid)] == ["msg3", "msg4"]
    ctx = store.assemble_context(sid)
    assert "summary v1" in ctx[0]["content"]
    assert [m["content"] for m in ctx[1:]] == ["msg3", "msg4"]


@pytest.mark.asyncio
async def test_migrated_summary_without_watermark_bootstraps(store):
    sid = "s"
    seed(store, sid, 5)
    store.set_summary(sid, "old summary")  # no watermark
    calls = []

    async def summarize(msgs, prior):
        calls.append(([m.content for m in msgs], prior))
        return "new summary"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=5, keep_verbatim_n=2))
    await c._compact_one(sid)
    assert calls == [(["msg0", "msg1", "msg2"], "old summary")]
    assert store.get_compaction_state(sid)[1] is not None


@pytest.mark.asyncio
async def test_queue_dedup_compacts_once(store):
    sid = "s"
    seed(store, sid, 5)
    calls = 0

    async def summarize(msgs, prior):
        nonlocal calls
        calls += 1
        return "summary"

    c = Compactor(store, summarize, CompactorConfig(enabled=True, min_messages_to_summarize=5, keep_verbatim_n=2, idle_interval_sec=0.01))
    await c.start()
    c.note_append(sid)
    c.note_append(sid)
    await asyncio.sleep(0.05)
    await c.stop()
    assert calls == 1
