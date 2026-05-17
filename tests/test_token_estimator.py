"""Tests for context_manager.token_estimator.

Two layers:
1. Unit behavior — calibration factors, message flattening, overhead, fallbacks.
2. Drift validation — load tests/fixtures/drift_pairs.json (committed snapshot
   harvested by tools/measure_drift.py from real CC/OC sessions) and assert that
   the calibrated estimator's MEDIAN absolute drift stays under per-backend
   thresholds. Synthetic Hermes rows are excluded from the drift assertion.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

import pytest

from context_manager import token_estimator as te

FIXTURE = Path(__file__).parent / "fixtures" / "drift_pairs.json"


def _load_fixture():
    if not FIXTURE.exists():
        pytest.skip("drift_pairs.json fixture not present; run tools/measure_drift.py")
    return json.loads(FIXTURE.read_text())


# ---------- unit behavior ----------

def test_estimate_string_zero_and_nonempty():
    assert te.estimate_tokens("") == 0
    n = te.estimate_tokens("hello world", backend="cc")
    assert n > 0


def test_backend_aliases_resolve():
    a = te.estimate_tokens("hello world hello world", backend="claude")
    b = te.estimate_tokens("hello world hello world", backend="cc")
    assert a == b


def test_correction_factor_is_applied():
    raw = te._cl100k("the quick brown fox jumps over the lazy dog " * 10)
    cc = te.estimate_tokens("the quick brown fox jumps over the lazy dog " * 10, backend="cc")
    oc = te.estimate_tokens("the quick brown fox jumps over the lazy dog " * 10, backend="oc")
    # CC factor > OC factor in our calibration
    assert cc > oc
    # And both ≈ raw * factor (within rounding)
    assert abs(cc - raw * te.CORRECTION_FACTORS["cc"]) <= 1
    assert abs(oc - raw * te.CORRECTION_FACTORS["oc"]) <= 1


def test_overhead_added_when_requested():
    t1 = te.estimate_tokens("hello", backend="cc", include_overhead=False)
    t2 = te.estimate_tokens("hello", backend="cc", include_overhead=True)
    assert t2 - t1 == te.OVERHEAD_TOKENS["cc"]


def test_messages_flatten_anthropic_blocks():
    msgs = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Hello!"},
            {"type": "tool_use", "name": "search", "input": {"q": "weather"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "Sunny"}]},
        ]},
    ]
    n = te.estimate_messages_tokens(msgs, backend="cc")
    assert n > 0
    # Per-message framing (4 tokens each) means empty-content msgs still cost > 0.
    empty = te.estimate_messages_tokens([{"role": "user", "content": ""}] * 3, backend="cc")
    assert empty >= 12 * te.CORRECTION_FACTORS["cc"] - 1


def test_set_correction_override(monkeypatch):
    original = te.CORRECTION_FACTORS["oc"]
    try:
        te.set_correction("oc", factor=2.0, overhead=999)
        assert te.CORRECTION_FACTORS["oc"] == 2.0
        assert te.OVERHEAD_TOKENS["oc"] == 999
    finally:
        te.set_correction("oc", factor=original, overhead=6500)


def test_unknown_backend_falls_back_to_default():
    n_unknown = te.estimate_tokens("hello world " * 50, backend="totally-made-up")
    n_default = te.estimate_tokens("hello world " * 50, backend="default")
    assert n_unknown == n_default


# ---------- drift validation against real-session fixture ----------

def _calibrated_pred(pair: dict) -> float:
    """What the calibrated estimator would predict, given the raw cl100k count
    that was recorded in the fixture's `estimate` field."""
    raw = pair["estimate"]
    backend = pair["backend"]
    factor = te.CORRECTION_FACTORS.get(backend, te.CORRECTION_FACTORS["default"])
    return raw * factor


def _drifts(pairs):
    out = []
    for p in pairs:
        pred = _calibrated_pred(p)
        actual = p["actual"]
        if actual <= 0 or pred <= 0:
            continue
        out.append(abs(pred - actual) / actual)
    return out


@pytest.mark.parametrize("backend,mode,max_median_drift", [
    ("cc", "delta", 0.25),   # ~5% post-calibration on observed fixture
    ("oc", "delta", 0.20),   # OC delta median ~9%; calibrated to ~0%
])
def test_calibration_reduces_drift_below_threshold(backend, mode, max_median_drift):
    data = _load_fixture()
    pairs = [p for p in data["pairs"]
             if p["backend"] == backend
             and p.get("mode") == mode
             and not p.get("synthetic")]
    if len(pairs) < 5:
        pytest.skip(f"fixture has only {len(pairs)} {backend}/{mode} pairs; need 5+")
    drifts = _drifts(pairs)
    median = statistics.median(drifts)
    assert median <= max_median_drift, (
        f"{backend}/{mode}: calibrated median drift {median:.1%} exceeds "
        f"threshold {max_median_drift:.1%} on {len(drifts)} pairs"
    )


def test_uncalibrated_cl100k_exceeds_15pct_drift_for_cc():
    """Sanity: the WHOLE point of this module — bare cl100k drifts >15% for CC."""
    data = _load_fixture()
    pairs = [p for p in data["pairs"]
             if p["backend"] == "cc" and p.get("mode") == "delta"
             and not p.get("synthetic")]
    if len(pairs) < 5:
        pytest.skip("not enough cc/delta pairs")
    raw_drifts = [abs(p["estimate"] - p["actual"]) / p["actual"] for p in pairs]
    assert statistics.median(raw_drifts) > 0.15, (
        "Bare cl100k drift on CC is no longer >15% — re-evaluate whether "
        "per-backend correction is still needed."
    )


def test_fixture_has_real_sessions_from_all_backends():
    data = _load_fixture()
    backends = {p["backend"] for p in data["pairs"]}
    assert {"cc", "oc", "hermes"}.issubset(backends), \
        f"fixture missing backends: {backends}"


# ---------- live dogfood: estimator vs Hermes session ----------

def test_estimator_runs_on_current_hermes_session(tmp_path):
    """Smoke: estimator handles a plain Hermes-style .jsonl message list end-to-end."""
    msgs = [
        {"role": "user", "content": "Plan a tokenizer-drift spike."},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Sure — I'll harvest real sessions."},
        ]},
        {"role": "user", "content": "Ship it."},
    ] * 20
    n = te.estimate_messages_tokens(msgs, backend="hermes", include_overhead=True)
    assert n > 100
