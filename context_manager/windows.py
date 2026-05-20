"""Token-window registry for known model ids.

Flat in-code dict keyed by model id. See design doc
docs/design/listing-and-drop-api.md §5.
"""
from __future__ import annotations

from typing import Optional, Tuple

# Context-window sizes in tokens. Source: vendor docs as of 2026-05.
_WINDOWS: dict[str, int] = {
    # Anthropic
    "opus-4.7": 200_000,
    "sonnet-4.5": 1_000_000,
    "sonnet-4": 200_000,
    "haiku-3.5": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-5": 400_000,  # TODO: confirm at GA
    # Google
    "gemini-2.0-pro": 2_000_000,
    "gemini-2.5-flash": 1_000_000,
}
_DEFAULT = 128_000


def get_window(model: Optional[str]) -> Tuple[int, bool]:
    """Return (window_size_tokens, known) for a model id.

    Match order, case-insensitive:
      (a) exact key match,
      (b) longest-prefix match (longest key first → "sonnet-4.5-20260301"
          hits "sonnet-4.5", not "sonnet-4").
    Unknown → (_DEFAULT, False).
    """
    if not model:
        return _DEFAULT, False
    norm = model.strip().lower()
    if not norm:
        return _DEFAULT, False
    if norm in _WINDOWS:
        return _WINDOWS[norm], True
    for key in sorted(_WINDOWS.keys(), key=len, reverse=True):
        if norm.startswith(key):
            return _WINDOWS[key], True
    return _DEFAULT, False
