---
title: DCP independent reimplementation — implementation spec for qike-ms/context-manager
status: reviewed (claude-sonnet-4-5 BLOCKERs addressed; codex/opencode reviewers timed out, retry queued)
date: 2026-05-19
upstream-ref: github.com/Opencode-DCP/opencode-dynamic-context-pruning (AGPL-3.0)
target-license: MIT (this port)
related: ./compaction-research.md
---

# DCP independent reimplementation — design spec

An **independent reimplementation** in Python of the *ideas* described in
`opencode-dynamic-context-pruning` (DCP, AGPL-3.0, TypeScript), shipped under
MIT in `qike-ms/context-manager`. We are reimplementing algorithms from a
behavioural description (DCP's public `README.md` only) — no DCP source code
has been or will be consulted by the spec author or by implementers.

> Terminology note: this is **not** a strict clean-room under the two-team
> Compaq-vs-IBM definition (where a separate team writes the spec without
> reading the original). The spec author read the public README, which is
> documentation, not source. We deliberately avoid the term "clean-room" in
> contributor-facing language to keep our legal posture honest; the file
> name is retained for continuity.

## 0. Why port (and what we are *not* claiming)

OpenCode's built-in compaction (see `compaction-research.md` §3) is a
single-shot template-driven summary triggered when the window fills. DCP's
insight is different and orthogonal: let the **model** decide when and what
to compress, keep the **store** immutable, and apply transforms only on the
outbound request payload. The four backends we proxy (Hermes, OC, Claude
Code, Codex) are all stateless replays of message history, so a transform
layer between store and provider is the right seam.

We are **not** claiming any novel algorithm — the patterns below are common
in the long-context-management literature (placeholder substitution, span
nesting, dedup, error-purging). DCP is one prior art; we cite it and design
independently. Algorithms are not copyrightable; only specific code
expression is (see §3).

---

## 1. Ideas being ported (described, not coded)

All six are independent strategies; each can be enabled/disabled per session.

### 1.1 Model-invoked `compress` tool
Expose a tool named `compress` to the model with two modes:

- **`range` mode** — args: `start_message_id`, `end_message_id`, `summary`
  (the model writes the high-fidelity technical summary itself, in a
  structured template we provide). The engine replaces messages in
  `[start, end]` with a single placeholder system message containing
  `summary`. Range mode is the default.
- **`message` mode** (experimental) — args: `message_ids: list[id]`,
  `summary`. Replaces specified messages (need not be contiguous) with the
  placeholder. Use sparingly; defeats prompt-cache prefix more.

The model — not the engine — picks the boundaries. The engine nudges the
model when context gets large (a system reminder appended to the next user
turn). We deliberately skip the elaborate `nudgeFrequency` /
`iterationNudgeThreshold` knobs in v1 and use a single threshold + cooldown.

### 1.2 Placeholder-swap middleware
**Session history is never mutated.** When the model invokes `compress`,
the engine writes a `Placeholder` row (id, range or msg-id list, summary
text, created_at, active). On every outbound request build, a middleware
walks the stored messages and substitutes any active placeholders, emitting
the summary text in place of the original span. Untransformed store stays
the source of truth and can be replayed verbatim if a placeholder is
deactivated.

### 1.3 Protected content patterns
Three orthogonal protections checked **before** a message is eligible for
replacement:

- **Tool-name allowlist** (default: `task, skill, todowrite, todoread,
  compress, write, edit`, plus user-extension list). Any assistant turn
  whose tool_calls contain a protected tool, or any tool-result whose
  parent tool_call was protected, is kept verbatim.
- **File-path globs** (default: empty). Configured globs match against
  `tool_call.arguments.file_path` / `.path` / `.filePath` (we normalise).
  Reads/writes/edits to protected paths are kept verbatim.
- **Optional user-message protection** (`protect_user_messages: bool`,
  default `False`). When on, user turns are never replaced. Trade-off
  documented inline: large pasted logs in user turns then never compress.

Protected messages **inside** a `compress range` get appended to the
summary (as labelled blocks like `--- protected: skill output ---`) rather
than dropped, so the model can still cite them. **Size cap:** the union
of protected-block bytes appended to a single summary must not exceed
`config.compress.protected_append_budget` (default 32 KiB / ~8k tokens).
On overflow, oldest protected blocks are themselves replaced with a
one-line stub `[protected output omitted, see message #N]` and the
engine logs a `dcp.protected_append_overflow` event. Prevents
pathological summaries that exceed the span they replaced.

### 1.4 Nested compression
When a new `compress range [a,b]` overlaps any existing active placeholder
whose range `[c,d]` is `⊆ [a,b]`, the older placeholder is embedded inside
the new one and deactivated. Embedding is **structural, not textual**: the
new `Placeholder` row carries `nested_in_id` and a `nested_summaries:
list[int]` list of prior placeholder ids. At render time, middleware
emits the new summary followed by each nested summary inside an opaque
fenced block with a UUID-tagged delimiter (e.g.
`<!--ctxmgr:nested:01HXYZ... start-->`) so heading collisions in
model-authored summaries cannot confuse the parser. Layered context is
preserved across many compress cycles rather than diluted.

For non-overlap-but-adjacent or partial-overlap, v1 rejects the
`compress` call with an error message back to the model: "ranges must be
disjoint or fully nest existing range #N". Simpler invariant, easier to
test. (Auto-split considered; punted to v2 after telemetry.)

### 1.5 Tool-call deduplication
After each `compress` invocation (and only then, to bound cache invalidation
to the same event — running on every build would re-invalidate the prompt
prefix even on turns where nothing structural changed), scan the
post-transform message list for tool calls with identical `(tool_name,
canonical_json(arguments))`. Keep the **latest** output; replace earlier
ones with a one-line stub: `[deduped: see message #N]`. Protected tool
names are exempt.

**Canonical JSON spec** (must match `tests/dcp/test_canonical.py`):
- UTF-8, no BOM
- object keys sorted lexicographically by code point
- no insignificant whitespace
- numbers: integers as decimal; floats via Python `repr` with NaN/±Inf
  rejected (raise → skip dedup for that call)
- strings: shortest valid JSON escaping; no Unicode normalisation (NFC
  considered and rejected: would mask real arg differences from tool
  side)
- `None`/`null` and missing keys treated as distinct

### 1.6 Error-input purging
A tool call whose result has `is_error=True` keeps the **error message**
intact but drops the original (often large) `arguments` block after `N`
turns (default `N=4`). Replace `arguments` with `{"_purged": "after 4
turns; error: <first 200 chars>"}`. The original arguments remain in
`ContextStore` (immutable) so post-hoc debugging via the store is
unaffected — only the *outbound* payload is slimmed. Like dedup, only
re-runs on `compress` events.

---

## 2. Python architecture for context-manager

New module: `context_manager/dcp/` (sibling to `store.py`, `compactor.py`).

```
context_manager/
  dcp/
    __init__.py        # public exports
    message.py         # Message normalized type
    adapters/
      __init__.py      # registry
      base.py          # MessageAdapter ABC
      hermes.py        # Hermes <-> Message
      opencode.py      # OC MessageV2 <-> Message
      claudecode.py    # CC stream JSON <-> Message
      codex.py         # Codex JSONL <-> Message
    engine.py          # CompressEngine — pure transforms
    placeholders.py    # PlaceholderStore (SQLite, extends ContextStore DB)
    tool.py            # CompressTool — schema + handler
    middleware.py      # apply_placeholders(messages, store) -> messages
    protections.py     # ProtectedSet matcher (tools, globs, user-msg flag)
    config.py          # DCPConfig dataclass + loader
```

### 2.1 `Message` (normalised type)

```python
@dataclass(frozen=True)
class Message:
    id: str                       # stable per row; adapter passes through
                                  # the backend-provided id when present,
                                  # else `f"{session_id}:{store_rowid}"`
                                  # (collision-free because session-scoped
                                  # and SQLite rowid is monotonic)
    session_id: str
    role: Literal["system","user","assistant","tool"]
    content: list[ContentPart]    # text, tool_call, tool_result, image_ref
    created_at: float             # epoch seconds
    meta: dict[str, Any]          # backend-specific opaque bag (not used by engine)
```

`ContentPart` is a tagged union: `TextPart`, `ToolCallPart(name, args,
call_id)`, `ToolResultPart(call_id, output, is_error)`, `ImageRefPart`.
Engine only inspects role, content tags, and `meta` is preserved
round-trip for the adapter.

### 2.2 Per-backend adapters

```python
class MessageAdapter(ABC):
    backend: str  # "hermes" | "opencode" | "claudecode" | "codex"

    @abstractmethod
    def to_messages(self, payload: Any) -> list[Message]: ...
    @abstractmethod
    def from_messages(self, msgs: list[Message]) -> Any: ...
    @abstractmethod
    def tool_schema(self) -> dict:  # CompressTool schema in this backend's tool format
        ...
```

Adapter responsibilities:

- **Hermes**: maps to/from `agent.context_engine` message dicts. Uses
  Hermes's own tool-schema JSON.
- **OpenCode**: maps to/from `MessageV2` parts. Tool schema = OC plugin
  tool-definition shape.
- **Claude Code**: maps the streaming JSON message format documented in
  `ClaudeCode-Python` clone (clean-room reference only). Tool schema =
  Anthropic tools API.
- **Codex**: maps Codex JSONL (`input_tokens`, `output_tokens`, items).
  Codex CLI has no public tool API surface in the version we proxy.
  **v1 decision: Codex adapter does not register `compress`.** Dedup +
  error-purge still run on outbound payloads. Manual compress remains
  available via a dispatcher-side `/compress` slash command that calls
  `CompressTool.invoke` directly. We are explicitly *not* shipping an
  in-band sentinel parser (`<<compress …>>` or similar): collision-
  with-natural-output risk is real and the failure mode (silent context
  corruption) is worse than no feature. Revisit if/when Codex CLI gains
  a tool channel.

### 2.3 `CompressEngine`

Pure functions over `list[Message]`. No I/O. No DB. No LLM.

```python
def apply_range_compress(msgs, start_id, end_id, summary, *, protections,
                         existing_placeholders) -> tuple[list[Message], Placeholder]
def apply_message_compress(msgs, ids, summary, *, protections, ...) -> ...
def dedupe_tool_calls(msgs, *, protections) -> list[Message]
def purge_errored_inputs(msgs, *, turn_threshold, protections, now_turn) -> list[Message]
def merge_nested(new_ph, existing_phs) -> tuple[Placeholder, list[Placeholder]]
```

All functions are total, deterministic, and return new lists (immutability
preserves rollback).

### 2.4 `PlaceholderStore`

SQLite table colocated in the same `ContextStore` DB (single file per
session). Schema:

```sql
CREATE TABLE placeholders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL,             -- 'range' | 'message'
  span_start_msg_id TEXT,         -- range mode
  span_end_msg_id TEXT,           -- range mode
  msg_ids_json TEXT,              -- message mode
  summary TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  nested_in_id INTEGER,           -- FK to placeholders.id when wrapped
  created_at REAL NOT NULL,
  deactivated_at REAL
);
CREATE INDEX ix_ph_session_active ON placeholders(session_id, active);
```

Public API: `add()`, `deactivate(id)`, `reactivate(id)`, `active_for(session_id)`,
`history_for(session_id)`. Survives process restarts; that's how state is
shared across backend reconnects (§5).

### 2.5 `CompressTool`

```python
class CompressTool:
    name = "compress"
    description = "Compress closed/stale conversation content into a high-fidelity summary."
    def schema(self, adapter: MessageAdapter) -> dict:
        return adapter.tool_schema_for(self)
    def invoke(self, args: dict, *, session_id, store, ph_store, config) -> ToolResult:
        # validates args, calls engine, writes Placeholder, runs dedup + purge
```

Backends register via their adapter's `tool_schema()`.

### 2.6 Pre-send middleware

```python
def build_outbound(session_id, store, ph_store, adapter, config) -> Any:
    msgs = store.list_messages(session_id)
    phs  = ph_store.active_for(session_id)
    msgs = middleware.apply_placeholders(msgs, phs, config.protections)
    if config.dedup.enabled:        msgs = engine.dedupe_tool_calls(msgs, ...)
    if config.purge_errors.enabled: msgs = engine.purge_errored_inputs(msgs, ...)
    msgs = middleware.maybe_inject_nudge(msgs, ph_store, config)
    return adapter.from_messages(msgs)
```

Idempotent. Called once per outbound request.

---

## 3. License plan — MIT-clean

**Rules:**

1. No code, comments, prompts, or schema strings copied from DCP (TS or
   shipped JSON). The upstream `README.md` was read for *behaviour*
   description and is cited; no text excerpted into the source tree.
2. All summary templates / nudge prompts authored from scratch in this
   repo, dated, and signed off in PR by the author.
3. **Algorithms are not copyrightable** under US law (Baker v. Selden,
   17 USC §102(b)). Only specific code expression is. Independent
   re-implementation of an algorithm from a behavioural spec is the
   textbook clean-room procedure.
4. We do not link to, vendor, npm-install, or runtime-load any DCP
   artifact. There is **zero AGPL surface** in the dependency graph.
5. Attribution: `docs/dcp-prior-art.md` notes "Inspired by DCP (AGPL-3.0,
   © Dan Smolsky); independently re-implemented from public README,
   no source code consulted." This is courtesy, not a legal
   requirement.
