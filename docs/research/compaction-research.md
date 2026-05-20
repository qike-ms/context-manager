---
title: tg-bridge — Context Compaction Research
issue: qike-ms/my-ai-skills#5
status: draft
date: 2026-05-17
---

# Compaction research for tg-bridge

The Telegram bridge proxies to four backends (Hermes, OpenCode, Claude Code, Codex) and currently has **no context management** — the stuck session that prompted this work ran 154 turns on opus-4.7 with 11M cumulative cache reads before stalling on a Copilot stream timeout. This doc surveys what each backend actually does, what's reusable, and where the bridge has to add its own logic.

---

## 1. Measuring real context size per backend

The first land-mine: **`tokens.cache.read` is a billing counter, not a context-window gauge.** Anthropic increments it on every turn that hits a cached prefix; over a 150-turn session it routinely exceeds 10M even though the actual prompt is well under 200k. The best available proxy for "how full is the window?" is **the `input_tokens` of the most recent request** (since OC, CC, Codex all replay full history each turn) — not perfect, since some backends inject hidden system/tool context not visible in that count, but the closest signal the bridge can read.

| Backend | Where the real number lives | Notes |
|---|---|---|
| **Hermes** | `usage.prompt_tokens` from the provider response, stored in `ContextEngine.last_prompt_tokens` (`agent/context_engine.py:50`). Compressor compares it to `threshold_tokens = context_length * threshold_percent` (default 0.50). Source: `agent/context_compressor.py:484–566`. | Already correct. Uses `get_model_context_length()` from `agent/model_metadata.py:1428`. |
| **OpenCode** | `MessageV2.Assistant.tokens` — fields `input`, `output`, `cache.read`, `cache.write`, `total`. Overflow check in `packages/opencode/src/session/overflow.ts:29` sums all four (`total OR input+output+cache.read+cache.write`) and compares to `usable(model)`. **This conflates billing with active context for the "is the window full?" question.** Hypothesis (not yet verified across providers): OC's 20k `COMPACTION_BUFFER` triggers early enough to mask the bug on small windows; would want traces across opus/sonnet/gemini to confirm. | The `/session/<id>/summarize` HTTP endpoint takes no token count — it just runs unconditionally. |
| **Claude Code** | Closed source. Public docs (`docs.anthropic.com/.../context-windows`) describe "auto-compact" near the limit and `/compact <focus>` for manual. The two open-source Python re-implementations (see §3) both poll `response.usage.input_tokens` against a static window with a `AUTO_COMPACT_BUFFER = 13_000` floor. | Bridge cannot read CC's internal estimate; must mirror the same arithmetic from response usage. |
| **Codex** | Codex CLI exposes usage in its JSONL stream (`input_tokens`, `cached_input_tokens`, `output_tokens`). Bridge already parses this for the responses adapter (`agent/codex_responses_adapter.py`). | In the CLI surface examined here (2026-05), no in-session compaction; rotation is the practical workaround. Other Codex surfaces may differ. |

**Recommendation:** in the bridge, store per-session `last_input_tokens` (the *real* number) **and** `cumulative_cache_read` (for cost telemetry only — never use it as a compaction trigger).

---

## 2. Model-window registry

Windows vary wildly and the bridge has to know the right one per active model. **Numbers below are approximate as of May 2026 and shift with provider tier / endpoint / beta flag — pin to provider docs at startup, do not hard-code in the bridge:**

| Model | Approx. context window | Source |
|---|---|---|
| claude-opus-4-5 / 4.7 | ~200,000 | docs.anthropic.com/en/docs/about-claude/models/overview |
| claude-sonnet-4-5 | ~1,000,000 (1M beta tier) | Anthropic Sonnet 4.5 release notes |
| gpt-5 family | ~400,000 (varies by tier) | OpenAI model docs |
| gemini-2.5-pro | ~2,000,000 | ai.google.dev model card |
| gpt-4.1 | ~1,000,000 | OpenAI |
| kimi-k2 / glm-4.6 | ~256k / ~200k | provider docs |

### Data structure

Hermes already ships the canonical table — `DEFAULT_CONTEXT_LENGTHS` in `agent/model_metadata.py:139`, with longest-prefix matching at `model_metadata.py:1700`. The bridge should **import that dict directly** rather than re-maintaining one. Fallback chain Hermes uses:

1. Explicit `config_context_length` override.
2. Provider metadata (LiteLLM-style `model_info.max_input_tokens`).
3. `DEFAULT_CONTEXT_LENGTHS` longest-prefix.
4. Last resort: 128k.

For OC/CC/Codex backends, the bridge gets the model name from the session response, normalises it (`anthropic/claude-opus-4-5` → `claude-opus-4-5`), and looks it up.

```python
# tg_bridge/window_registry.py
from agent.model_metadata import get_model_context_length

def window_for(provider: str, model: str, override: int | None = None) -> int:
    return get_model_context_length(model=model, provider=provider,
                                    config_context_length=override)
```

If hermes-agent isn't importable from the bridge process, vendor a frozen copy of `DEFAULT_CONTEXT_LENGTHS` + the lookup function (~200 LOC).

---

## 3. Per-tool comparison

| Aspect | **Hermes** | **OpenCode** | **OpenClaw** | **Claude-Code (Python clones)** |
|---|---|---|---|---|
| **Trigger condition** | `last_prompt_tokens >= context_length * 0.50` (`should_compress`, `context_compressor.py:601`). Threshold configurable. | `tokens.total >= context_length - reserved` where `reserved = max(20k, max_output_tokens)` (`overflow.ts:8–32`). | **No compaction code.** Repo is an iMessage/Slack/Discord gateway; "summarize" hits are i18n strings only. | `AUTO_COMPACT_BUFFER = 13_000` below window (`terminal-creator/ClaudeCode-Python/cc/compact/compact.py:25`). Reactive trigger gated by `has_attempted_reactive_compact` to avoid loops. |
| **Preserved verbatim** | Tail of N turns sized to `MIN_PRESERVE_RECENT_TOKENS..MAX_PRESERVE_RECENT_TOKENS` (2k–8k); system prompt always kept. | Last `DEFAULT_TAIL_TURNS = 2` turns + everything inside `PRUNE_PROTECT = 40_000` tail token budget (`compaction.ts:38–43`). Plus messages with `PRUNE_PROTECTED_TOOLS = ["skill"]` parts. | n/a | Last `POST_COMPACT_KEEP_TURNS = 4` user-assistant pairs (`compact.py:27`). |
| **Summarised** | Middle turns via auxiliary model (cheap/fast). Iterative: if a prior summary exists, asks summarizer to *update* it rather than redo (`context_compressor.py:1023`). Has a structured "summarizer preamble" that frames prior turns as source material, not instructions. | Single template enforced (`SUMMARY_TEMPLATE`, `compaction.ts:44–79`) — fixed Markdown with Goal / Constraints / Progress / Decisions / Next Steps / Files. Multiple compactions get re-summarised into a new `CompactBoundaryMessage`. | n/a | Free-form summarizer prompt (`COMPACT_SYSTEM_PROMPT`, `compact.py:31`). No fixed schema. |
| **Dropped** | Tool results truncated to `TOOL_OUTPUT_MAX_CHARS`; images stripped from historical turns (`_strip_historical_media`); duplicate tool results deduped. | Tool outputs truncated to `TOOL_OUTPUT_MAX_CHARS = 2_000` *before* sending to summarizer (`compaction.ts:39`). | n/a | Whole old turn block dropped, only summary kept. |
| **Tool-call handling** | Tool calls + results serialized into labelled text for the summarizer (`_serialize_for_summary`, `context_compressor.py:819`). Args JSON truncated to ~200 chars/call. | Tool parts included in summary input but capped per-message. `skill` tool parts are protected from pruning. | n/a | No special handling — tool blocks flattened into prose by `_messages_to_text`. |
| **File state / plans / todos** | Not explicitly preserved as structured slots; relies on summarizer prompt asking for "file paths, commands, error messages, line numbers". | **Yes — structurally.** SUMMARY_TEMPLATE has dedicated `Relevant Files`, `In Progress`, `Next Steps`, `Critical Context` sections. The schema *forces* the model to fill them or write `(none)`. Strongest design of the three. | n/a | No — free-form. Loses structure between compactions. |
| **Single vs multi-pass** | Multi-pass: (1) prune old tool outputs, (2) summarise middle, (3) reassemble; can iterate when threshold re-crossed. Has fallback to main model if auxiliary fails (`_fallback_to_main_for_compression`). | Single LLM call per compaction event, but the template enforces structure and earlier summaries are fed back in. | n/a | Single pass. No iteration logic. |
| **Source cites** | `~/.hermes/hermes-agent/agent/context_compressor.py`, `agent/context_engine.py`, `agent/model_metadata.py` | `~/git/opencode/packages/opencode/src/session/{compaction.ts, summary.ts, overflow.ts, processor.ts}` (branch `dev`, May 2026) | `~/git/openclaw` — `rg -i 'compact|summariz' src/` returns zero hits in core logic, only i18n strings and a `condenser` plugin stub. **No reusable compaction implementation exists in OpenClaw.** | `github.com/terminal-creator/ClaudeCode-Python/cc/compact/compact.py`, `cc/compact/autoCompact.py`; `github.com/GPT-AGI/Clawd-Code/src/compact_service/` and `src/context_system/microcompact.py`. Both are **community clean-room re-implementations** (Chinese-language READMEs, ~5k LOC), claim to follow leaked TS structure but are not official. Treat as illustrative, not authoritative. |

