# context-manager

Pluggable context + memory layer for agent dispatchers.

Designed to be the persistence layer for
[`qike-ms/agent-dispatcher`](https://github.com/qike-ms/agent-dispatcher) but
reusable by any agent that wants a dispatcher-owned, backend-agnostic
conversation store with an optional pluggable long-term memory.

## Install

```bash
pip install -e .[dev]
```

Python 3.9+.

## Quick start

```python
from context_manager import ContextStore, NoopMemoryBackend, MemorySearch

store = ContextStore("~/.agent-dispatcher/context.db")
sid = "chat-42:thread-7"
store.append(sid, "user", "What's the weather?")
store.append(sid, "assistant", "Sunny.")
messages = store.assemble_context(sid, recent_n=30)  # OpenAI-format list
```

## Public API

| Symbol | What |
| --- | --- |
| `ContextStore` | SQLite per-session message store (append / get_recent / summary / pop_last_n / reset). |
| `Compactor` | Continuous background summarizer (stub — awaits LLM logic). |
| `MemoryBackend` | Abstract base for long-term memory adapters. |
| `NoopMemoryBackend` | Default no-op adapter. Always safe. |
| `HermesMemoryBackend` | Writes mirrored turns into `~/.hermes/state.db`. |
| `MemorySearch` | Query facade over a `MemoryBackend`. |

## Status

| Component | Status |
| --- | --- |
| `ContextStore` | functional |
| `NoopMemoryBackend` | functional |
| `HermesMemoryBackend` | functional (write + FTS search) |
| `Compactor` | **stub** — interface complete; LLM call pending compaction-research |
| `MemorySearch` | functional facade |

## Design

See [`agent-dispatcher` architecture
doc](https://github.com/qike-ms/agent-dispatcher) for the full design rationale.

Key commitments:

- Dispatcher owns context; backends are stateless workers.
- SQLite schema is a compatible subset of Hermes's `messages` so a Hermes
  adapter can mirror rows without lossy translation.
- Compactor is **continuous**, not threshold-triggered. Last N messages are
  ALWAYS kept verbatim — no "Dory" surprises.
- Memory adapter is pluggable. Hermes is one adapter, not a hard dep.

## License

MIT.