6. Any contributor opening a PR to this module must confirm they have
   not read DCP source. CONTRIBUTING.md gets a line for it.

**Residual risk:** zero, provided the rules above are followed. If a
contributor lifts a prompt verbatim from DCP because "it's just text",
that text *is* copyrightable. Mitigation = the contributor checkbox.

---

## 4. Test plan

`tests/dcp/` mirrors module layout. All hermetic (per AGENTS.md rule).

### 4.1 Unit tests (one per transform)
- `test_apply_range_compress.py` — happy path, empty range, single-message
  range, range crossing protected message (must skip / embed).
- `test_apply_message_compress.py` — non-contiguous ids, missing id error.
- `test_dedupe.py` — identical args dedup, near-identical (different
  whitespace in JSON) must still dedup via canonicalisation, protected
  tool exempt.
- `test_purge_errors.py` — error preserved, args replaced after N turns,
  not before, protected tool exempt.
- `test_nested.py` — `[c,d] ⊆ [a,b]` nests; disjoint OK; partial overlap
  rejected with structured error.

### 4.2 Property tests (`hypothesis`)
- **Placeholder reversibility**: for any random `list[Message]` and any
  random sequence of `apply_*` then `deactivate_all`, the result of
  `apply_placeholders` equals the original list. (Round-trip identity.)
