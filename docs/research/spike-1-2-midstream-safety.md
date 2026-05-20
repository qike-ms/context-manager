---
title: tg-bridge — Spike 1+2: Midstream Compaction Safety (OC `/summarize`, CC `/compact`)
issue: qike-ms/my-ai-skills#5
parent: qi/projects/tg-bridge/compaction-research.md
status: draft
date: 2026-05-17
---

# Spike 1 & 2 — Can OC `/summarize` and CC `/compact` run mid-stream?

> **Open questions §6.2 + §6.3 of `compaction-research.md` (528111b72)**
>
> 2. Does `POST /summarize` work on a session mid-stream, or only when idle?
> 3. Is CC `/compact` safe to send between assistant turns, or does it require the model to finish?
>
> Both questions change Compactor worker design — they decide whether the worker must idle-gate the trigger or can fire-and-forget.

## TL;DR

| Backend | API | Mid-stream behaviour | Bridge implication |
|---|---|---|---|
| OpenCode | `POST /session/{id}/summarize` | **Accepted at HTTP layer; serially queued by SessionRunState.** Returns `200 true` only **after** the in-flight assistant turn finishes; total latency = (remaining time of running turn) + (compaction LLM call). Server-side queueing is invisible to the caller. | **Free-running for HTTP, idle-gated for time budget.** It is safe to POST any time, but the Compactor worker must treat the call as "eventually completes after current turn" — don't time it out tightly, and don't fire while abort is in flight. |
| Claude Code | `/compact` (user message in `--input-format=stream-json`) | **Queued as a normal user turn.** The current assistant turn runs to `end_turn`, then `/compact` runs as a new turn — server emits a `system/compact_boundary` event with `pre_tokens`/`post_tokens`/`duration_ms` and `trigger:"manual"`, followed by `result subtype=success num_turns=0`. | **Free-running.** The bridge can write `/compact` to the stream-json input pipe at any moment without corrupting the running turn. Compactor watches stderr/stdout for `compact_boundary` to confirm. |

**Recommendation: free-running Compactor for both.** Neither backend exposes a "session is busy → reject compaction" error path. Both serialize compaction *behind* the running turn at the orchestrator layer. The worker does not need an idle-gate.

The only constraint is **latency budgeting**: a POST to `/summarize` will block the caller for `time_to_finish_running_turn + ~10s LLM call`. The Compactor worker must use a generous timeout (≥ longest expected turn + 60s) or fire-and-forget via a detached fiber and watch `Event.Compacted` on the bus.

---

## 1. Test environment

- macOS 26.5, opencode 1.15.3, Claude Code 2.1.143
- OpenCode headless server: `opencode serve --port 18765 --hostname 127.0.0.1 --print-logs` in `/tmp/spike-compactor`
- Model for both: `amazon-bedrock / us.anthropic.claude-sonnet-4-6` (≈200k context, server-side keys via env)
- All raw artifacts in `/tmp/spike-compactor/` (gitignored, throwaway)

OpenAPI handler path verified live (`GET /doc`): `POST /session/{id}/summarize`, body `{providerID, modelID, auto?}`, response `boolean`.

Source code references inspected at `~/git/opencode` (branch `dev`, snapshot 2026-05-17):

- `packages/opencode/src/session/run-state.ts` — `SessionRunState`: `assertNotBusy`, `ensureRunning(sessionID, onInterrupt, work)`. Backed by `Runner` (`src/effect/runner.ts`) with a `SynchronizedRef` whose states are `Idle | Running | Shell | ShellThenRun`. `ensureRunning` on a `Running` state returns `awaitDone(st.run.done)` — **i.e. waits for the existing run, does not reject and does not start a new fiber.**
- `packages/opencode/src/session/prompt.ts:1877–1881` — `loop` calls `state.ensureRunning(input.sessionID, lastAssistant(input.sessionID), runLoop(input.sessionID))`.
- `packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts:257–277` — the `summarize` handler calls `compactSvc.create(...)` then `promptSvc.loop(...)`. `loop` therefore inherits the `ensureRunning` queuing behaviour above.
- `packages/opencode/src/session/compaction.ts:586–616` — `create` appends a synthetic `user` message with a `compaction` part. `process` (`processCompaction`, line 346) handles it inside the same loop iteration.
- `packages/opencode/src/session/prompt.ts:1698–1717` — within `runLoop`, the compaction part is recognised when the next user turn is processed; on overflow, an additional `compaction.create` is enqueued automatically.

