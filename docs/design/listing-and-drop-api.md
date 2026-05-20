# Design: Per-Session Listing and Hard-Drop API for ContextStore

Status: Draft (spike5-listing-drop-api), revised round 2
Author: M5
Date: 2026-05-19

## Revision Log

- **r2 (2026-05-19)**: Address codex + claude-aws review.
  - Reconciled with existing soft-delete columns (`dropped_at`,
    `dropped_by`, `drop_batch_id`) added by `pop_last_n`. New `drop_*`
    methods coexist with them; all reads default to live rows.
  - Replaced `backend` with `model` (column already used by
    Hermes-compatible schema; `windows.py` is keyed by model id).
  - Documented `message_count` accounting for all three drop methods.
  - Documented existing `list_sessions()` and clarified §7 boundary.
  - Pinned `iter_messages` kind semantics (system rows, priority).
  - Added longest-prefix tie-break rule for `windows.py`.
  - Added explicit warning for `drop_by_tool` JSON-only blind spot.

## 1. Scope

This design adds **per-session message inspection** and **selective hard-deletion**
to `ContextStore`. The motivating use case: an interactive operator (or upstream
agent) needs to look inside a single session's history, identify cruft (failed
tool calls, oversized blobs, abandoned probe threads) and surgically delete it
to reclaim context-window budget.

`ContextStore` is **per-session by construction**: every public method
added here takes `session_id`. The store does already expose a
`list_sessions(source=None)` helper (see `context_manager/store.py:145`),
which is a thin convenience used by the existing test/maintenance paths
and predates this design. This design does **not** add or extend any
cross-session APIs; the listed five methods are all per-session.
Cross-session concerns (ranking by recency, GC of empty sessions,
multi-session search) remain the caller's problem — in our deployment
that's `agent-dispatcher`, which owns the session registry.

## 2. API Surface (5 methods)

### 2.0 Interaction with existing soft-delete columns

The store *already* carries `dropped_at`, `dropped_by`, `drop_batch_id`
on `messages`, added by `_apply_migrations` to support `pop_last_n()`
which **soft-deletes** the last N rows in a session. Those columns are
preserved as-is. The new methods in this design interact with them as
follows:

- **Reads (`iter_messages`, `token_usage`) default to LIVE rows only**,
  i.e. `WHERE dropped_at IS NULL`. The doc and tests assume this.
  A future kwarg `include_dropped=False` could relax this; out of scope
  here.
- **Hard-drop methods (`drop_messages`, `drop_by_tool`, `drop_range`)
  physically DELETE matching rows regardless of `dropped_at`** — both
  live and soft-dropped rows are removed. Rationale: a hard drop is the
  strongest possible operator intent; soft-dropped rows are already
  unreachable through normal reads, so deleting them too is harmless
  and keeps the DB lean.
- **`message_count` is maintained on LIVE deletions only.** All three
  drop methods MUST decrement `sessions.message_count` by the count of
  rows that were *live at the moment of deletion* (i.e. matched the
  drop predicate AND had `dropped_at IS NULL`). Soft-dropped rows that
  get physically removed do NOT decrement `message_count` again — they
  were already subtracted by `pop_last_n`. This invariant matches
  `pop_last_n`'s own accounting.

This makes the new methods compatible with the existing model without
forcing soft-delete to be removed.

### 2.1 Methods

