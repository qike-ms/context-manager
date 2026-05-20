# Design: Per-Session Listing and Hard-Drop API for ContextStore

Status: Draft (spike5-listing-drop-api)
Author: M5
Date: 2026-05-19

## 1. Scope

This design adds **per-session message inspection** and **selective hard-deletion**
to `ContextStore`. The motivating use case: an interactive operator (or upstream
agent) needs to look inside a single session's history, identify cruft (failed
tool calls, oversized blobs, abandoned probe threads) and surgically delete it
to reclaim context-window budget.

`ContextStore` is **per-session by construction**: every public method already
takes `session_id`. Cross-session concerns (listing all sessions, ranking by
recency, GC of empty sessions, multi-session search) are explicitly the caller's
problem — in our deployment that's `agent-dispatcher`, which already owns a
session registry. We will not add `list_sessions()` or any cross-session
iterator here; doing so would conflate two ownership boundaries.

## 2. API Surface (5 methods)

All methods are added to `ContextStore`. None modify existing methods.

```python
def iter_messages(
    self,
    session_id: str,
    kind: Literal["all", "tool_calls", "text"] = "all",
    offset: int = 0,
    limit: int = 20,
) -> list[MessageView]:
    """Return a page of MessageViews for the session, oldest-first by id.

    kind filter:
      - "all":        every row
      - "tool_calls": rows where tool_name IS NOT NULL OR tool_calls IS NOT NULL
                      OR role = 'tool'
      - "text":       rows where the above is false (assistant/user text only)

    offset/limit are applied AFTER kind filtering, in id-ascending order.
    Returns [] for unknown session_id (no exception).
    """

def token_usage(
    self,
    session_id: str,
    model: Optional[str] = None,
) -> TokenUsage:
    """Summarize the session's current token footprint.

    - active_tokens: sum of token_estimate over all rows in the session.
    - total_seen_or_None: sum including dropped rows, IF we ever track that.
      With hard-delete this is always None (see §6).
    - window_size: from windows.get_window(model_or_session_backend).
    - window_pct: active_tokens / window_size, or None if window unknown.
    - calibrated: True iff every row has a non-NULL token_estimate.
    - missing_estimates: count of rows with NULL token_estimate.

    If model is None, fall back to the session's stored backend (see §4).
    If still unknown, window_size = default (128k), window_pct = None,
    and known=False is reflected via window_pct being None.
    """

def drop_messages(self, session_id: str, msg_ids: list[int]) -> int:
    """Hard DELETE the named rows. Returns number actually deleted.

    Rows belonging to other sessions are silently ignored (the session_id
    guard is part of the WHERE clause). Unknown ids are silently ignored
    — the return value tells the caller how many actually went.
    Empty msg_ids returns 0 without touching the DB.
    Also decrements sessions.message_count by the deletion count.
    """

def drop_by_tool(self, session_id: str, tool_name: str) -> int:
    """Hard DELETE every row in the session whose tool_name == tool_name.

    Matches the messages.tool_name column exactly (case-sensitive).
    Does NOT inspect tool_calls JSON — only the denormalized column.
    Returns rows deleted. Unknown tool_name → 0.
    """

def drop_range(self, session_id: str, from_id: int, to_id: int) -> int:
    """Hard DELETE every row in [from_id, to_id] INCLUSIVE in the session.

    If from_id > to_id, returns 0 (no-op, no error — callers may compute
    bounds dynamically; we treat empty range as a benign no-op).
    Returns rows deleted.
    """
```

### Design notes on the API

- All `drop_*` methods are **idempotent in effect** (running twice on the same
  ids deletes 0 the second time) but **not reversible** — see §6.
- `iter_messages` returns lightweight `MessageView`s, not full `Message`
  rows. Callers who need the full content should use the existing
  `get_messages` / `get_message_by_id` (the latter to be added in implementation
  if not present — out of scope for this design beyond noting it).
- `token_usage` is read-only and cheap (one SUM query + one COUNT query).

## 3. Dataclasses

