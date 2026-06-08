"""Tool-result offload policy: head+tail preview + filesystem spill.

When a single appended message exceeds the configured token threshold, the
full body is written to a session-scoped file and the stored ``content`` is
replaced with a ``head + truncation marker + tail + path`` preview. Callers
can later read slices of the original via :meth:`ContextStore.read_offload`.

Design:
- Opt-in via :class:`OffloadPolicy` (``enabled=False`` by default).
- One file per offloaded message: ``{root_dir}/{session_id}/msg_{message_id}.txt``.
- Provenance recorded in a new ``offload_records`` table.
- Each offload emits a ``context_event`` of type ``"offload"`` (Phase 1
  audit trail).
- Hard-delete via :meth:`ContextStore.drop_messages` also removes the
  offload file (move to ``.deleted/`` quarantine when ``quarantine=True``).
- Plain UTF-8 text only; binary tool results are out of scope for v1.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


DEFAULT_OFFLOAD_ROOT = Path("~/.context-manager/offload").expanduser()
TRUNCATION_MARKER = "... [{lines} lines truncated, full content at {path}] ..."
DEFAULT_THRESHOLD_TOKENS = 4000
DEFAULT_HEAD_LINES = 5
DEFAULT_TAIL_LINES = 5


@dataclass
class OffloadPolicy:
    """Configuration controlling on-append offload behavior.

    Attributes:
        threshold_tokens: messages whose ``token_estimate`` (or computed
            estimate) meets/exceeds this value are offloaded.
        head_lines: how many leading lines to keep inline.
        tail_lines: how many trailing lines to keep inline.
        root_dir: base directory for offload files. ``{session_id}/`` is
            appended automatically.
        enabled: if ``False`` (default) the policy is inert; callers can
            still ``read_offload`` historical records.
        quarantine_on_drop: if ``True``, files are moved to ``.deleted/``
            instead of unlinked when the owning message is hard-deleted.
    """

    threshold_tokens: int = DEFAULT_THRESHOLD_TOKENS
    head_lines: int = DEFAULT_HEAD_LINES
    tail_lines: int = DEFAULT_TAIL_LINES
    root_dir: Path = field(default_factory=lambda: DEFAULT_OFFLOAD_ROOT)
    enabled: bool = False
    quarantine_on_drop: bool = False

    def __post_init__(self) -> None:
        if self.threshold_tokens <= 0:
            raise ValueError("threshold_tokens must be positive")
        if self.head_lines < 0 or self.tail_lines < 0:
            raise ValueError("head_lines/tail_lines must be non-negative")
        if not isinstance(self.root_dir, Path):
            self.root_dir = Path(self.root_dir)
        self.root_dir = self.root_dir.expanduser()


@dataclass
class OffloadRecord:
    """One offloaded message's provenance."""

    message_id: int
    path: Path
    original_tokens: int
    original_lines: int
    original_chars: int
    created_at: float
    deleted: bool = False

    def to_json(self) -> str:
        return json.dumps(
            {
                "message_id": self.message_id,
                "path": str(self.path),
                "original_tokens": self.original_tokens,
                "original_lines": self.original_lines,
                "original_chars": self.original_chars,
                "created_at": self.created_at,
                "deleted": self.deleted,
            }
        )


def session_dir(policy: OffloadPolicy, session_id: str) -> Path:
    """Return the per-session offload directory (not yet created).

    The session id is reduced to ``Path(...).name`` after replacing path
    separators and NULs so that values like ``"../etc"``, ``".."``, or
    ``"./.deleted"`` cannot escape ``policy.root_dir``. The resolved
    directory is also re-checked against the resolved root.
    """
    candidate = session_id.replace(os.sep, "_").replace("\x00", "_")
    if os.altsep:
        candidate = candidate.replace(os.altsep, "_")
    safe = Path(candidate).name or "_"
    if safe in (".", ".."):
        safe = "_"
    root = Path(policy.root_dir).resolve()
    target = (root / safe).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:  # pragma: no cover - defence in depth
        raise ValueError(f"session_id {session_id!r} escapes offload root") from exc
    return Path(policy.root_dir) / safe