- **Determinism**: `apply_*` is a pure function of inputs — same inputs
  twice → bit-identical output.
- **Order preservation**: non-replaced messages keep relative order.
- **Idempotence**: running middleware twice ≡ running it once.

### 4.3 Snapshot / fixture tests
Per-adapter golden files in `tests/dcp/fixtures/{hermes,opencode,
claudecode,codex}/`. Each fixture pairs an input transcript (JSON) with
the expected outbound payload after middleware. Update via
`pytest --snapshot-update` ratchet, reviewed in PR.

### 4.4 Concurrency
`test_placeholder_store_concurrent.py` — 2 threads writing placeholders
to the same session DB; SQLite `BEGIN IMMEDIATE` ensures no lost writes.

### 4.5 Backend round-trip
For each adapter: `from_messages(to_messages(payload)) == payload` for
the fixture corpus (modulo documented lossy fields, listed in adapter
docstring).

---

## 5. Integration with agent-dispatcher

Hook points in `agent-dispatcher`:

- **Outbound (request build).** Today, dispatcher reads
  `ContextStore.list_messages(session_id)` and hands it to the backend
  client. Insert middleware between those two steps:

  ```python
  msgs = ctx_store.list_messages(sid)
  payload = dcp.build_outbound(sid, ctx_store, ph_store, adapter, dcp_cfg)
  backend.send(payload)
  ```

