"""Tests for PlaceholderStore (SQLite, in-memory)."""

import sqlite3

import pytest

from context_manager.dcp.placeholders import PlaceholderStore


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA journal_mode=WAL")
    yield c
    c.close()


@pytest.fixture
def store(conn):
    return PlaceholderStore(conn)


def test_add_range_and_retrieve(store):
    ph = store.add_range("s1", "10", "20", "SUMMARY")
    active = store.active_for("s1")
    assert len(active) == 1
    assert active[0].id == ph.id
    assert active[0].span_start == "10"
    assert active[0].span_end == "20"
    assert active[0].summary == "SUMMARY"
    assert active[0].active


def test_add_message_and_retrieve(store):
    ph = store.add_message("s1", ["5", "7", "9"], "MSG SUMMARY")
    active = store.active_for("s1")
    assert len(active) == 1
    assert active[0].msg_ids == ["5", "7", "9"]
    assert active[0].kind == "message"


def test_deactivate(store):
    ph = store.add_range("s1", "1", "5", "S")
    store.deactivate(ph.id)
    active = store.active_for("s1")
    assert active == []


def test_reactivate(store):
    ph = store.add_range("s1", "1", "5", "S")
    store.deactivate(ph.id)
    store.reactivate(ph.id)
    active = store.active_for("s1")
    assert len(active) == 1


def test_session_isolation(store):
    store.add_range("s1", "1", "5", "A")
    store.add_range("s2", "10", "20", "B")
    assert len(store.active_for("s1")) == 1
    assert len(store.active_for("s2")) == 1


def test_count_active(store):
    store.add_range("s1", "1", "3", "A")
    store.add_range("s1", "5", "7", "B")
    assert store.count_active("s1") == 2
    ph = store.add_range("s1", "9", "11", "C")
    store.deactivate(ph.id)
    assert store.count_active("s1") == 2


def test_history_includes_inactive(store):
    ph = store.add_range("s1", "1", "5", "S")
    store.deactivate(ph.id)
    hist = store.history_for("s1")
    assert len(hist) == 1
    assert not hist[0].active


def test_covers_range(store):
    ph = store.add_range("s1", "10", "20", "S")
    assert ph.covers("10")
    assert ph.covers("15")
    assert ph.covers("20")
    assert not ph.covers("9")
    assert not ph.covers("21")


def test_covers_message_mode(store):
    ph = store.add_message("s1", ["5", "7", "9"], "S")
    assert ph.covers("5")
    assert ph.covers("7")
    assert not ph.covers("6")
    assert not ph.covers("10")


def test_schema_idempotent(conn):
    """Opening a second PlaceholderStore on the same conn must not fail."""
    PlaceholderStore(conn)
    PlaceholderStore(conn)  # should not raise