```python
def iter_messages(
    self,
    session_id: str,
    kind: Literal["all", "tool_calls", "text"] = "all",
    offset: int = 0,
    limit: int = 20,
) -> list[MessageView]:
    """Return a page of MessageViews for the session, oldest-first by id.

    Only LIVE rows (dropped_at IS NULL) are returned.

    kind filter:
      - "all":   every live row (including system).
      - "tool":  rows where role='tool' OR tool_calls IS NOT NULL OR
                 tool_name IS NOT NULL OR tool_call_id IS NOT NULL.
                 (Renamed from the earlier draft's "tool_calls" — this
                  bucket includes both tool-call and tool-result rows;
                  the new name is honest about that.)
      - "text":  every other live row, INCLUDING role='system'.
                 System rows are text-ish and rare; we keep them in the
                 text bucket rather than hide them, so an operator
                 scrolling the timeline sees them.

    offset/limit are applied AFTER kind filtering, in id-ascending order.
    Returns [] for unknown session_id (no exception).
    """

def token_usage(
    self,
    session_id: str,
    model: Optional[str] = None,
) -> TokenUsage:
    """Summarize the session's current token footprint.

    - active_tokens: SUM(token_estimate) over LIVE rows only
                     (dropped_at IS NULL).
    - total_seen_or_None: SUM(token_estimate) over ALL physical rows
                          (live + soft-dropped). Distinguishes "what's
                          in the window now" from "what we've ever
                          counted". With hard-delete this is bounded
                          below by the live total only; it does NOT
                          recover hard-deleted rows.
    - window_size: from windows.get_window(model_or_session_model).
    - window_pct: active_tokens / window_size, or None if model/window
                  unknown (i.e. get_window returned known=False).
    - calibrated: True iff every LIVE row has a non-NULL token_estimate.
    - missing_estimates: count of LIVE rows with NULL token_estimate.

    Model resolution order:
      1. explicit `model` arg if provided,
      2. else `sessions.model` (existing column in the Hermes-compatible
         schema; see §4),
      3. else unknown → window_size=default, window_pct=None.
    """

def drop_messages(self, session_id: str, msg_ids: list[int]) -> int:
    """Hard DELETE the named rows. Returns number actually deleted.

    Rows belonging to other sessions are silently ignored (the session_id
    guard is part of the WHERE clause). Unknown ids are silently ignored.
    Empty msg_ids returns 0 without touching the DB.
    Decrements sessions.message_count by the number of LIVE rows
    deleted (see §2.0). Single transaction.
    """

def drop_by_tool(self, session_id: str, tool_name: str) -> int:
    """Hard DELETE every row in the session whose tool_name == tool_name.

    Matches the messages.tool_name column exactly (case-sensitive).
    Does NOT inspect tool_calls JSON — only the denormalized column.
    KNOWN BLIND SPOT: an assistant 'tool_call' emission row stores the
    tool name inside the tool_calls JSON blob and may leave tool_name
    NULL. Such rows will NOT be matched. If the operator wants to also
    purge the call-emission row, they should follow up with
    drop_messages([call_emission_id]). This is intentional — keeping
    the predicate column-only keeps semantics auditable.
    Returns rows deleted. Unknown tool_name → 0.
    Decrements sessions.message_count by LIVE-row count (see §2.0).
    """

def drop_range(self, session_id: str, from_id: int, to_id: int) -> int:
    """Hard DELETE every row in [from_id, to_id] INCLUSIVE in the session.

    If from_id > to_id, returns 0 (no-op, no error — callers may compute
    bounds dynamically; we treat empty range as a benign no-op).
    Returns rows deleted.
    Decrements sessions.message_count by LIVE-row count (see §2.0).
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
    total_seen: Optional[int]      # SUM over live+soft-dropped; None
                                   # only when no rows at all exist.
                                   # (Renamed from total_seen_or_None.)
    window_size: int
    window_pct: Optional[float]    # None if model/window unknown
    calibrated: bool
    missing_estimates: int
```

`kind` derivation rules for `MessageView`, **in priority order**
(first match wins):

1. `tool_result` if `role == 'tool'` OR `tool_call_id IS NOT NULL`.
2. `tool_call`   if `tool_calls IS NOT NULL` (assistant emitting calls).
3. `text`        otherwise.

This ordering matters for the rare row that is both a tool-result and
somehow has `tool_calls` populated — we classify by role first.

Previews are truncated server-side to keep `iter_messages` cheap. Truncation
uses byte-safe slicing on the decoded text (no mid-codepoint cuts).

## 4. Schema Changes (Additive)

Schema version bumps from 1 to 2. Migration is idempotent and runs on
store open (`ALTER TABLE ... ADD COLUMN`, which SQLite allows non-
destructively, and we already use the same pattern in
`_apply_migrations`).

```sql
ALTER TABLE messages ADD COLUMN token_estimate INTEGER NULL;
ALTER TABLE sessions ADD COLUMN model          TEXT    NULL;
```

### Why `model` (not `backend`) on `sessions`

