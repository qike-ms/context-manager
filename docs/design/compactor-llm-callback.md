# Compactor LLM Callback Design

## Goal

Replace the `Compactor._compact_one` stub with a minimal, testable compaction pass: summarize newly-eligible session messages through a caller-supplied async `summarize_fn`, preserve unsummarized messages verbatim, and reassemble future context as summary + messages after a persisted watermark.

## Scope

Library-only. `context-manager` does not call OpenCode/Claude/Codex/Hermes directly. `agent-dispatcher` supplies backend-specific `summarize_fn` later.

## API

Existing constructor remains:

```python
SummarizeFn = Callable[[list[Message], str | None], Awaitable[str]]
Compactor(store, summarize_fn, config)
```

Config additions:

```python
@dataclass
class CompactorConfig:
    enabled: bool = False
    keep_verbatim_n: int = 20
    idle_interval_sec: float = 30.0
    min_messages_to_summarize: int = 40
    delete_summarized: bool = False
```

`delete_summarized=False` is default: compaction should not surprise-delete debug history.

## Store invariants

Add session columns:

- `summary_through_message_id INTEGER NULL`
- `summary_revision INTEGER NOT NULL DEFAULT 0`

`summary_through_message_id` says which live message id the summary covers through.
`summary_revision` increments whenever summary/watermark are changed or invalidated.

All summary invalidation paths (`drop_messages`, `drop_by_tool`, `drop_range`, `reset`, `pop_last_n`) must atomically set:

```sql
summary = NULL,
summary_through_message_id = NULL,
summary_revision = summary_revision + 1
```

Change `assemble_context(session_id, recent_n)`:

- If no summary: current behavior (`get_recent(recent_n)`).
- If summary + watermark: emit summary system message + all **live** messages with `id > summary_through_message_id`.
- If summary but no watermark (old DB): fallback to current summary + `get_recent(recent_n)`.
- If watermark but no summary: ignore watermark and use no-summary behavior. This should only happen during migration/invalidation bugs, but prevents context loss.

## Algorithm

For `session_id`:

1. Read compact state: `prior_summary`, `prior_watermark`, `prior_revision`.
2. If `prior_summary is None`, ignore `prior_watermark` defensively and read all live rows.
3. If `prior_summary is not None` but `prior_watermark is NULL` (old DB), treat it as no-summary for compaction: pass all live rows to `summarize_fn` with `prior_summary` as context, then write a valid watermark. This bootstraps migrated sessions into the new invariant.
4. Else read only unsummarized live messages: rows where `id > prior_watermark`.
5. If `len(delta) < min_messages_to_summarize`, no-op.
5. Split delta:
   - `head = delta[:-keep_verbatim_n]`
   - `tail = delta[-keep_verbatim_n:]`
6. If `head` is empty, no-op.
7. `new_watermark = head[-1].id`; if missing, no-op and log warning.
8. Call `new_summary = await summarize_fn(head, prior_summary)`.
9. Validate non-empty summary.
10. Atomic writeback with guard:
   - Re-read `summary_revision` and ensure it still equals `prior_revision`.
   - Ensure all `head` ids still exist **and are live** (`dropped_at IS NULL`).
   - If guard fails, abort and requeue once.
   - Else set `summary=new_summary`, `summary_through_message_id=new_watermark`, increment `summary_revision`.
11. If `delete_summarized=True`, delete rows `id <= new_watermark` using an internal helper that does **not** clear summary/watermark. Public user-deletion APIs still clear summary/watermark because user deletion invalidates summaries.

## Concurrency rule

Rows appended while `summarize_fn` is running have `id > new_watermark`, so `assemble_context()` includes them. Rows hard-dropped, reset, or soft-deleted while summarizing either increment `summary_revision` or fail the live-id guard, so stale summaries are not committed.

## Prompt guidance for caller summarize_fn

README should recommend OpenCode's structured schema:

- Goal
- Constraints
- Progress
- Decisions
- Next Steps
- Relevant Files

## Queue behavior

- Existing worker stays.
- Deduplicate queued session ids to avoid backlogs from chatty sessions.
- On guarded writeback failure, requeue the session once.

## Tests

Add `tests/test_compactor.py`:

1. disabled compactor does not call summarize.
2. below delta threshold no-ops.
3. first compaction passes first delta head + prior `None`.
4. stores summary, watermark, increments revision.
5. `assemble_context()` after compaction includes summary + all live rows after watermark.
6. appending after compaction does not drop old tail row.
7. second compaction passes only rows after prior watermark, with prior summary.
8. appending during `summarize_fn` is included because id > watermark.
9. concurrent hard-drop during `summarize_fn` aborts writeback.
10. concurrent soft-delete/reset/pop during `summarize_fn` aborts writeback.
11. invalidation clears both summary and watermark.
12. empty summary does not replace prior summary/watermark.
13. optional `delete_summarized=True` deletes summarized head but keeps summary/watermark.
14. queue dedup: multiple `note_append(sid)` before worker runs compacts once.

## Non-goals

- No backend HTTP calls.
- No DCP placeholder-swap.
- No mission-aware scoring.
- No automatic irreversible deletion by default.
- No model-window trigger logic; dispatcher decides when to enqueue.
