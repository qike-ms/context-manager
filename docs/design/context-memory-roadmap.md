# Context-manager Context + Memory Roadmap

Status: draft for trio review
Date: 2026-05-31
Repo: qike-ms/context-manager

## Goal

Improve context-manager into a small, reusable context substrate for multi-backend agent dispatchers.

The library should learn from mature context and memory systems while keeping its own identity:

- dispatcher-owned context, independent of backend session internals;
- backend-agnostic storage and APIs;
- caller-supplied summarizers instead of hardcoded model/provider calls;
- explicit inspection and deletion APIs;
- safe behavior under concurrent appends, rewinds, resets, drops, and compaction.

## Non-goals

- Do not become a full agent runtime.
- Do not own platform adapters, gateway routing, tool execution, or model credentials.
- Do not hardcode one agent backend's session format as the core abstraction.
- Do not make long-term memory automatic prompt injection without an inspection path.
- Do not copy AGPL code or port implementation details from incompatible projects.

## Current baseline

### What exists

- `ContextStore` SQLite message/session store.
- Per-session append, recent/full retrieval, OpenAI-format assembly, metadata, model setting, token usage, listing, reset/rewind/drop APIs.
- Model-window registry and calibrated token estimator.
- `Compactor` with async caller-provided `summarize_fn`.
- Watermark/revision-safe summary writeback.
- Last-N verbatim preservation.
- `MemoryBackend`, `NoopMemoryBackend`, `HermesMemoryBackend`, `MemorySearch` facade.

### Main weaknesses

- Tail retention is count-based, not token-budget based.
- Compaction output does not have a library-level safe summary envelope.
- Compaction has logs but no persisted event stream or metrics API.
- No standalone memory index backend.
- No memory status/index/search operational API outside the abstract facade.
- Tool-output pruning and untrusted-output exclusion are caller-specific.
- No explicit extractive memory promotion before/after compaction.

## Design principles

1. **The dispatcher owns continuity.** Backend sessions are cache/compute artifacts; context-manager stores the durable conversation state.
2. **Safety before compression.** Never turn tool output, credentials, or untrusted content into authoritative instructions.
3. **Inspectability before automation.** Every drop, compaction, memory write, and index update should be auditable.
4. **Small core, pluggable edges.** Core stays SQLite + Python stdlib where possible; richer backends are adapters.
5. **No surprise deletion.** User-initiated drops are hard delete; compactor-internal summarization can preserve raw rows by default unless caller opts into deletion.
6. **Backend-agnostic summarization.** The caller chooses the hosted or local summarizer; context-manager owns only contracts and invariants.

## Definitions

- **Turn:** one user message plus all following assistant, tool-call, and tool-result rows until the next user message.
- **System messages:** outside ordinary user turns. They stay pinned or are handled by explicit caller policy; compaction/pruning must not silently drop them as tool context.
- **Tool pair:** a tool-call row plus its corresponding tool-result row. Tail selection must keep the pair together or drop the whole pair from the verbatim tail.

## Proposed phases

## Phase 1 — Safe summary envelope and compaction events

### Add `SummaryEnvelope`

Add a small standard wrapper around stored summaries:

```python
@dataclass
class SummaryEnvelope:
    version: int
    text: str
    through_message_id: int
    safety_policy: str
    source: str
    created_at: float
```

Store as explicit columns or metadata JSON while preserving existing `summary` compatibility.

Required behavior:

- Existing raw summary strings still load.
- New summaries can be identified as library-safe.
- `get_summary()` keeps returning plain text; `get_summary_envelope()` returns the structured envelope or `None` when no summary exists.
- Callers can reject unsafe/legacy summaries before injecting them into worker history.
- Summary prefix should clearly say the summary is reference material, not active instructions.

### Add persisted compaction events

Add `context_events` table:

```sql
CREATE TABLE context_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message_id INTEGER,
  timestamp REAL NOT NULL,
  metadata TEXT
);
```

Events:

- `compaction_started`
- `compaction_completed`
- `compaction_skipped`
- `compaction_aborted_revision_changed`
- `summary_invalidated`
- `messages_dropped`
- `reset`
- `rewind`

Acceptance tests:

- Compaction success emits started+completed with watermark and count.
- Revision conflict emits aborted event and preserves prior summary.
- Drop/reset/rewind invalidate summary and emit events.
- Event reads are per-session only.