- **Inbound (response parse).** When the model emits a `compress` tool
  call, the adapter routes it through `CompressTool.invoke(...)` which
  writes to `PlaceholderStore`. The tool's *result* (string the model
  sees) is short: `"compressed messages 12–47 into placeholder #3"`.
  The visible side-effect appears on the **next** outbound build.

- **State across backend restarts.** `PlaceholderStore` is SQLite,
  same file as `ContextStore` (one file per `session_id =
  f"{chat_id}:{thread_id or 'None'}"`, per AGENTS.md). On dispatcher
  restart, placeholders auto-rehydrate; nothing to migrate. On backend
  reconnect (e.g. OC server restart), the store is the source of truth
  — DCP middleware just replays existing placeholders against the
  current message list. No in-memory cache to lose.

- **Session-id mapping across backends.** When a user switches a
  Telegram thread from OC to CC mid-session, the placeholder records
  are backend-agnostic (they reference `Message.id`, which is stable
  per row in `ContextStore`). The new adapter renders them into the
  new backend's payload format on the fly.

- **Telemetry hook.** Dispatcher already logs `last_input_tokens`.
  Extend with `dcp_active_placeholders`, `dcp_tokens_saved_est`
  (sum of pre-summary token estimate − summary token estimate, using
  `context_manager.token_estimator`). Feeds future autopilot.