```python
@dataclass
class MessageView:
    id: int
    role: str                      # 'user'|'assistant'|'tool'|'system'
    kind: str                      # 'tool_call'|'tool_result'|'text'
    tool_name: Optional[str]
    tool_args_preview: Optional[str]   # first 80 chars of tool_calls JSON args
    text_preview: Optional[str]        # first 120 chars of content
    token_estimate: Optional[int]      # NULL until backfilled

@dataclass
class TokenUsage:
    active_tokens: int
    total_seen_or_None: Optional[int]  # always None under hard-delete (§6)
    window_size: int
    window_pct: Optional[float]        # None if model/window unknown
    calibrated: bool
    missing_estimates: int
```

`kind` derivation rules for `MessageView`:

- `tool_call`   if `tool_calls` IS NOT NULL (assistant emitting a call)
- `tool_result` if `role == 'tool'` or `tool_call_id` IS NOT NULL
- `text`        otherwise

Previews are truncated server-side to keep `iter_messages` cheap. Truncation
uses byte-safe slicing on the decoded text (no mid-codepoint cuts).

## 4. Schema Changes (Additive)

Schema version bumps from 1 to 2. Migration is idempotent and runs on store
open (`ALTER TABLE ... ADD COLUMN`, which SQLite allows non-destructively).

```sql
ALTER TABLE messages ADD COLUMN token_estimate INTEGER NULL;
ALTER TABLE sessions ADD COLUMN backend        TEXT    NULL;
```

### Why `backend` lives on `sessions`, not `messages`

A single session is tied to one model family at a time (the agent driving the
chat). Putting `backend` on `messages` would (a) duplicate it on every row,
and (b) invite the false hope of mixed-model accounting, which token windows
don't actually support — the window belongs to the *current* call's model,
not to historical messages. Storing it once per session matches reality and
costs ~1 column-write per session lifetime.

Alternative considered: stuff it into `sessions.metadata` JSON. Rejected
because (1) it would force a JSON parse on every `token_usage()` call, and
(2) we already promote first-class fields out of metadata when they're
queried hot (`source`, `user_id`, `title`).

### Why `token_estimate` is on `messages`

It is intrinsically per-row, must be summable in SQL (`SELECT SUM(...)`),
and is set at append time by the (future) estimator. JSON-in-metadata
would defeat both.

### What is NOT in this migration

No `dropped_at`, `dropped_by`, `drop_batch_id`. Drops are hard DELETE
(see §6). No tombstones, no audit table.

## 5. Token-Window Registry (`context_manager/windows.py`)

```python
# Context-window sizes in tokens. Source: vendor docs as of 2026-05.
_WINDOWS: dict[str, int] = {
    # Anthropic
    "opus-4.7":       200_000,
    "sonnet-4.5":   1_000_000,
    "sonnet-4":       200_000,
    "haiku-3.5":      200_000,
    # OpenAI
    "gpt-4o":         128_000,
    "gpt-5":          400_000,   # TODO: confirm at GA
    # Google
    "gemini-2.0-pro": 2_000_000,
    "gemini-2.5-flash": 1_000_000,
}
_DEFAULT = 128_000

def get_window(model: Optional[str]) -> tuple[int, bool]:
    """Return (window_size_tokens, known).

    Matching is case-insensitive and tries (a) exact key, (b) longest-prefix
    match on the normalized model string (so 'opus-4.7-20260301' still hits
    'opus-4.7'). Unknown → (_DEFAULT, False).
    """
```

The registry is intentionally a flat dict, not a config file: it's tiny,
changes rarely, and lives in code so it's reviewable in PRs. A future
override hook (`set_window(model, size)`) can be added without breaking
the signature.

## 6. Why No Soft-Delete

The user explicitly opted out of soft-delete on 2026-05-19.

**Trade-off accepted:**

| Aspect            | Soft-delete                 | Hard-delete (chosen)         |
|-------------------|-----------------------------|------------------------------|
| Schema cost       | +3 cols, +1 index           | 0 extra cols                 |
| DB growth         | Unbounded (rows live on)    | Bounded by live message_count|
| Undo              | Possible (UPDATE)           | Impossible without backup    |
| Audit             | Free (row still there)      | None                         |
| Future DCP swap   | Trivial (mark as placeholder)| User-dropped rows are gone (§9)|