The Hermes-compatible schema already uses `sessions.model` (see
`tests/test_store.py:92` for the cross-checked schema and
`hermes_state.py` for the upstream definition). Our core `SCHEMA`
string currently omits this column; we add it via migration so that
(a) standalone ContextStore DBs have a place to store the model,
and (b) the column name lines up with the Hermes mirror so
`HermesMemoryBackend` continues to mirror 1:1 without translation.

The `windows.py` registry is keyed by **model id** (`opus-4.7`,
`gpt-4o`, …), not by backend/provider. Using `model` as the column
name avoids the impedance mismatch the earlier draft would have
introduced.

A single session is tied to one model at a time. Putting `model` on
`messages` would duplicate it on every row and falsely imply mixed-
model windows are supported (they aren't — the window belongs to the
*current* call). Storing it once per session matches reality.

Alternative considered: stuff it into `sessions.metadata` JSON.
Rejected because (1) it would force a JSON parse on every
`token_usage()` call, and (2) we already promote first-class fields
out of metadata when they're queried hot (`source`, `user_id`,
`title`).

### Why `token_estimate` is on `messages`

It is intrinsically per-row, must be summable in SQL
(`SELECT SUM(...)`), and is set at append time by the (future)
estimator. JSON-in-metadata would defeat both.

### Coexistence with existing soft-delete columns

The existing `dropped_at`, `dropped_by`, `drop_batch_id` columns
(see `context_manager/store.py:104`) are untouched. The
schema_version migration in this PR does NOT remove them and does
NOT depend on their absence. See §2.0 for runtime semantics.

### What is NOT in this migration

No new `dropped_*` columns, no audit table, no tombstones for the
new hard-drop methods. The existing soft-delete columns continue
to serve `pop_last_n` only.

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

    Matching is case-insensitive and tries:
      (a) exact key match on the normalized model string,
      (b) longest-prefix match against registry keys. Ties are broken
          by **longest key first** — so a model 'sonnet-4.5-20260301'
          hits 'sonnet-4.5', NOT 'sonnet-4'. Implementation: sort the
          candidate keys by length descending and return the first
          that is a prefix of the normalized model.
    Unknown → (_DEFAULT, False).
    """
```

The registry is intentionally a flat dict, not a config file: it's tiny,
changes rarely, and lives in code so it's reviewable in PRs. A future
override hook (`set_window(model, size)`) can be added without breaking
the signature.

## 6. Why No NEW Soft-Delete (and how existing soft-delete is preserved)

The user explicitly opted out of *adding new* soft-delete semantics on
2026-05-19. The store's existing `dropped_at` / `dropped_by` /
`drop_batch_id` columns are kept because they back `pop_last_n()`,
which is already in use; we just don't extend that pattern to the new
operator-driven drop API.

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
| `test_drop_messages_coexist_with_soft_dropped` | Hard-deleting a soft-dropped row does NOT double-decrement message_count |
| `test_drop_messages_decrements_message_count` | Exact count delta verified |
| `test_drop_by_tool_match` | Deletes exactly the matching tool_name rows |
| `test_drop_by_tool_no_match` | Returns 0 |
| `test_drop_by_tool_leaves_call_emission_row` | Documents the JSON blind spot (negative test) |
| `test_drop_by_tool_decrements_message_count` | Counter maintained |
| `test_drop_range_inclusive` | Boundary ids both deleted |
| `test_drop_range_inverted` | from_id > to_id returns 0, deletes nothing |
| `test_drop_range_decrements_message_count` | Counter maintained |
| `test_schema_migration_idempotent` | Open v1 DB → migrates to v2, second open is no-op; existing soft-delete columns untouched |
| `test_windows_prefix_tiebreak` | 'sonnet-4.5-20260301' resolves to sonnet-4.5, not sonnet-4 |

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

### Resolved by r2

- ~~drop_by_tool and tool_calls JSON~~ → documented as a known blind
  spot in §2.1; operator can follow up with drop_messages.
- ~~system rows in iter_messages~~ → land in `text` bucket (§2.1).
- ~~kind derivation priority~~ → explicit ordering in §3.
- ~~message_count accounting on all three drop methods~~ → §2.0.
- ~~model vs backend column name~~ → use `model` (§4).
- ~~longest-prefix tie-break~~ → longest key wins (§5).