---

## 6. Scope cuts vs DCP upstream (v1 explicitly skips)

| DCP feature | v1 decision | Rationale |
|---|---|---|
| Notification UI (chat / toast) | **skip** | tg-bridge surfaces this differently (Telegram messages); decouple from engine |
| `autoUpdate` | **skip** | We are Python in a monorepo, no npm |
| `commands.*` slash commands (`/dcp sweep`, `/dcp decompress`, etc.) | **skip** in v1 | Useful but not core; revisit in v2 once core lands |
| Prompt overrides (`experimental.customPrompts`) | **skip** | Our prompts live in repo, edited via PR |
| `nudgeFrequency` / `iterationNudgeThreshold` / `nudgeForce` knobs | **simplify** to one threshold + cooldown | Tune later from telemetry; YAGNI |
| `protectTags` (`<protect>…</protect>` inline tags) | **skip** v1 | Add when a user asks |
| `manualMode` toggle | **skip** v1 | Engine-disabled config is sufficient |
| `summaryBuffer` (let summary tokens extend window) | **skip** v1 | Footgun; revisit |
| `modelMaxLimits` / `modelMinLimits` per-model overrides | **skip** v1 | One global threshold; per-model via our existing `window_registry` |
| `experimental.allowSubAgents` | **skip** | Subagent semantics differ per backend; defer |
| Cumulative `/dcp stats` across sessions | **skip** v1 | Telemetry hook (§5) lays groundwork; UI later |
| OC-specific `MessageV2` quirks beyond what the adapter needs | **skip** | Only the adapter knows |

In v1, the user-visible surface is: model gets a `compress` tool;
dedup + error-purge run automatically; placeholders persist. That's it.

---

## 7. Open questions (to resolve before coding)

1. **Codex tool affordance.** Confirm the JSONL surface really has no
   tool-call channel in the CLI build we proxy. If so, sentinel parser
   is fine; otherwise prefer the real channel. — **Action:** trace one
   Codex session end-to-end before adapter PR.
2. **Token-saving telemetry.** Use `token_estimator.py` or a real
   tokenizer per provider? Estimator is fine for v1 (cheap, in-tree).
3. **Where does `dcp_cfg` live?** Propose `~/.config/context-manager/
   dcp.toml`, project override at `./.context-manager/dcp.toml`.
   Schema in `dcp/config.py`, validated on load.
4. **Cache-invalidation cost.** DCP README cites ~85% vs 90% hit rate.
   We should measure on Hermes traces before promoting from opt-in
   to default.

---

## 8. Milestones

- **M1:** `Message` type + Hermes adapter + `CompressEngine` pure
  functions + property tests. No DB, no tool. Internal demo via
  scripted fixture.
- **M2:** `PlaceholderStore` + middleware + `CompressTool` wired into
  one backend (Hermes). End-to-end test on a captured 100-turn
  transcript.
- **M3:** OC + CC adapters. Snapshot tests green on fixtures from
  real sessions.
- **M4:** Codex adapter (sentinel parser). Dispatcher integration PR.
- **M5:** Telemetry + opt-in flag default-on for one user (me).
  Measure cache hit rate vs baseline.

Each milestone ships independently; no big-bang merge.

---

## 9. Review log

| Reviewer | Verdict | BLOCKERs raised | Status |
|---|---|---|---|
| claude-sonnet-4-5 | reviewed | (1) "clean-room" terminology overclaim; (2) Codex `<<compress>>` sentinel collision-unsafe; (3) `## Prior compressed context` heading collision in nested summaries | all 3 addressed in this revision — see §0 terminology note, §2.2 Codex bullet, §1.4 structural nesting |
| claude-sonnet-4-5 (nits) | reviewed | dedup-on-compress justification thin; protected-append unbounded; canonical_json under-specified; Message.id collision wording | all addressed in §1.5, §1.3, §1.5 canonical spec, §2.1 |
| codex (gpt-5-codex) | **timed out at ~15min** | — | retry queued; spec proceeds without their sign-off, will fold any findings into a follow-up commit |
| opencode | **timed out at ~15min** | — | retry queued (same) |

No reviewer raised an AGPL contamination risk against §3 once the
"clean-room" wording was softened. The legal posture is: independent
reimplementation from a behavioural README, algorithms uncopyrightable
under 17 USC §102(b), zero AGPL artifacts in the dependency graph.
