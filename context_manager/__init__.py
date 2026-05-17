"""context-manager — pluggable context + memory layer for agent dispatchers.

Public API:
    ContextStore       — SQLite-backed per-session message history.
    Compactor          — continuous background summarizer (STUB; awaits LLM logic).
    MemorySearch       — query facade over a MemoryBackend.
    MemoryBackend      — abstract long-term memory adapter.
    NoopMemoryBackend  — default no-op adapter.
    HermesMemoryBackend — adapter writing into Hermes's SQLite session store.
"""

from .store import ContextStore, Message
from .compactor import Compactor
from .memory import (
    MemoryBackend,
    NoopMemoryBackend,
    HermesMemoryBackend,
    MemorySearch,
)

__all__ = [
    "ContextStore",
    "Message",
    "Compactor",
    "MemorySearch",
    "MemoryBackend",
    "NoopMemoryBackend",
    "HermesMemoryBackend",
]

__version__ = "0.1.0"