def offload_file_path(policy: OffloadPolicy, session_id: str, message_id: int) -> Path:
    return session_dir(policy, session_id) / f"msg_{int(message_id)}.txt"


def build_preview(
    content: str,
    *,
    head_lines: int,
    tail_lines: int,
    path: Path,
    head_chars: Optional[int] = None,
    tail_chars: Optional[int] = None,
) -> str:
    """Render the inline ``head + marker + tail + path`` preview.

    Two truncation modes apply, in order:

    1. **Line-based:** when the content has more than ``head_lines + tail_lines``
       lines, keep the first ``head_lines`` and last ``tail_lines`` lines and
       insert the truncation marker between them.
    2. **Character-based fallback:** if line-based truncation does not fire
       (single huge line, or fewer total lines than head+tail) but the body
       still exceeds the character budget implied by the policy, fall back to
       a head/tail character slice. This guarantees the preview never inlines
       a 100KB single-line tool result.

    For short payloads whose total size is below both budgets, the original
    content is returned unchanged.
    """
    head_char_budget = head_chars if head_chars is not None else head_lines * 200
    tail_char_budget = tail_chars if tail_chars is not None else tail_lines * 200
    char_budget = head_char_budget + tail_char_budget
    lines = content.splitlines()
    total = len(lines)
    if total > head_lines + tail_lines:
        head = lines[:head_lines] if head_lines else []
        tail = lines[-tail_lines:] if tail_lines else []
        truncated = total - len(head) - len(tail)
        marker = TRUNCATION_MARKER.format(lines=truncated, path=str(path))
        parts = []
        if head:
            parts.append("\n".join(head))
        parts.append(marker)
        if tail:
            parts.append("\n".join(tail))
        return "\n".join(parts)
    # Line-based truncation did not fire — fall back to char-based truncation
    # to guarantee oversized single-line / dense payloads still get a preview.
    if char_budget and len(content) > char_budget:
        head = content[:head_char_budget] if head_char_budget else ""
        tail = content[-tail_char_budget:] if tail_char_budget else ""
        truncated_chars = len(content) - len(head) - len(tail)
        marker = TRUNCATION_MARKER.format(
            lines=f"{truncated_chars} chars",
            path=str(path),
        )
        parts = []
        if head:
            parts.append(head)
        parts.append(marker)
        if tail:
            parts.append(tail)
        return "\n".join(parts)
    return content


def write_offload_file(path: Path, content: str) -> None:
    """Atomically write the full payload to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def read_offload_slice(
    path: Path,
    offset: int = 0,
    limit: Optional[int] = None,
) -> str:
    """Read ``[offset, offset+limit)`` **characters** from the offload file.

    Implemented as a full-text read with character-aware slicing so
    multi-byte UTF-8 payloads are sliced correctly. For very large
    offload files where memory is a concern, callers should chunk reads
    explicitly.

    Raises ``FileNotFoundError`` if the file has been removed.
    """
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative or None")
    text = path.read_text(encoding="utf-8")
    if limit is None:
        return text[offset:]
    return text[offset : offset + int(limit)]


def delete_offload_file(
    path: Path,
    *,
    quarantine: bool = False,
    quarantine_dir: Optional[Path] = None,
) -> bool:
    """Remove or quarantine the file. Returns True if it existed.

    When ``quarantine=True``, the file is moved to
    ``{quarantine_dir or path.parent}/.deleted/{path.name}`` (timestamp
    suffix added on collisions).
    """
    if not path.exists():
        return False
    if not quarantine:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True
    base = (quarantine_dir or path.parent) / ".deleted"
    base.mkdir(parents=True, exist_ok=True)
    dest = base / path.name
    if dest.exists():
        import time as _time
        dest = base / f"{path.stem}.{int(_time.time()*1000)}{path.suffix}"
    shutil.move(str(path), str(dest))
    return True
