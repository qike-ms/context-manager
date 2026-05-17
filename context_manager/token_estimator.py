"""Token estimation with per-backend calibration.

Why this exists
---------------
Spike 4 (qike-ms/my-ai-skills#5) measured tokenizer drift between a single
``tiktoken.cl100k_base`` baseline and the actual ``usage.input_tokens``
reported by three backends on 5 real long sessions per backend:

    backend  uncalibrated median drift    fit       calibrated median err
    cc       +98.7%                       slope=0.096, intercept=48_784   5.3%
    oc       +54.9%                       slope=1.258, intercept=15_158   4.8%
    hermes   n/a (no usage data persisted; provisional)

Uncalibrated ``cl100k_base`` exceeds the 15% drift bar from the spike brief
on both CC and OC, so per-backend calibration is required. We model

    actual_tokens ≈ slope * cl100k(text) + intercept

per backend. ``slope`` captures the tokenizer ratio AND the contribution of
prompt content to overall billing; ``intercept`` captures the system prompt
+ tool schemas + cached prefix that aren't visible in the session log.
Numbers come from ``tools/measure_drift.py`` on real sessions; see
``docs/tokenizer-drift.md`` for methodology and the full drift table.

Public API
----------
- ``estimate_tokens(text, backend="cc")`` -> int
- ``estimate_messages_tokens(messages, backend="cc")`` -> int
- ``CALIBRATION`` (per-backend ``(slope, intercept, measured)``)
- ``set_calibration(backend, slope, intercept)`` for runtime overrides
  (raises ``ValueError`` on unknown backend — no silent ``default`` poisoning)
- legacy ``CORRECTION_FACTORS`` / ``OVERHEAD_TOKENS`` kept for back-compat
  but ``CALIBRATION`` is the authoritative source

Optional dependency: ``tiktoken``. If unavailable, falls back to a char/4
heuristic; ``estimate_tokens("")`` still returns 0 in fallback mode.
"""
from __future__ import annotations

import logging
import math
from typing import Iterable, Mapping, NamedTuple, Sequence, Union

log = logging.getLogger(__name__)


class Calibration(NamedTuple):
    slope: float
    intercept: int
    measured: bool  # False ⇒ provisional / synthetic

    def predict(self, cl100k_count: int) -> int:
        return max(0, int(round(self.slope * cl100k_count + self.intercept)))


# Per-backend (slope, intercept) measured by tools/measure_drift.py via OLS
# regression of ``actual_input_tokens`` vs ``cl100k(text)`` on real sessions.
# Numbers reflect the fixture committed at tests/fixtures/drift_pairs.json.
CALIBRATION: dict[str, Calibration] = {
    "cc":     Calibration(slope=0.096, intercept=48784, measured=True),
    "oc":     Calibration(slope=1.258, intercept=15158, measured=True),
    "hermes": Calibration(slope=1.10,  intercept=2000,  measured=False),  # provisional
    "default": Calibration(slope=1.10, intercept=2000,  measured=False),
}

# Back-compat alias views (do not mutate via these; use set_calibration).
CORRECTION_FACTORS: dict[str, float] = {k: v.slope for k, v in CALIBRATION.items()}
OVERHEAD_TOKENS: dict[str, int] = {k: v.intercept for k, v in CALIBRATION.items()}

_ENCODER = None
_ENCODER_WARNED = False


def _encoder():
    global _ENCODER, _ENCODER_WARNED
    if _ENCODER is not None:
        return _ENCODER
    try:
        import tiktoken  # type: ignore
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # pragma: no cover - optional dep
        if not _ENCODER_WARNED:
            log.warning("tiktoken unavailable (%s); using char/4 heuristic", exc)
            _ENCODER_WARNED = True
        _ENCODER = False
    return _ENCODER


def _cl100k(text: str) -> int:
    if not text:
        return 0
    enc = _encoder()
    if enc is False or enc is None:
        # Heuristic fallback: ~4 chars/token for English-ish content.
        return max(1, math.ceil(len(text) / 4))
    # disallowed_special=() so user content containing literal "<|endoftext|>"
    # doesn't raise; this matches OpenAI's documented offline-counting recipe.
    return len(enc.encode(text, disallowed_special=()))


_ALIASES = {
    "claude": "cc", "claude-code": "cc", "anthropic": "cc",
    "codex": "oc", "openai": "oc", "opencode": "oc",
}


def _norm_backend(backend: str | None) -> str:
    """Resolve aliases. Unknown backends → 'default' (silent — used by the
    public estimate APIs, where falling back is safer than raising)."""
    if not backend:
        return "default"
    b = backend.strip().lower()
    return _ALIASES.get(b, b if b in CALIBRATION else "default")