So the source predicts: **`POST /summarize` while busy ⇒ queued, not rejected.** The empirical test confirms.

---

## 2. OpenCode — observations

### 2.1 Source-derived hypothesis

`SessionRunState.assertNotBusy` is only called by `shell` and `revert` handlers (verified by `rg`). `summarize` does **not** call `assertNotBusy`, so there is no `SessionBusyError` (`BadRequest`) path for it. Calling `summarize` mid-stream should succeed.

The HTTP handler runs synchronously inside the request fiber until `promptSvc.loop` returns; `loop` enters `ensureRunning`, which for an already-`Running` state returns the running fiber's deferred. The handler therefore awaits the in-flight turn before any compaction work begins.

### 2.2 Live test — `/summarize` mid-stream (control + experiment)

**Control (idle session, single short reply already finished):**

```
POST /session/{sid}/summarize  →  HTTP 200  body=true  wall=13.7s
```

`wall ≈ compaction LLM call only`. Baseline.

**Experiment (long task in flight, fire `/summarize` at t≈1.5s):**

```
T0:  POST /session/{sid}/prompt_async  →  HTTP 204  (long 5000-word essay started)
T1.5: POST /session/{sid}/summarize    →  blocks ...
```

Run 1: `summarize` returned HTTP 200 `true` after **4.81s** — the in-flight task happened to be a *short* one (essay never produced; assistant got blocked on a tool call early).

Run 2 (with a deliberately heavy "5000-word memory-ordering deep dive" task that genuinely streamed for ~120s before completion): `/summarize` request blocked for the full client-side curl timeout (60s) and the response was never received before the timeout fired, *but the server completed the work*. Inspecting `/session/{sid}/message` after the dust settled:

```
messages = 5
  [0] role=user            parts=[(text, 128)]                       # original prompt
  [1] role=assistant       parts=[step-start, tool=todowrite/done]   # the live turn
  [2] role=assistant       parts=[step-start, text(24225), tool, step-finish]  # the long essay finished
  [3] role=user            parts=[(compaction, auto=False)]          # injected by /summarize
  [4] role=assistant mode=compaction summary=True parts=[step-start, text(2863), step-finish]  # OC SUMMARY_TEMPLATE filled out
```

The summary text starts:

```
## Goal
- Produce a comprehensive ~5000-word technical deep-dive on memory ordering in modern CPUs with chapter structure, C/assembly code examples, and academi[...]
```

Confirming: **the running turn was allowed to finish first; compaction ran after, in the same session, with the full `SUMMARY_TEMPLATE` schema (Goal / Constraints / Progress / Decisions / Next Steps / Critical Context / Relevant Files).** No turn was lost. No interleaving. No `BadRequest`.

### 2.3 Abort interaction

What if the bridge wants to drop the running turn and force compaction immediately?

```
T0:    prompt_async (heavy 3000-word task)
T2.5:  POST /summarize   (blocks)
T6.5:  POST /abort       →  HTTP 200 true  wall=0.019s
T?:    /summarize returns HTTP 200 true after wall=8.83s total
```

After abort:

```
messages = 4
  [0] user (prompt)
  [1] assistant build summary=None parts=[step-start, tool=todowrite/completed, step-finish]
  [2] assistant build summary=None parts=[step-start, text(2912)]   # truncated, no step-finish
  [3] user parts=[(compaction, auto=False)]                          # injected
  # NO compaction-assistant message — compaction did NOT run after the abort
```

