import asyncio

import pytest

from context_manager import Compactor, ContextStore
from context_manager.compactor import CompactorConfig


@pytest.fixture
def store(tmp_path):
    s = ContextStore(tmp_path / "ctx.db")
    yield s
    s.close()


def seed(store, sid, n):
    return [store.append(sid, "user", f"msg{i}") for i in range(n)]


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
