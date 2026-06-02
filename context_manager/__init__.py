"""context-manager — pluggable context + memory layer for agent dispatchers.

Public API:
    ContextStore       — SQLite-backed per-session message history.
    Compactor          — continuous background summarizer (STUB; awaits LLM logic).
    MemorySearch       — query facade over a MemoryBackend.
    MemoryBackend      — abstract long-term memory adapter.
    NoopMemoryBackend  — default no-op adapter.
    HermesMemoryBackend — adapter writing into Hermes's SQLite session store.
"""

from .store import ContextEvent, ContextStore, Message, MessageView, SummaryEnvelope, TokenUsage
from .compactor import (
    Compactor,
    CompactorConfig,
    PrunePolicy,
    prune_tool_outputs,
    render_for_summary,
)
from .windows import get_window
from .memory import (
    MemoryBackend,
    NoopMemoryBackend,
    HermesMemoryBackend,
    MemorySearch,
)
from .token_estimator import (
    estimate_tokens,
    estimate_messages_tokens,
    set_calibration,
    CALIBRATION,
    CORRECTION_FACTORS,
    OVERHEAD_TOKENS,
)

__all__ = [
    "ContextStore",
    "ContextEvent",
    "Message",
    "MessageView",
    "SummaryEnvelope",
    "TokenUsage",
    "Compactor",
    "CompactorConfig",
    "PrunePolicy",
    "prune_tool_outputs",
    "render_for_summary",
    "get_window",
    "MemorySearch",
    "MemoryBackend",
    "NoopMemoryBackend",
    "HermesMemoryBackend",
    "estimate_tokens",
    "estimate_messages_tokens",
    "set_calibration",
    "CALIBRATION",
    "CORRECTION_FACTORS",
    "OVERHEAD_TOKENS",
]

__version__ = "0.1.0"