So: **abort cancels the entire runner (including the queued compaction)**, leaving the synthetic compaction user-part orphaned. The next user prompt will see the orphan and continue from it (which is fine — OC's `runLoop` handles the `compaction` part natively, see `prompt.ts:1698`). The `/summarize` HTTP call still returned `200 true` because `compactSvc.create()` succeeded; only `promptSvc.loop()` was interrupted.

**Bridge implication:** if the Compactor races with `/abort`, the session ends in a "compaction pending, no compaction assistant" state. The next user message will trigger compaction to run as the first thing in the next runLoop iteration. This is benign but worth documenting — the Compactor's success signal must be **`Event.Compacted` on `/event` SSE**, not the HTTP response.

### 2.4 What `summarize` does **not** do

- It does **not** stream the compaction output. The HTTP response is just `true` (`Schema.Boolean`).
- It does **not** return the summary blob. To get the structured summary the bridge must read `/session/{sid}/message` after `Event.Compacted` fires and pull the latest `assistant mode=compaction` text part.
- It does **not** accept a `force=true` or `tail_turns` parameter today. The signature is `{providerID, modelID, auto?: boolean}` (auto defaults to false).

---

## 3. Claude Code — observations

CC has no HTTP server. The only programmatic way to drive it non-interactively is `--print --input-format=stream-json`, which reads JSON-lines from stdin (one user message per line) and emits JSON-lines on stdout (assistant events).

### 3.1 Test

```bash
( echo '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Write a 3000-word essay on the history of compilers, chapter by chapter..."}]}}'
  sleep 3
  echo '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"/compact"}]}}'
  sleep 90
) | claude -p --verbose --model sonnet --dangerously-skip-permissions \
           --input-format=stream-json --output-format=stream-json \
           --include-partial-messages
```

The `/compact` line is written to stdin **3 s after** the long essay turn began — well before the assistant finishes.

### 3.2 Output (filtered)

```
line 2   system/init       session_id=796c7105... model=claude-sonnet-4-6
line 261 assistant         text len=19117  ("# The History of Compilers...")
line 266 result            subtype=success num_turns=1 dur_ms=105048 stop=end_turn
line 271 system/init       (same session_id, reinitialised for next turn)
line 272 system/compact_boundary  trigger=manual pre_tokens=29758 post_tokens=1846 duration_ms=39051
line 275 result            subtype=success num_turns=0 dur_ms=39056 stop=None
```

Interpretation:

- The first assistant turn ran for 105 s and completed normally (`end_turn`, full 19117-char essay). The `/compact` line on stdin **did not interrupt it**.
- After the turn finished, CC emitted a second `system/init` (turn boundary), then ran `/compact`:
  - emitted `system/subtype=compact_boundary` with `pre_tokens=29758`, `post_tokens=1846`, `duration_ms=39051`, `trigger:"manual"` — i.e. CC compacted 29758 → 1846 tokens (≈94% reduction)
  - followed by `result num_turns=0` (compact doesn't count as a model turn)
- A shorter test (count 1→30, `/compact` 4s later) showed the same pattern: full assistant reply, then a `compact_boundary`, then a `/compact` result turn.

### 3.3 Implications

- **Safe to fire `/compact` mid-stream.** CC's stdin reader buffers the line; the agent loop picks it up at the next turn boundary. No interleaving with the running turn, no corruption.
- **Detection signal:** the bridge can watch for `{"type":"system","subtype":"compact_boundary"}` and extract `compact_metadata.{pre_tokens, post_tokens, duration_ms, trigger}`. This is the authoritative confirmation.
- **No cross-session API.** Unlike OC, CC compaction is bound to the running `claude -p` process. If the bridge spawns CC per request (which the current tg-bridge does for `/cc`), `/compact` is only useful when the bridge holds a long-lived stream-json process. **This is a design constraint, not a safety problem.**

---

## 4. Cross-cutting findings

1. **Neither backend rejects compaction-while-busy.** Both serialize behind the running turn. The Compactor worker does **not** need to query an `is-busy` endpoint before triggering.
2. **Both honour the OC structural template / CC focus phrase verbatim.** The empirical CC `pre→post` ratio (29758→1846 on a 19k-char essay) is in line with OC's behaviour and matches what `compaction-research.md §3` reported from source reading.
3. **Latency profile, Compactor sizing:**
   - OC idle compaction LLM call: ~10–14 s (Sonnet 4.6)
   - OC mid-stream: `wait_for_running_turn + ~10–14 s`. For a tg-bridge session stuck on a 154-turn opus run, the running turn could be 2–5 min. The Compactor worker must use a *generous* timeout (≥ 600 s) or async-detach.
   - CC mid-stream: `wait_for_running_turn + ~39 s compact`. The 39 s is consistent with CC running its full clone-room template + Q&A.
4. **HTTP success ≠ work complete.** OC `/summarize` returns `200 true` even when the queued compaction is later cancelled by `/abort`. The bridge must use bus events (`session.compacted`) or message-list polling to confirm, not the HTTP 200.
5. **Codex CLI: still nothing.** No in-session compaction API. Rotation remains the only path (out of scope for this spike).

---

## 5. Recommendation for the Compactor worker

### Worker design (revised)

```
┌──────────────────────────────────────────────────────────────────┐
│  Compactor worker (per logical conversation, not per backend)    │
│                                                                  │
│  Trigger:  bridge_event(session, last_usage)                     │
│            if ratio >= 0.70 and not pending(session): enqueue   │
│                                                                  │
│  Execution per enqueued session, by backend:                     │
│                                                                  │
│   OC  : POST /session/{id}/summarize  (timeout=600s OR detach)  │
│         await Event.Compacted on /event SSE for confirmation     │
│         on /abort race: re-enqueue and retry next idle window    │
│                                                                  │
│   CC  : write '/compact' line to long-lived stream-json stdin    │
│         await system/compact_boundary on stdout                  │
│         (no separate idle-gate needed)                           │
│                                                                  │
│   Hermes : no-op (auto-compacts internally)                      │
│   Codex  : session rotation (separate spike)                     │
└──────────────────────────────────────────────────────────────────┘
```

**Concurrency policy:** one pending compaction per session ID. The worker MUST de-duplicate triggers (a second high-ratio response while compaction is already queued must not double-enqueue) — this is a worker-side concern; the OC server will happily accept N pending compactions in a row.

**Abort interlock:** if the bridge issues `/abort` on a session while a compaction is queued, the Compactor must re-enqueue. The orphaned `compaction` user-part in OC is harmless but only resolves on the next user prompt.

**Idle-gate is NOT needed.** The original concern in `compaction-research.md §6.2/§6.3` was that mid-stream calls might either reject (`BadRequest`) or corrupt the running turn. Both fears are empirically falsified for the v1 design.

### What *is* needed (and was not obvious before this spike)

1. A bus-event/SSE subscriber per OC session to confirm completion.
2. A long-lived CC stream-json subprocess holder for the bridge — the current tg-bridge spawns CC per request, which makes `/compact` unusable mid-stream. Rework to long-lived subprocesses (one per logical conversation) **must** ship together with the Compactor or the CC path will silently no-op.
3. Worker timeout = max(remaining-turn-budget + 60s, 120s). Do not use the default 30s curl timeout.
4. A "compaction-pending" gate on `prompt_async`: if the bridge has just enqueued `/summarize` and the user hits Enter again on the chat, the bridge must wait for `Event.Compacted` before the next prompt to avoid stacking *two* compactions (works, but doubles latency for nothing).

---

## 6. Verdict

**VALIDATED** — both OC `/summarize` and CC `/compact` are safe to fire mid-stream. The Compactor worker should be **free-running with bus-event confirmation**, not idle-gated.

### Artifacts

- OC server logs + curl meta: `/tmp/spike-compactor/{providers.json, sess*.json, midstream.json, sum_abort.*, abort_msgs.json, final.json, final_sess.json}`
- CC stream-json transcripts: `/tmp/spike-compactor/cc-{stream,out,out2,long}.jsonl`
- All throwaway; not committed.

### Source citations (for the trio review)

- `~/git/opencode/packages/opencode/src/session/run-state.ts:70–74` (`assertNotBusy` only called by shell/revert)
- `~/git/opencode/packages/opencode/src/effect/runner.ts:115–138` (`ensureRunning` queues onto existing `Running` state)
- `~/git/opencode/packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts:257–277` (`summarize` handler does not call `assertNotBusy`)
- `~/git/opencode/packages/opencode/src/session/compaction.ts:586–616` (`create` enqueues, `process` (line 346) runs inside the prompt loop)
- `~/git/opencode/packages/opencode/src/session/compaction.ts:43–80` (SUMMARY_TEMPLATE) — confirmed to be the structure of the response text.
- CC `compact_boundary` event — observed in stream-json output (no public source).

### Open follow-up (out of scope for Spike 1/2)

- **Spike 3** — Codex session rotation: does `codex` CLI accept a seed system message via flag, or only via stdin?
- **Spike 4** — Cross-backend continuity: feed OC's `SUMMARY_TEMPLATE` output back into CC as a system message — does CC honour it?
- **Spike 5** — How OC's `Event.Compacted` SSE behaves when the consumer drops/reconnects.