def _resolve_backend_strict(backend: str | None) -> str:
    """For set_calibration: raise on unknown so a typo can't silently
    overwrite the shared 'default' bucket."""
    if not backend:
        raise ValueError("backend is required")
    b = backend.strip().lower()
    canonical = _ALIASES.get(b, b)
    if canonical not in CALIBRATION:
        raise ValueError(
            f"unknown backend {backend!r}; known: {sorted(CALIBRATION)}"
        )
    return canonical


def estimate_tokens(
    text: Union[str, Sequence[Mapping]],
    backend: str = "default",
    *,
    include_overhead: bool = True,
) -> int:
    """Estimate prompt-input tokens for ``text`` on the given ``backend``.

    The model is ``actual ≈ slope * cl100k(text) + intercept``. ``intercept``
    captures the per-request fixed cost (system prompt + tool schemas + any
    cache prefix); pass ``include_overhead=False`` to drop it when you only
    want the marginal cost of additional content.

    Args:
        text: Either a string, or a sequence of message dicts
            (``{"role","content"}``). For message lists, Anthropic-style
            content blocks are flattened.
        backend: One of ``"cc"``, ``"oc"``, ``"hermes"``, or aliases
            (``"claude"``, ``"codex"``, ...). Unknown values map to
            ``"default"``.
        include_overhead: If True (default), include the per-request
            ``intercept``. If False, return only the slope * cl100k term.

    Returns:
        Integer token estimate, calibrated by per-backend slope+intercept.
    """
    if isinstance(text, str):
        raw = _cl100k(text)
    else:
        raw = _messages_raw(text)
    return _apply_calibration(raw, backend, include_overhead)


def estimate_messages_tokens(
    messages: Iterable[Mapping],
    backend: str = "default",
    *,
    include_overhead: bool = True,
) -> int:
    """Estimate tokens for an iterable of message dicts.

    Adds a small per-message framing constant (4 tokens, OpenAI's documented
    convention; Anthropic uses a slightly different framing but the
    per-backend ``slope`` absorbs the difference).
    """
    raw = _messages_raw(messages)
    return _apply_calibration(raw, backend, include_overhead)


def _apply_calibration(raw: int, backend: str, include_overhead: bool) -> int:
    cal = CALIBRATION.get(_norm_backend(backend), CALIBRATION["default"])
    val = cal.slope * raw
    if include_overhead:
        val += cal.intercept
    return max(0, int(round(val)))


def _messages_raw(messages: Iterable[Mapping]) -> int:
    total = 0
    for m in messages or []:
        if not isinstance(m, Mapping):
            continue
        total += 4  # per-message framing
        total += _cl100k(_flatten_content(m.get("content")))
    return total


def _flatten_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, Mapping):
                parts.append(str(b)); continue
            t = b.get("type")
            if t in ("text", "input_text", "output_text"):
                parts.append(b.get("text", ""))
            elif t == "tool_use":
                parts.append(str(b.get("name", "")))
                inp = b.get("input")
                if inp is not None:
                    import json as _json
                    parts.append(_json.dumps(inp, ensure_ascii=False))
            elif t == "tool_result":
                parts.append(_flatten_content(b.get("content")))
            else:
                import json as _json
                parts.append(_json.dumps(dict(b), ensure_ascii=False))
        return "\n".join(parts)
    return str(content)


def set_calibration(backend: str, slope: float | None = None,
                    intercept: int | None = None,
                    measured: bool | None = None) -> None:
    """Override a backend's calibration at runtime (for tests / new measurements).

    Raises ``ValueError`` on unknown backends so a typo can't silently
    overwrite the shared ``default`` calibration.
    """
    b = _resolve_backend_strict(backend)
    cur = CALIBRATION[b]
    CALIBRATION[b] = Calibration(
        slope=float(slope) if slope is not None else cur.slope,
        intercept=int(intercept) if intercept is not None else cur.intercept,
        measured=cur.measured if measured is None else bool(measured),
    )
    # Refresh back-compat views.
    CORRECTION_FACTORS[b] = CALIBRATION[b].slope
    OVERHEAD_TOKENS[b] = CALIBRATION[b].intercept


# Back-compat: older callers may use set_correction(backend, factor, overhead).
def set_correction(backend: str, factor: float | None = None,
                   overhead: int | None = None) -> None:
    """Deprecated alias for ``set_calibration(backend, slope=factor, intercept=overhead)``."""
    set_calibration(backend, slope=factor, intercept=overhead)


__all__ = [
    "Calibration",
    "CALIBRATION",
    "CORRECTION_FACTORS",
    "OVERHEAD_TOKENS",
    "estimate_tokens",
    "estimate_messages_tokens",
    "set_calibration",
    "set_correction",
]
