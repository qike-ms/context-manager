"""Tests for ContextStore.reset()."""

from context_manager.store import ContextStore


def _store(tmp_path):
    return ContextStore(tmp_path / "ctx.db")


def test_reset_empty_session_returns_zero(tmp_path):
    s = _store(tmp_path)
    assert s.reset("sid") == 0


def test_reset_empty_session_records_reset_event(tmp_path):
    s = _store(tmp_path)

    assert s.reset("sid") == 0

    events = s.iter_events("sid")
    assert [e.event_type for e in events] == ["reset"]
    assert events[0].metadata["count"] == 0
    assert events[0].metadata["reason"] is None


def test_reset_nonexistent_session_auto_creates(tmp_path):
    s = _store(tmp_path)
    assert s.reset("new") == 0
    ids = [row["id"] for row in s.list_sessions()]
    assert "new" in ids


def test_reset_clears_assemble_context(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        s.append("sid", "user", f"m{i}")
    assert s.reset("sid") == 3
    assert s.assemble_context("sid") == []


def test_reset_then_append_only_new_rows(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        s.append("sid", "user", f"m{i}")
    s.reset("sid")
    for i in range(2):
        s.append("sid", "user", f"new{i}")
    msgs = s.get_recent("sid")
    assert [m.content for m in msgs] == ["new0", "new1"]


def test_reset_idempotent(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        s.append("sid", "user", f"m{i}")
    assert s.reset("sid") == 3
    assert s.reset("sid") == 0


def test_reset_preserves_dropped_rows_in_raw_table(tmp_path):
    s = _store(tmp_path)
    for i in range(4):
        s.append("sid", "user", f"m{i}")
    before = s._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    s.reset("sid")
    after = s._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert before == after == 4


def test_reset_zeros_message_count(tmp_path):
    s = _store(tmp_path)
    for i in range(5):
        s.append("sid", "user", f"m{i}")
    s.reset("sid")
    row = s._conn.execute(
        "SELECT message_count FROM sessions WHERE id=?", ("sid",)
    ).fetchone()
    assert row[0] == 0


def test_reset_does_not_affect_other_sessions(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        s.append("A", "user", f"a{i}")
        s.append("B", "user", f"b{i}")
    s.reset("A")
    assert s.assemble_context("A") == []
    assert len(s.get_recent("B")) == 3


def test_reset_records_reason_in_metadata(tmp_path):
    s = _store(tmp_path)
    s.append("sid", "user", "x")
    s.reset("sid", reason="user_command")
    meta = s.get_metadata("sid")
    assert meta["reset_history"][-1]["reason"] == "user_command"
    assert meta["reset_history"][-1]["count"] == 1


def test_reset_history_appends_not_overwrites(tmp_path):
    s = _store(tmp_path)
    s.append("sid", "user", "x")
    s.reset("sid", reason="first")
    s.append("sid", "user", "y")
    s.reset("sid", reason="second")
    meta = s.get_metadata("sid")
    reasons = [e["reason"] for e in meta["reset_history"]]
    assert reasons == ["first", "second"]


def test_reset_only_marks_live_rows_dropped_by_reset(tmp_path):
    s = _store(tmp_path)
    for i in range(4):
        s.append("sid", "user", f"m{i}")
    s.pop_last_n("sid", 1)  # marks 1 row dropped_by='rewind'
    s.reset("sid")  # marks remaining 3 dropped_by='reset'
    rows = s._conn.execute(
        "SELECT dropped_by, drop_batch_id FROM messages WHERE session_id=?",
        ("sid",),
    ).fetchall()
    by_kind = {}
    for db, bid in rows:
        by_kind.setdefault(db, set()).add(bid)
    assert "rewind" in by_kind and "reset" in by_kind
    # batch_ids segregated per kind
    assert by_kind["rewind"].isdisjoint(by_kind["reset"])
    # all reset rows share a single batch_id
    assert len(by_kind["reset"]) == 1


def test_reset_history_capped_at_10(tmp_path):
    s = _store(tmp_path)
    for i in range(12):
        s.append("sid", "user", f"m{i}")
        s.reset("sid", reason=f"r{i}")
    meta = s.get_metadata("sid")
    assert len(meta["reset_history"]) == 10
    # most recent kept
    assert meta["reset_history"][-1]["reason"] == "r11"
    assert meta["reset_history"][0]["reason"] == "r2"
