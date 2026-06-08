# AGENTS.md — context-manager

Dev guide for AI coding assistants and humans.

## Layout

```
context_manager/
  __init__.py     # public API surface
  store.py        # ContextStore — SQLite per-session message log
                  # + iter_messages / token_usage / drop_messages /
                  #   drop_by_tool / drop_range
  windows.py      # Token-window registry + get_window(model)
  compactor.py    # Compactor — watermark-safe background summarizer with
                  # caller-supplied async summarize_fn
  memory.py       # MemoryBackend ABC, NoopMemoryBackend, HermesMemoryBackend,
                  # MemorySearch facade
tests/
  test_store.py   # pytest — append/retrieve/summary/tool-calls/Hermes smoke
  test_listing_and_drops.py  # design §8 coverage
pyproject.toml
README.md
AGENTS.md
```

## Module map

| File | Purpose | Status |
| --- | --- | --- |
| `store.py` | SQLite append-only message store, session-keyed; per-session listing + hard-drop API | functional |
| `windows.py` | Model→context-window registry + `get_window(model)` helper | functional |
| `compactor.py` | Watermark-safe continuous summarizer worker | functional (caller supplies async `summarize_fn`) |
| `memory.py` | Long-term memory adapters | Noop+Hermes functional |

## Dev setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
pytest -q
```

Targeting Python 3.9+ (macOS system python). No third-party deps in core.

## Schema invariants

- `ContextStore` schema is a compatible subset of Hermes's `messages` table
  (see `~/.hermes/hermes-agent/hermes_state.py` → `SessionDB.append_message`).
  This is deliberate: `HermesMemoryBackend` can mirror rows 1:1.
- `session_id` is opaque text. agent-dispatcher composes it as
  `f"{chat_id}:{thread_id or 'None'}"`.

## Compactor contract

- `Compactor` owns lifecycle/queueing/watermark-safe summary writeback only.
- Callers supply backend-specific async `summarize_fn(messages, prior_summary)`.
- Store tracks `summary_through_message_id` so `assemble_context()` emits summary + every live row after the watermark; later appends cannot fall through the cracks.
- User deletions/rewinds/reset invalidate summary + watermark. Internal compactor deletion (`delete_summarized=True`) preserves summary.
- Keep last `CompactorConfig.keep_verbatim_n` live messages out of each compaction pass.

## Testing rules

- All tests must be hermetic (use `tmp_path` for DBs).
- No live Hermes/Telegram/network calls in unit tests.
- New public API methods MUST come with a test in the same PR.

## Versioning

SemVer. Pre-1.0: breaking changes allowed on minor bumps; document in README.

After changing `[project].version`, sync the primary consumer dependency:

```bash
python scripts/sync_agent_dispatcher_dependency.py
```

This updates the sibling `../agent-dispatcher/pyproject.toml` requirement from
the current `context-manager` version. Use `--check` in automation to fail on
drift without writing files.

## Related

- `qike-ms/agent-dispatcher` — primary consumer
- `qike-ms/my-ai-skills` — compaction-research and bridge async refactor live here
