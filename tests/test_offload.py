"""Tests for issue #19 — tool-result offload policy."""

from __future__ import annotations

import threading

import pytest

from context_manager import ContextStore, OffloadPolicy, OffloadRecord


def _store(tmp_path, **policy_kwargs):
    db = tmp_path / "cm.db"
    store = ContextStore(db)
    if policy_kwargs:
        store.set_offload_policy(
            OffloadPolicy(root_dir=tmp_path / "offload", **policy_kwargs)
        )
    return store


def test_under_threshold_no_offload(tmp_path):
    store = _store(tmp_path, enabled=True, threshold_tokens=10_000)
    mid = store.append("s1", "tool", content="short content\nline2\n")
    assert store.get_offload(mid) is None
    assert store.iter_offloads("s1") == []
    rec = store.get_recent("s1")[0]
    assert rec.content == "short content\nline2\n"


def test_over_threshold_offloads_and_replaces_content(tmp_path):
    store = _store(
        tmp_path,
        enabled=True,
        threshold_tokens=20,
        head_lines=2,
        tail_lines=2,
    )
    big = "\n".join(f"line {i}" for i in range(200))
    mid = store.append("s1", "tool", content=big)
    rec = store.get_offload(mid)
    assert rec is not None
    assert rec.message_id == mid
    assert rec.path.exists()
    assert rec.original_chars == len(big)
    assert rec.original_lines >= 200
    stored = store.get_recent("s1")[0].content
    assert stored is not None
    assert "lines truncated" in stored
    assert str(rec.path) in stored
    assert stored.startswith("line 0\nline 1\n")
    assert stored.endswith("line 198\nline 199")


def test_assemble_context_uses_preview_not_full_payload(tmp_path):
    store = _store(
        tmp_path,
        enabled=True,
        threshold_tokens=10,
        head_lines=1,
        tail_lines=1,
    )
    big = "\n".join(f"row {i}" for i in range(500))
    mid = store.append("s1", "tool", content=big)
    msgs = store.assemble_context("s1", recent_n=10)
    full = [m for m in msgs if isinstance(m.get("content"), str) and "row 250" in m["content"]]
    assert full == [], "assemble_context must NOT inline the full offloaded body"
    assert any("lines truncated" in m["content"] for m in msgs if isinstance(m.get("content"), str))
    # Sanity: full body is still recoverable
    assert "row 250" in store.read_offload(mid)


def test_read_offload_slice(tmp_path):
    store = _store(tmp_path, enabled=True, threshold_tokens=10, head_lines=0, tail_lines=0)
    body = "abcdefghijklmnopqrstuvwxyz" * 100
    mid = store.append("s1", "tool", content=body)
    assert store.read_offload(mid, offset=0, limit=10) == body[:10]
    assert store.read_offload(mid, offset=26, limit=26) == body[26:52]
    assert store.read_offload(mid) == body


def test_read_offload_unknown_raises(tmp_path):
    store = _store(tmp_path, enabled=True, threshold_tokens=10)
    with pytest.raises(KeyError):
        store.read_offload(99_999)


def test_drop_messages_cleans_offload_file(tmp_path):
    store = _store(tmp_path, enabled=True, threshold_tokens=10, head_lines=1, tail_lines=1)
    body = "\n".join(f"l{i}" for i in range(50))
    mid = store.append("s1", "tool", content=body)
    rec = store.get_offload(mid)
    assert rec is not None and rec.path.exists()
    store.drop_messages("s1", [mid])
    rec_after = store.get_offload(mid)
    assert rec_after is not None
    assert rec_after.deleted is True
    assert not rec_after.path.exists()
    with pytest.raises(FileNotFoundError):
        store.read_offload(mid)


def test_drop_quarantines_when_policy_enabled(tmp_path):
    store = _store(
        tmp_path,
        enabled=True,
        threshold_tokens=10,
        head_lines=0,
        tail_lines=0,
        quarantine_on_drop=True,
    )
    body = "x" * 5_000
    mid = store.append("s1", "tool", content=body)
    rec = store.get_offload(mid)
    assert rec is not None
    store.drop_messages("s1", [mid])
    assert not rec.path.exists()
    quarantine = rec.path.parent / ".deleted"
    assert quarantine.exists()
    assert any(quarantine.iterdir())


def test_offload_emits_context_event(tmp_path):
    store = _store(tmp_path, enabled=True, threshold_tokens=10, head_lines=1, tail_lines=1)
    body = "\n".join(f"r{i}" for i in range(80))
    mid = store.append("s1", "tool", content=body)
    events = store.iter_events("s1", event_type="offload")
    assert len(events) == 1
    meta = events[0].metadata
    assert meta["message_id"] == mid
    assert meta["original_lines"] >= 80
    assert "path" in meta


def test_policy_disabled_by_default(tmp_path):
    store = ContextStore(tmp_path / "cm.db")
    assert store.get_offload_policy().enabled is False
    huge = "x" * 100_000
    mid = store.append("s1", "tool", content=huge)
    assert store.get_offload(mid) is None
    assert store.get_recent("s1")[0].content == huge


