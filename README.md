# context-manager

Pluggable context + memory substrate for agent dispatchers.

`context-manager` gives dispatchers durable ownership of conversation state
outside any single model backend. It is designed for routers that may switch
or rotate backend sessions while preserving one backend-agnostic context log,
token budget view, selective deletion API, and optional compaction/memory
adapters.

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

## Inspecting & dropping (v0.2)

`ContextStore` provides per-session listing and selective hard-delete:

```python
# List
for m in store.iter_messages(sid, kind="all", offset=0, limit=20):
    print(m.id, m.role, m.kind, m.text_preview)

# Token budget
store.set_model(sid, "opus-4.7")
u = store.token_usage(sid)
print(u.active_tokens, u.window_size, u.window_pct, u.calibrated)

# Hard-DELETE (irreversible)
store.drop_messages(sid, [3, 5, 8])
store.drop_by_tool(sid, "search")     # only matches messages.tool_name col
store.drop_range(sid, from_id=10, to_id=20)
```

Design + edge cases: `docs/design/listing-and-drop-api.md`.

## Tool-result offload (opt-in)

Spill oversized tool results to disk so a single 50KB `cargo build` or
repo-wide `rg` dump cannot poison the next prompt. Stored content is
replaced with a `head + truncation marker + tail + path` preview; the
full payload stays on disk and is readable on demand.

```python
from context_manager import ContextStore, OffloadPolicy

store = ContextStore("~/.agent-dispatcher/context.db")
store.set_offload_policy(OffloadPolicy(enabled=True, threshold_tokens=4000))
mid = store.append("chat-42:thread-7", "tool", content=huge_tool_output)
preview = store.get_recent("chat-42:thread-7")[-1].content  # short head+tail+path
full   = store.read_offload(mid)                            # full original
```

`store.drop_messages([...])` also removes (or quarantines) the offload file.

## Sidecar (optional)

Non-Python hosts can use `context-manager` and DCP through a local HTTP
sidecar over a Unix domain socket. Core installs remain dependency-free; install
the optional sidecar extra when you need the daemon:

```bash
pip install -e '.[sidecar]'
context-manager-sidecar \
  --socket "${XDG_RUNTIME_DIR:-$HOME/.local/run}/ctxmgr/ctxmgr.sock" \
  --db "$HOME/.local/share/ctxmgr/ctxmgr.db"
```

Endpoints are versioned under `/v1`:

- `GET /v1/healthz`
- `POST /v1/sessions/{sid}/append`
- `POST /v1/sessions/{sid}/build_outbound`
- `POST /v1/sessions/{sid}/compress`
- `GET /v1/sessions/{sid}/usage`
- `POST /v1/sessions/{sid}/set_model`
- `GET /v1/sessions/{sid}/placeholders`
- `POST /v1/sessions/{sid}/placeholders/{pid}/deactivate`
- `GET /v1/sessions/{sid}/parent_summary`

Design details and the one-sidecar-per-host model are documented in
`docs/design/sidecar-architecture.md`. A sample systemd user unit is in
`etc/systemd/context-manager-sidecar.service`.

## MCP server (optional)

Hosts that only support MCP tools, such as Claude Code or Goose, can use the
sidecar through a stdio MCP server:

```bash
pip install -e '.[sidecar,mcp]'  # MCP extra requires Python 3.10+
context-manager-sidecar \
  --socket "${XDG_RUNTIME_DIR:-$HOME/.local/run}/ctxmgr/ctxmgr.sock" \
  --db "$HOME/.local/share/ctxmgr/ctxmgr.db"
context-manager-mcp
```

The MCP server exposes tools such as `ctx_health`, `ctx_append`,
`ctx_build_outbound`, `compress`, `ctx_usage`, `ctx_list_placeholders`, and
`ctx_deactivate_placeholder`. This is a **lossy integration**: MCP tools cannot
replace the host's native provider request automatically, so automatic DCP
placeholder substitution requires host-specific request-transform hooks.

## Public API

| Symbol | What |
| --- | --- |
| `ContextStore` | SQLite per-session message store (append / get_recent / summary / pop_last_n / reset). |
| `Compactor` | Continuous background summarizer with pluggable async `summarize_fn`. |
| `MemoryBackend` | Abstract base for long-term memory adapters. |
| `NoopMemoryBackend` | Default no-op adapter. Always safe. |
| `HermesMemoryBackend` | Writes mirrored turns into `~/.hermes/state.db`. |
| `MemorySearch` | Query facade over a `MemoryBackend`. |
| `DCPConfig`, `DCPMiddleware`, `CompressTool` | Dynamic Context Pruning integration primitives. |

## Status

| Component | Status |
| --- | --- |
| `ContextStore` | functional |
| `NoopMemoryBackend` | functional |
| `HermesMemoryBackend` | functional (write + FTS search) |
| `Compactor` | functional lifecycle + watermark-safe pluggable summarization; backend LLM call supplied by caller |
| `MemorySearch` | functional facade |

## Design

See `docs/design/` for implementation-facing design notes. The current roadmap
for strengthening context and memory management is
`docs/design/context-memory-roadmap.md`.

Key commitments:

- Dispatcher owns context; backends are stateless workers.
- SQLite schema is a compatible subset of Hermes's `messages` so a Hermes
  adapter can mirror rows without lossy translation.
- Compactor is **continuous**, not threshold-triggered. Last N messages are
  ALWAYS kept verbatim — no "Dory" surprises.
- Memory adapter is pluggable. Hermes is one adapter, not a hard dep.

## License

MIT.