The user's reasoning: drops are operator-initiated, deliberate, and rare;
the operator has already decided "this is cruft." Keeping it around just
to second-guess that decision is dead weight. If we later need reversibility
(e.g. for an "oops, undo last drop" UX) that is a separate feature with its
own design doc — it would likely take the form of a recycle-bin table written
to inside `drop_*` and TTL'd, not a column on `messages`.

## 7. What Is NOT in This API

Out of scope (callers' responsibility):

- `list_sessions()` and any cross-session iteration → `agent-dispatcher`.
- Session metadata mutation beyond fields already supported by
  `ContextStore` (title, ended_at, summary). No new mutators in this PR.
- Token estimation itself — the estimator that *populates*
  `token_estimate` is tracked separately (spike4-token-estimator).
  This API only consumes the column; it does not compute it.
- Compaction / summarization — `Compactor` is unaffected.
- Long-term memory (`MemoryBackend`) — unaffected.

## 8. Test Plan

All tests hermetic via `tmp_path`. One test per public method, plus the
listed edge cases. New file: `tests/test_listing_and_drops.py`.

| Test | What it asserts |
|------|-----------------|
| `test_iter_messages_all` | Pagination, oldest-first ordering, returns MessageView shape |
| `test_iter_messages_kind_filter` | `tool_calls` and `text` kinds segregate correctly |
| `test_iter_messages_empty_session` | Unknown session_id returns `[]`, no exception |
| `test_iter_messages_offset_past_end` | offset > count returns `[]` |
| `test_token_usage_known_model` | window_pct computed, calibrated=True when all rows estimated |
| `test_token_usage_uncalibrated` | missing_estimates>0, calibrated=False, window_pct still computed from active_tokens |
| `test_token_usage_unknown_model` | window_pct is None, window_size is default |
| `test_token_usage_fallback_to_session_backend` | model=None reads sessions.backend |
| `test_drop_messages_basic` | Returns count, rows actually gone, message_count decremented |
| `test_drop_messages_unknown_id` | Unknown ids ignored, returns only-real count |
| `test_drop_messages_empty_list` | Returns 0, no DB write |
| `test_drop_messages_cross_session_isolation` | ids from another session NOT deleted |
| `test_drop_by_tool_match` | Deletes exactly the matching tool_name rows |
| `test_drop_by_tool_no_match` | Returns 0 |
| `test_drop_range_inclusive` | Boundary ids both deleted |
| `test_drop_range_inverted` | from_id > to_id returns 0, deletes nothing |
| `test_schema_migration_idempotent` | Open v1 DB → migrates to v2, second open is no-op |

## 9. Integration with Future DCP Placeholder-Swap

The Dynamic Context Pruning layer (separate, future) plans to replace
low-value messages with cheap placeholders (e.g. `"<tool result elided>"`)
to reclaim window space *reversibly*. Because the drops introduced here
are **hard DELETE**, the DCP layer will treat user-dropped messages as
non-existent — they cannot become placeholders, and DCP cannot resurrect
them. This is deliberate: user drops are a stronger signal than DCP's
heuristic pruning, and conflating them would weaken DCP's recovery
semantics.

If we later decide user-drops should be DCP-reversible, that's a new
design (see §6) and would likely add a dedicated `drops_recycle` table
rather than retrofitting soft-delete onto `messages`.

## 10. Open Questions

- **gpt-5 window size**: 400k is the rumoured GA number; confirm before
  shipping. Until confirmed, `windows.py` carries a `# TODO` comment.
- **Preview length** (80/120 chars): arbitrary. Should we make these
  parameters of `iter_messages`? Leaning no — keep the API narrow; a
  caller wanting full content can fetch the row.
- **`drop_by_tool` and `tool_calls` JSON**: do we need to also match
  rows where the tool name appears only inside the `tool_calls` JSON
  blob (i.e. the assistant's call-emission row, where `tool_name`
  column is NULL but the JSON names the tool)? Current design says no
  — keep it simple and column-driven. Flagging for reviewers.