## Phase 2 — Token-budget tail selection

Replace fixed `keep_verbatim_n` as the only option with token-budget support:

```python
@dataclass
class CompactorConfig:
    keep_verbatim_n: int = 20
    keep_verbatim_tokens: Optional[int] = None
    keep_verbatim_window_ratio: float = 0.25  # fraction of configured model window
```

If both `keep_verbatim_tokens` and `keep_verbatim_window_ratio` are set,
`keep_verbatim_tokens` wins. The ratio denominator is the configured model window
for the session (`token_usage.window_size`), not current active tokens or the
compactable span.

Selection algorithm:

1. Compute live rows not covered by watermark.
2. Reserve tail by tokens from newest backward.
3. Preserve user-turn boundaries when possible.
4. If a single recent turn exceeds budget, keep the newest suffix of that turn only if it does not orphan tool-call/tool-result pairs.
5. If preserving a tool-call/tool-result pair would exceed budget, drop the whole pair from the verbatim tail and rely on the summary for that older tool context; never keep an orphaned tool call or orphaned tool result.
6. Fall back to `keep_verbatim_n` when estimates are missing.

Acceptance tests:

- Tail size respects token budget.
- Recent user correction stays verbatim when budget permits.
- Tool-call/result pairs are not split into invalid model history.
- Missing estimates degrade to message-count behavior.

## Phase 3 — Tool-output pruning and untrusted-content policy

Add optional preprocessing helpers, not mandatory policy:

```python
class PrunePolicy:
    max_tool_result_chars: int = 4000
    drop_tool_results_older_than_turns: Optional[int] = None
    preserve_tool_names: set[str] = field(default_factory=set)
```

Expose:

- `render_for_summary(messages, policy)`
- `prune_tool_outputs(messages, policy)`
- `redact_sensitive_text(text)` hook, caller-extensible; callers may chain stricter redactors, never replace or disable the default redactor.

Rules:

- Tool results are untrusted source material.
- Summarizer prompt must never present tool output as instructions.
- Credentials/secrets are always redacted before summarization, memory extraction, or memory storage. Redaction is not caller-disableable; callers may only add stricter redactors.
- Caller can opt into storing pruned placeholders or only using pruned renderings for summary input.

Acceptance tests:

- Tool result containing prompt injection is not promoted into summary instructions.
- Long tool output is capped with explicit placeholder.
- Redaction runs before summarizer callback receives text.
- Policy is deterministic and separately testable.

## Phase 4 — Standalone SQLite memory backend

Add a real memory backend that does not depend on Hermes:

```python
class SqliteMemoryBackend(MemoryBackend):
    def remember(session_key, messages, tags=None): ...
    def search(query, limit=10): ...
    def index_status(): ...
    def reindex(force=False): ...
```

For compaction-time extracted memory, extend the backend contract with staged
item writes:

```python
class MemoryBackend(Protocol):
    def stage_items(self, items: list[MemoryItem], span_id: str) -> None: ...
    def commit_staged(self, span_id: str) -> None: ...
    def abandon_staged(self, span_id: str) -> None: ...
```

Backends that do not support extracted long-term memory may implement these as
no-ops. Search must return only committed memory items; staged or abandoned
items are invisible.

V1 should use SQLite FTS5, not vectors. Keep vector embeddings as later adapter.

Tables:

- `memory_items`: durable extracted facts/summaries/snippets.
- `memory_item_fts`: FTS over title/body/tags/source.
- `memory_sources`: provenance back to session/message IDs.

Memory item schema:

```python
@dataclass
class MemoryItem:
    id: str
    kind: Literal["fact", "decision", "preference", "procedure", "artifact", "summary"]
    body: str
    source_session_id: str
    source_message_ids: list[int]
    tags: dict
    created_at: float
    confidence: float
```

Acceptance tests:

- `remember()` stores provenance.
- `stage_items()` writes extracted items as non-searchable staged rows.
- `commit_staged()` makes staged items searchable atomically for a source span.
- `abandon_staged()` prevents staged items from ever appearing in search.
- `search()` returns ranked FTS results.
- Duplicate items are deduped by content hash + source.

## Phase 5 — Memory extraction around compaction boundaries

Add optional extraction callback before compaction discards or summarizes old context:

```python
ExtractMemoryFn = Callable[[list[Message], Optional[SummaryEnvelope]], Awaitable[list[MemoryItem]]]
```

