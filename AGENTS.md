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
  compactor.py    # Compactor — continuous background summarizer (STUB)
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
| `compactor.py` | Continuous summarizer worker | **stub** (interface only) |
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

## What's stubbed (don't ship as production)

- `Compactor._compact_one` — needs LLM call. Awaits compaction-research cron
  `0b0bd07b6e5b` / `qike-ms/my-ai-skills#5`. Disabled by default
  (`CompactorConfig.enabled = False`).

## Testing rules

- All tests must be hermetic (use `tmp_path` for DBs).
- No live Hermes/Telegram/network calls in unit tests.
- New public API methods MUST come with a test in the same PR.

## Versioning

SemVer. Pre-1.0: breaking changes allowed on minor bumps; document in README.

## Related

- `qike-ms/agent-dispatcher` — primary consumer
- `qike-ms/my-ai-skills` — compaction-research and bridge async refactor live here
