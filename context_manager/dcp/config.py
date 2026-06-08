"""DCP configuration dataclass.

Intentionally minimal for v1: one nudge threshold, one cooldown,
one token-fill trigger, per-session enable/disable, and the three
protection axes.  No per-model knobs — use the window registry
(context_manager.windows) for per-model window sizes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class DCPProtectionsConfig:
    # Tool names whose call+result pairs are never replaced by a placeholder.
    # The `compress` tool itself is always protected implicitly.
    tool_names: List[str] = field(
        default_factory=lambda: [
            "task", "skill", "todowrite", "todoread", "write", "edit", "compress",
        ]
    )
    # File-path glob patterns (matched against tool_call arguments.file_path /
    # .path / .filePath) that are protected verbatim.
    file_globs: List[str] = field(default_factory=list)
    # When True, user turns are never part of a compressed range.
    protect_user_messages: bool = False
    # Max bytes of protected-block content appended to a single summary.
    # Overflow produces a one-line stub.
    protected_append_budget: int = 32 * 1024  # 32 KiB


@dataclass
class DCPNudgeConfig:
    # Start injecting nudge reminders once active_tokens / window_size >= this.
    context_fill_threshold: float = 0.65
    # Cooldown: do not re-nudge within this many turns of the last compress call.
    cooldown_turns: int = 10
    # How many turns between repeated nudges (after the first).
    repeat_every_turns: int = 5


@dataclass
class DCPDedupeConfig:
    enabled: bool = True


@dataclass
class DCPPurgeErrorsConfig:
    enabled: bool = True
    # Replace errored tool-call arguments after this many turns.
    after_turns: int = 4


@dataclass
class DCPConfig:
    enabled: bool = True
    # Render each outbound message's _ctx_id inline as a [#N] prefix so the
    # model can reference messages by id when calling the compress tool.
    # Without this the model cannot construct a valid compress() call because
    # the start_message_id / end_message_id args have no visible source.
    render_ctx_ids: bool = True
    nudge: DCPNudgeConfig = field(default_factory=DCPNudgeConfig)
    protections: DCPProtectionsConfig = field(default_factory=DCPProtectionsConfig)
    dedupe: DCPDedupeConfig = field(default_factory=DCPDedupeConfig)
    purge_errors: DCPPurgeErrorsConfig = field(default_factory=DCPPurgeErrorsConfig)

    @classmethod
    def from_env(cls) -> "DCPConfig":
        """Load a DCPConfig from environment variables (all optional)."""
        enabled = os.environ.get("DCP_ENABLED", "1") == "1"
        render_ids = os.environ.get("DCP_RENDER_CTX_IDS", "1") == "1"
        fill = float(os.environ.get("DCP_FILL_THRESHOLD", "0.65"))
        cooldown = int(os.environ.get("DCP_COOLDOWN_TURNS", "10"))
        repeat = int(os.environ.get("DCP_REPEAT_EVERY_TURNS", "5"))
        purge_after = int(os.environ.get("DCP_PURGE_AFTER_TURNS", "4"))
        protect_user = os.environ.get("DCP_PROTECT_USER_MESSAGES", "0") == "1"
        return cls(
            enabled=enabled,
            render_ctx_ids=render_ids,
            nudge=DCPNudgeConfig(
                context_fill_threshold=fill,
                cooldown_turns=cooldown,
                repeat_every_turns=repeat,
            ),
            protections=DCPProtectionsConfig(
                protect_user_messages=protect_user,
            ),
            purge_errors=DCPPurgeErrorsConfig(
                after_turns=purge_after,
            ),
        )