The second argument is the current summary envelope, if one exists, so extractors
can avoid re-emitting already-covered memories.

Compactor flow:

1. Select head/tail.
2. Render safe summary input.
3. Optionally extract memory from head using redacted input.
4. Stage memory items with deterministic source-span IDs via configured `MemoryBackend`.
5. Run summarizer.
6. Commit summary/event state only if the source watermark/revision still matches.
7. Finalize staged memory items after the summary commit succeeds; if the summary aborts, call `abandon_staged(span_id)` so those staged items are never returned by search and do not remain in limbo.

Rules:

- Extraction failures must not corrupt context state.
- Memory extraction must be idempotent for the same source message IDs.
- Extracted memories must include provenance and kind.
- Preferences require user-message evidence.
- No assistant/tool output is stored as user preference without explicit user-role evidence.
- Secret redaction runs before extraction callback input and before memory storage.

Acceptance tests:

- Extraction runs once per compacted source span.
- Retry after revision conflict does not duplicate memories.
- User preference extraction ignores assistant-only text.
- Memory writes that precede a revision conflict are not searchable as committed memories.
- Failed extraction logs event and compaction can still proceed if configured `memory_required=False`.

## Phase 6 — Operational APIs and CLI

Expose operator-friendly functions first; CLI can be thin wrapper later:

```python
store.compaction_status(session_id)
store.list_events(session_id, limit=50)
memory.index_status()
memory.reindex(force=False)
memory.search(query, limit=10)
```

Optional CLI:

```bash
python -m context_manager status --db context.db --session SID
python -m context_manager events --db context.db --session SID
python -m context_manager memory search --db memory.db "query"
python -m context_manager memory index --db memory.db
```

Acceptance tests:

- Status works without any LLM/provider credentials.
- Event output is deterministic JSON.
- Search works after a fresh index.
- CLI never prints raw secrets from stored messages by default.

## Phase 7 — Dispatcher integration contract

Document and test a caller contract for `agent-dispatcher` and future dispatchers:

- Dispatcher owns session registry.
- Store is per-session, not global registry.
- Dispatcher marks tool-derived assistant rows with `kind="tool"` and `tool_name=<name>` metadata so pruning, search, and deletion policies can distinguish them from natural assistant text.
- Dispatcher supplies summarizer and memory extractor.
- Dispatcher decides whether summaries are injected into worker history.
- Dispatcher treats backend sessions as ephemeral caches.

Add integration fixtures that simulate:

- backend switch mid-topic,
- compaction while a new append races,
- reset/rewind after summary,
- tool-derived boundary after watermark,
- memory search result injection.

## Risks

- Summary envelope migration can break callers that expect `summary` to be plain text.
  - Mitigation: keep `get_summary()` returning text; add `get_summary_envelope()`.
- Token-budget tail can split model-invalid tool pairs.
  - Mitigation: explicit pair-preservation tests.
- Memory extraction can hallucinate durable facts.
  - Mitigation: provenance, kind taxonomy, user-role evidence checks, and disabled-by-default extraction.
- CLI/API can leak private stored context.
  - Mitigation: redacted preview by default; explicit `--raw` only for local trusted use.

## Definition of done for v0.3

Minimum useful release:

1. Summary envelope.
2. Persisted compaction events.
3. Token-budget tail selection.
4. Safe render/prune policy helpers.
5. Updated agent-dispatcher integration tests.
6. Legacy raw-summary compatibility tests for `get_summary()` plus `get_summary_envelope()`.
7. README updated to say long-term memory is pluggable and experimental unless `SqliteMemoryBackend` lands.

## Definition of done for v0.4

Memory release:

1. `SqliteMemoryBackend` with FTS5.
2. Memory item provenance and dedupe.
3. Memory extraction callback around compaction boundaries.
4. Status/search/reindex APIs.
5. Minimal CLI.
6. Fresh-environment dogfood with a fresh venv and no Hermes installed.

## Open questions for review

1. Should `SummaryEnvelope` be stored as JSON metadata or normalized columns?
2. Should memory extraction be in `Compactor` core or a separate coordinator class?
3. Should `SqliteMemoryBackend` share the context DB or use a separate memory DB?
4. Should redaction be built-in stdlib regex only, or should richer redaction be optional extra?
5. Should v0.3 include the CLI, or wait until v0.4 memory backend exists?
