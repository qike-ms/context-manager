"""Token estimation with per-backend calibration.

Why this exists
---------------
Spike 4 (qike-ms/my-ai-skills#5) measured tokenizer drift between a single
`tiktoken.cl100k_base` baseline and the actual `usage.input_tokens` reported
by three backends on real long sessions:

    backend    median delta drift   median full-prompt drift   n
    cc         +39.6%               +98.9%                     41 / 73
    oc         +8.8%                +74.5%                     49 / 58
    hermes     n/a (no usage data persisted)                   --

Both CC's delta drift (~40%) and OC's full-prompt drift (~75%) exceed the
15% threshold from the spike brief, so a single cl100k tokenizer is
insufficient. This module applies per-backend correction factors derived
from the fixture in ``tests/fixtures/drift_pairs.json``.

Public API
----------
- ``estimate_tokens(text, backend="cc")`` -> int
- ``estimate_messages_tokens(messages, backend="cc")`` -> int
- ``CORRECTION_FACTORS`` and ``OVERHEAD_TOKENS`` (per backend, see docs)
- ``set_correction(backend, factor, overhead)`` for runtime overrides

This module has one optional dependency: ``tiktoken``. If it is missing,
estimation falls back to a char/4 heuristic and a warning is logged once.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Iterable, Mapping, Sequence, Union

log = logging.getLogger(__name__)

# Per-backend median ratios (actual_provider_tokens / cl100k_estimate)
# measured by tools/measure_drift.py on real sessions. See
# docs/tokenizer-drift.md for the full table and methodology.
CORRECTION_FACTORS: dict[str, float] = {
    "cc": 1.40,      # Anthropic Claude (CC dumps) — delta-mode median 1.3965
    "oc": 1.09,      # OpenAI Codex (OC dumps)    — delta-mode median 1.0881
    "hermes": 1.10,  # Mixed providers; conservative default until usage is persisted
    "default": 1.10,
}

# Per-backend fixed overhead (system prompt + tool schemas) in tokens.
# Applied once per request (full-prompt mode), not per message. Derived from
# the gap between full-prompt and delta drift in the fixture.
OVERHEAD_TOKENS: dict[str, int] = {
    "cc": 4500,
    "oc": 6500,
    "hermes": 2000,
    "default": 2000,
}

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
    enc = _encoder()
    if enc is False or enc is None:
        # Heuristic fallback: ~4 chars/token for English-ish content.
        return max(1, math.ceil(len(text or "") / 4))
    return len(enc.encode(text or "", disallowed_special=()))


def _norm_backend(backend: str | None) -> str:
    if not backend:
        return "default"
    b = backend.strip().lower()
    aliases = {
        "claude": "cc", "claude-code": "cc", "anthropic": "cc",
        "codex": "oc", "openai": "oc", "opencode": "oc",
    }
    return aliases.get(b, b if b in CORRECTION_FACTORS else "default")


def estimate_tokens(
    text: Union[str, Sequence[Mapping]],
    backend: str = "default",
    *,
    include_overhead: bool = False,
) -> int:
    """Estimate prompt-input tokens for ``text`` on the given ``backend``.

    Args:
        text: Either a string, or a sequence of message dicts (``{"role","content"}``).
              For message lists, content blocks (Anthropic-style ``[{type,text,...}]``)
              are flattened.
        backend: One of ``"cc"``, ``"oc"``, ``"hermes"``, or aliases
                 (``"claude"``, ``"codex"``, ...). Unknown values map to ``"default"``.
        include_overhead: If True, add ``OVERHEAD_TOKENS[backend]`` to model the
                          per-request system-prompt + tool-schema overhead.
                          Use this when the input represents a full conversation
                          being sent to the provider; leave False for incremental
                          (single-message) accounting.

    Returns:
        Integer token estimate, calibrated by per-backend correction factor.
    """
    if isinstance(text, str):
        raw = _cl100k(text)
    else:
        raw = estimate_messages_tokens_raw(text)
    b = _norm_backend(backend)
    factor = CORRECTION_FACTORS.get(b, CORRECTION_FACTORS["default"])
    out = int(round(raw * factor))
    if include_overhead:
        out += OVERHEAD_TOKENS.get(b, OVERHEAD_TOKENS["default"])
    return out


def estimate_messages_tokens(
    messages: Iterable[Mapping],
    backend: str = "default",
    *,
    include_overhead: bool = False,
) -> int:
    """Estimate tokens for an iterable of message dicts.

    Each message contributes its flattened text plus a small per-message
    framing constant (4 tokens, OpenAI's documented per-message overhead).
    """
    raw = estimate_messages_tokens_raw(messages)
    b = _norm_backend(backend)
    factor = CORRECTION_FACTORS.get(b, CORRECTION_FACTORS["default"])
    out = int(round(raw * factor))
    if include_overhead:
        out += OVERHEAD_TOKENS.get(b, OVERHEAD_TOKENS["default"])
    return out


def estimate_messages_tokens_raw(messages: Iterable[Mapping]) -> int:
    total = 0
    for m in messages or []:
        if not isinstance(m, Mapping):
            continue
        total += 4  # per-message framing
        total += _cl100k(_flatten_content(m.get("content")))
        # Role tokens are tiny; folded into the framing constant above.
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


def set_correction(backend: str, factor: float | None = None,
                   overhead: int | None = None) -> None:
    """Override a backend's calibration at runtime (for tests / new measurements)."""
    b = _norm_backend(backend)
    if factor is not None:
        CORRECTION_FACTORS[b] = float(factor)
    if overhead is not None:
        OVERHEAD_TOKENS[b] = int(overhead)


__all__ = [
    "estimate_tokens",
    "estimate_messages_tokens",
    "estimate_messages_tokens_raw",
    "set_correction",
    "CORRECTION_FACTORS",
    "OVERHEAD_TOKENS",
]