def test_concurrent_appends_each_offload(tmp_path):
    store = _store(
        tmp_path,
        enabled=True,
        threshold_tokens=10,
        head_lines=0,
        tail_lines=0,
    )
    ids: list[int] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        mid = store.append("s1", "tool", content=f"body-{idx}-" + "y" * 5_000)
        with lock:
            ids.append(mid)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ids) == 8
    records = store.iter_offloads("s1")
    assert {r.message_id for r in records} == set(ids)
    for r in records:
        assert r.path.exists()


def test_iter_offloads_returns_records_in_order(tmp_path):
    store = _store(tmp_path, enabled=True, threshold_tokens=10)
    ids = []
    for i in range(3):
        ids.append(store.append("s1", "tool", content=f"row{i}\n" * 4_000))
    out = store.iter_offloads("s1")
    assert [r.message_id for r in out] == ids
    assert all(isinstance(r, OffloadRecord) for r in out)


def test_short_payload_returns_original_when_below_head_plus_tail(tmp_path):
    from context_manager.offload import build_preview
    from pathlib import Path

    content = "a\nb\nc"
    out = build_preview(content, head_lines=10, tail_lines=10, path=Path("/x/y"))
    assert out == content


def test_offload_policy_threshold_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        OffloadPolicy(threshold_tokens=0)


def test_offload_policy_rejects_negative_head_tail(tmp_path):
    with pytest.raises(ValueError):
        OffloadPolicy(head_lines=-1)
    with pytest.raises(ValueError):
        OffloadPolicy(tail_lines=-1)


def test_oversized_single_line_payload_does_not_leak(tmp_path):
    store = _store(
        tmp_path,
        enabled=True,
        threshold_tokens=20,
        head_lines=2,
        tail_lines=2,
    )
    big = "x" * 100_000  # one massive line; line-based truncation cannot fire
    mid = store.append("s1", "tool", content=big)
    rec = store.get_offload(mid)
    assert rec is not None
    stored = store.get_recent("s1")[0].content
    assert stored is not None
    assert big not in stored, "single-line oversized payload must not be inlined"
    assert "chars" in stored or "truncated" in stored
    assert str(rec.path) in stored
    # Full original still recoverable
    assert store.read_offload(mid) == big


def test_path_traversal_session_id_is_contained(tmp_path):
    store = _store(tmp_path, enabled=True, threshold_tokens=10, head_lines=0, tail_lines=0)
    root = store.get_offload_policy().root_dir.resolve()
    # ".." should not escape the offload root
    mid = store.append("..", "tool", content="x" * 5_000)
    rec = store.get_offload(mid)
    assert rec is not None
    resolved = rec.path.resolve()
    assert str(resolved).startswith(str(root)), (
        f"offload path {resolved} escaped root {root}"
    )


def test_path_traversal_with_slashes_in_session_id(tmp_path):
    store = _store(tmp_path, enabled=True, threshold_tokens=10, head_lines=0, tail_lines=0)
    root = store.get_offload_policy().root_dir.resolve()
    mid = store.append("../etc/passwd", "tool", content="x" * 5_000)
    rec = store.get_offload(mid)
    assert rec is not None
    assert str(rec.path.resolve()).startswith(str(root))


def test_drop_by_tool_cleans_offload(tmp_path):
    store = _store(tmp_path, enabled=True, threshold_tokens=10, head_lines=1, tail_lines=1)
    mid = store.append("s1", "tool", content="z" * 5_000, tool_name="rg")
    rec = store.get_offload(mid)
    assert rec is not None and rec.path.exists()
    store.drop_by_tool("s1", "rg")
    after = store.get_offload(mid)
    assert after is not None and after.deleted is True
    assert not rec.path.exists()


def test_drop_range_cleans_offload(tmp_path):
    store = _store(tmp_path, enabled=True, threshold_tokens=10, head_lines=1, tail_lines=1)
    a = store.append("s1", "tool", content="a" * 5_000)
    b = store.append("s1", "tool", content="b" * 5_000)
    paths = [store.get_offload(mid).path for mid in (a, b)]
    store.drop_range("s1", a, b)
    for path in paths:
        assert not path.exists()
    for mid in (a, b):
        rec = store.get_offload(mid)
        assert rec is not None and rec.deleted is True


def test_commit_compaction_summary_delete_summarized_cleans_offload(tmp_path):
    store = _store(tmp_path, enabled=True, threshold_tokens=10, head_lines=1, tail_lines=1)
    a = store.append("s1", "tool", content="a" * 5_000)
    b = store.append("s1", "tool", content="b" * 5_000)
    paths = [store.get_offload(mid).path for mid in (a, b)]
    summary, watermark, rev = store.get_compaction_state("s1")
    ok = store.commit_compaction_summary(
        "s1",
        "summary text",
        b,
        rev,
        [a, b],
        delete_summarized=True,
    )
    assert ok is True
    for path in paths:
        assert not path.exists()


def test_read_offload_handles_unicode(tmp_path):
    store = _store(tmp_path, enabled=True, threshold_tokens=10, head_lines=0, tail_lines=0)
    body = "🚀漢字" * 1000  # multi-byte characters, plenty over threshold
    mid = store.append("s1", "tool", content=body)
    # Character-based slicing must align to code points, not bytes
    assert store.read_offload(mid, offset=0, limit=3) == body[:3]
    assert store.read_offload(mid, offset=1000, limit=3) == body[1000:1003]