**OC's `/session/<id>/summarize`** (`packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts`) routes to `SessionSummary.summarize` (`session/summary.ts:101`) which invokes the full template pipeline above. So the slur in the issue ("dumb single-pass LLM call") is half right: it *is* one LLM call, but the template forces structured preservation. The real problem is that the bridge calling it has no way to *trigger* it at the right moment — there's no autopilot on the server, only the TUI.

---

## 4. Attention-mechanism adaptation — honest assessment

Vaswani et al. 2017 ("Attention Is All You Need", arxiv 1706.03762) computes `softmax(QKᵀ/√d)·V` over token embeddings inside a single forward pass of a single model. **The analogy to turn-level pruning is loose.** What carries over:

- **Score-then-select** instead of position-based heuristics: every turn gets a relevance score against a query vector (the "mission", §spike doc), and we keep top-k.
- **Multiple heads** ≈ multiple scoring criteria run in parallel (relevance to mission, file-state references, decision content, recency) and combined.

What does **not** carry over:

- We have no `Q, K, V` matrices — we'd be embedding turns with a separate model and computing cosine similarity. That's a retrieval-augmented summariser, not "attention".
- Calling this "attention" is fashionable but misleading. **Honest framing: this is goal-conditioned salience scoring with cosine similarity over turn embeddings.** The Vaswani citation belongs in "inspiration", not "method".

Useful primitives the bridge can borrow without overclaiming:
1. **Positional bias toward recency** (Vaswani uses sinusoidal pos. encoding; we just exponentially decay scores with turn distance).
2. **Multi-head pooling** (separate scorers for code, decisions, errors, files — combine via weighted sum, weights learned/tuned offline).

If we frame this honestly, no one rolls their eyes when we ship it.

---

## 5. Recommended strategy for tg-bridge

### Trigger
For every backend call, after receiving the response, compute:
```
window  = window_for(provider, model)
filled  = response.usage.input_tokens   # NOT cache.read, NOT total
ratio   = filled / window
```
Trigger compaction **before** sending the next user message when `ratio >= 0.70`. Hard-fail at `0.90` (refuse to send, force compaction).

### Preservation budget
```
keep_tail_tokens    = clamp(window * 0.10, 4_000, 16_000)   # last N turns verbatim
summary_budget      = clamp(window * 0.05, 2_000, 8_000)
system_prompt + tool_defs : verbatim, never touched
```

### Pseudocode
```python
async def maybe_compact(session: BridgeSession, last_usage: Usage) -> None:
    window  = window_for(session.provider, session.model)
    filled  = last_usage.input_tokens
    if filled < window * 0.70:
        return
    msgs = await session.fetch_messages()                       # backend-specific
    system, tool_defs, tail, middle = split(msgs, keep_tail_tokens(window))
    summary = await summarise(
        middle,
        template=OC_TEMPLATE,                                   # steal OC's schema
        max_tokens=summary_budget(window),
        model=session.aux_model or "local-gemma-31b",
    )
    new_msgs = [system, *tool_defs, synthetic_summary_msg(summary), *tail]
    await session.replace_messages(new_msgs)                    # backend-specific
    bridge_log(f"⟳ compacting context ({filled}→{est(new_msgs)} tokens)")
```

### Per-backend delivery

| Backend | How to apply the compacted history |
|---|---|
| **OC** | Two options. (a) `POST /session/<id>/summarize` — cedes control of template and timing to OC; bridge just triggers it. (b) Rotate to a new session, inject summary as first system+user pair. **Recommend (a)** for fidelity to OC's structural template, **but** patch upstream to accept a `force=true` body flag and to return the resulting summary so the bridge can re-use it on other backends. |
| **CC** | Inject `/compact <focus>` as a user message; CC handles the rest. For cross-backend continuity, also write the OC-style summary into the session's working directory as `.tg-bridge/last-summary.md` so the model sees it via CLAUDE.md include. |
| **Codex** | No in-session compaction. Rotate to new Codex session; first user message is the OC-template summary, followed by the actual user prompt. |
| **Hermes** | Already auto-compacts. Bridge does nothing except read `last_prompt_tokens` from the response for its own ratio tracking. |

### Cross-backend continuity
Bridge maintains a single canonical summary blob per logical conversation (independent of backend session IDs). Switching backends (`/cc` after `/oc`) replays `[system, last_summary, last_2_turns]` to the new backend.

---

## 6. Open questions (spikes)

1. **Can we tokenise without a tokenizer round-trip per backend?** OC uses a `Token` util that approximates 1 token ≈ 4 chars for Anthropic models. Hermes uses `estimate_messages_tokens_rough`. Bridge needs one shared approximator that's "wrong by ≤10%" across models — acceptable if we set the 0.70 trigger conservatively.
2. **Does `POST /summarize` work on a session mid-stream**, or only when idle? Need to read OC's processor.ts:542 path and check whether it queues vs rejects.
3. **CC `/compact` mid-session**: is it safe to send between assistant turns, or does it require the model to finish? Anthropic docs are silent; needs an experiment.
4. **Codex session rotation**: does the Codex CLI accept a "seed system message" via flag, or only via stdin? If only stdin, bridge needs a synthetic first user turn.
5. **What's the right tail size?** OC defaults to 2 turns + 40k tokens of tail headroom. For tool-heavy sessions 2 turns is plenty; for chatty/research sessions where each turn is small, 6–8 turns better.

---

## 7. Sources

- Hermes: `~/.hermes/hermes-agent/agent/context_compressor.py`, `agent/context_engine.py`, `agent/model_metadata.py` (local, this checkout)
- OpenCode: `~/git/opencode/packages/opencode/src/session/{compaction,summary,overflow,processor}.ts` — `github.com/sst/opencode` branch `dev`, snapshot 2026-05-17
- OpenClaw: `~/git/openclaw` (HEAD 2026-05-17) — **no compaction implementation in core**; `condenser` plugin stub only
- Claude Code Python clones (clean-room, unofficial):
  - `github.com/terminal-creator/ClaudeCode-Python` — `cc/compact/compact.py`
  - `github.com/GPT-AGI/Clawd-Code` — `src/compact_service/`, `src/context_system/microcompact.py`
  - `github.com/AnthonyAlcaraz/claude-code-python-rewrite` — Python + Rust rewrite, architectural study
- Anthropic docs:
  - `docs.anthropic.com/en/docs/build-with-claude/prompt-caching`
  - `docs.anthropic.com/en/docs/about-claude/context-windows`
  - `docs.anthropic.com/en/docs/claude-code/overview` (auto-compact, `/compact`)
- Vaswani et al., "Attention Is All You Need", arxiv:1706.03762

---

## Brainstorm pass

Drafted, then asked `codex exec` to critique. Top three changes adopted:

1. **OpenClaw section** initially claimed it had a "condenser" implementation. The reviewer flagged this as ungrounded — the repo only has the word in i18n strings and a stub plugin. Updated to say "no reusable implementation exists" with the actual `rg` result.
2. **Attention section** was originally enthusiastic about the analogy. Reviewer pointed out we don't have Q/K/V and shouldn't call it attention. Reframed as "goal-conditioned salience scoring" and demoted the Vaswani citation to inspiration.
3. **`cache.read` framing** — first draft buried the billing-vs-window distinction in a footnote. Reviewer (rightly) said this is the *headline* finding and moved it to §1's opening paragraph.
4. **`codex exec` second pass** flagged overclaims: "the only honest measure" of context size, hard-coded model windows, and absolutist "Codex has no compaction". All softened to "best available proxy", "approximate, pin at startup", and "in the CLI surface examined here".
