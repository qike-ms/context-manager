---
title: tg-bridge — Mission-Driven Pruning Spike
issue: qike-ms/my-ai-skills#5
status: spike-proposal
date: 2026-05-17
---

# Mission-driven pruning (spike proposal)

Companion to `compaction-research.md`. The research doc covers what each backend already does; this doc fleshes out **Qi's novel idea**: have the bridge periodically ask the agent what its current mission is, then use that mission statement as the anchor for what to keep vs drop during compaction.

---

## 1. The idea, fleshed out

### Cadence
Two triggers, whichever fires first:

- **Time-based:** every 8 user turns since the last mission probe.
- **Drift-based:** when cosine similarity between the new user turn and the stored mission falls below `MISSION_DRIFT_THRESHOLD = 0.55` (embedding model: a cheap local one — `bge-small-en` or whatever the bridge already has).
- **Pre-compaction:** always re-probe immediately before a compaction event so the mission is fresh.

### Phrasing (matters a lot)
Bad: "What's your current goal?" → the model answers as if Qi asked, polluting the conversation.

Good: a **side-channel auxiliary call** (separate context, NOT the main session) that gets the last ~10 turns and asks:

> You are a meta-observer. Read the conversation excerpt below and answer in **at most 2 sentences**: what is the user's current mission, and is the assistant still working on it (yes/drifting/abandoned)? Output JSON: `{"mission": "...", "status": "on_track|drifting|abandoned", "confidence": 0.0–1.0}`.

This keeps the probe out of the main session entirely. No pollution, no model-as-actor confusion.

### Response handling
- `on_track, conf ≥ 0.7` → store mission, proceed.
- `drifting, conf ≥ 0.7` → store new mission, **lower the compaction trigger** to 0.55 of window (compact sooner, since old turns are now off-topic ballast).
- `abandoned` → flag for user: bridge sends a Telegram message "↪ mission appears to have changed — confirm new mission or `/resume` previous?"
- `conf < 0.7` → keep previous mission, increment a drift counter; after 3 low-confidence probes, escalate to user.

---

## 2. Mission representation

Three options, ordered by complexity:

| Representation | Pros | Cons | Recommendation |
|---|---|---|---|
| **Plain text** (1–2 sentences) | Trivial; survives serialisation; usable directly in summarizer prompts | Can't score turns numerically against it without embedding | Always store this as the human-readable form. |
| **Structured slots** (`{goal, constraints, anti_goals, success_criteria}`) | Maps cleanly to OC's summary template; usable for downstream tools | Slightly more LLM work per probe; some sessions don't fit (exploration) | Add as optional, populate when status=`on_track` and confidence is high. |
| **Vector** (single embedding of the text mission) | Enables cosine-similarity scoring of every turn | Opaque; needs embedder dependency in bridge | **Required** if we want salience scoring; cheap (~1ms/turn with a local model). |

**Decision:** store all three. Text for humans + prompts, slots when available, vector for scoring.

---

## 3. Pruning algorithm — mission-conditioned scoring

```python
def score_turn(turn, mission_vec, turn_idx, latest_idx) -> float:
    rel = cosine(embed(turn.text), mission_vec)        # 0..1
    rec = math.exp(-0.15 * (latest_idx - turn_idx))    # decay
    # Per-head bumps (these are heuristics, not learned):
    bumps = 0.0
    if turn.has_tool_error:           bumps += 0.15
    if turn.has_user_correction:      bumps += 0.20    # "no, actually..."
    if turn.touched_files:            bumps += 0.10
    if turn.has_decision_marker:      bumps += 0.10    # "let's go with X"
    if turn.tool_name in PROTECTED:   bumps += 1.0     # never drop skill/plan
    return 0.55 * rel + 0.30 * rec + bumps
```

Then: keep top-k turns under the preservation budget; bucket the rest into the summarizer with their scores attached so the summarizer can write longer entries for higher-scored content.

This is **goal-conditioned salience**, not attention. See compaction-research §4 for why we avoid the "attention" label.

---

## 4. Failure modes

### Research mode = exploration is the mission
If Qi says "let's explore why opus-4.7 is hanging on Copilot", every turn that looks like an unrelated tangent might actually *be* the work. Mission-conditioned scoring will wrongly prune exploratory turns.

**Mitigation:** the meta-observer prompt explicitly asks for a `mode` field: `{focused, exploratory, debugging}`. In `exploratory` mode:
- Recency weight goes up (`0.55 * rel` → `0.30 * rel + 0.55 * rec`).
- Turns with high *novelty* (low cosine to all prior turns) get a `+0.15` bump — exploration values surprise.
- The drift detector is **disabled**; in exploration drift is the point.

### Mission probe itself drifts
The auxiliary model could hallucinate a mission. Defence: probe returns `confidence`; below 0.5 we ignore it. Probes also include the **previous** mission text and ask whether it's still valid first — biases toward stability.

### User corrects the agent and we prune the correction
The `has_user_correction` heuristic (regex: `\b(no|actually|wrong|stop|instead)\b` early in user turn + within 2 turns of an assistant tool call) gives such turns a 0.20 bump. Imperfect but cheap.

### Cost
Each probe is one short auxiliary LLM call (~500 input tokens, ~80 output). At 8-turn cadence and local model, free. At 8-turn cadence on a paid model, ~$0.001/probe — negligible.

---

## 5. Prior art

Searched honestly; here's what's real:

- **"Goal-conditioned compression"** — the term exists in RL (`Andrychowicz et al. 2017, Hindsight Experience Replay`, arxiv:1707.01495) but means something completely different (relabelling trajectories). Not directly applicable; the *phrase* is borrowed not the technique.
- **LangChain memory** — has `ConversationSummaryMemory`, `ConversationSummaryBufferMemory`, `VectorStoreRetrieverMemory`, and `ConversationKGMemory`. None take an explicit mission as a parameter. The closest is `EntityMemory`, which extracts entities and summarises per entity — entity-conditioned, not goal-conditioned. (`python.langchain.com/docs/versions/migrating_memory/`)
- **LlamaIndex** — `SummaryIndex` and `DocumentSummaryIndex` summarise documents; the `ChatSummaryMemoryBuffer` summarises chat. No `objective` parameter. The closest goal-conditioning is via the `QueryEngine`'s retrieval step, where the user's current query acts as an implicit goal — but that's per-query, not stable across a session. (`docs.llamaindex.ai/en/stable/module_guides/storing/chat_stores/`)
- **MemGPT / Letta** (`arxiv:2310.08560`) — has a `core_memory` block the user can pin; closest existing primitive to "mission". Bridge could literally write the mission into Letta's `core_memory.human` block if Letta were the backend. Worth knowing about, doesn't apply directly.
- **Anthropic's `/compact <focus>`** — exists in Claude Code. `focus` is a one-time hint, not a maintained mission. Half the idea.
- **OC's `SUMMARY_TEMPLATE`** — has a `## Goal` slot. Closest production analog: the goal is *re-extracted* every compaction from the conversation. Our spike adds *continuous tracking* of that goal between compactions.

**Honest verdict:** the combination (continuous out-of-band mission probe + mission-conditioned salience scoring + mode detection) does not appear to exist as a named technique. It's a small but real composition of known parts.

---

## 6. Minimum viable spike

### Subject
The stuck OC session `ses_1cc9d73aeffecTNOIMJfOQyn7m` (opus-4.7, 154 turns, mentioned in issue #5). Export via `opencode session export <id> > /tmp/stuck-session.json`.

### Procedure
1. Run mission-probe over the export at turns {8, 16, 24, …, 152}. Record extracted mission + confidence at each probe.
2. **Manual scoring:** Qi reads the export and writes the *true* mission at each probe point (~30 min of work). Compute agreement (semantic similarity) with the auto-extracted mission.
3. Run two compaction strategies on the export at the point it would have triggered (~turn 80 by token budget):
   - **Baseline:** OC's existing `SUMMARY_TEMPLATE` over the whole thing.
   - **Mission-conditioned:** score every turn with §3's formula using the auto-mission at turn 80; bucket top-30% into "detailed" summary section, bottom-70% into "compressed" section.
4. **Eval:** feed both compacted contexts + the *actual* turn 81 user prompt into opus-4.7 and compare responses. Eval rubric: does the response (a) reference correct prior decisions, (b) avoid redoing work, (c) stay on the real mission. Three runs each, average.

### Cost
~$2 of opus calls + 30 min of Qi's time + ~1 day of bridge-side prototype code (mission probe + scorer; no integration with the real compactor needed for the spike).

### Success criteria
Mission-conditioned compaction wins or ties on ≥2/3 rubric items, and the auto-extracted mission agrees with Qi's hand-labelled mission ≥80% of the time. If either fails, the idea isn't ready and we ship plain OC-template compaction instead.

---

## 7. Recommendation

Build the spike. **But don't ship mission-driven pruning to production until the spike validates it** — there's real risk it underperforms vs OC's template, which is already structured and battle-tested.

Ship order:
1. **First (no spike needed):** real-context-size tracking + model-window registry + 0.70 trigger + always-call OC's `/summarize` for OC sessions. This alone fixes the 154-turn stuck-session bug.
2. **Second (after spike):** mission probe as a *telemetry* feature — bridge logs the mission + drift status to Telegram even before using it for pruning. Lets Qi sanity-check the extractor in the wild.
3. **Third (after telemetry validation):** mission-conditioned pruning, gated behind a per-session flag.

### Where I pushed back on Qi's framing

- **"Attention-style scoring"** → renamed to "goal-conditioned salience scoring". We don't have Q/K/V, calling it attention overclaims. See compaction-research §4.
- **"Bridge periodically asks the agent its mission"** → changed to *out-of-band auxiliary call*, not an in-session question. Asking in-session pollutes the conversation and confuses the model about who's talking to it.
- **One-shot mission** → made it continuously re-probed with stability bias, since a single probe at session start would go stale fast.

---

## Brainstorm pass

Same as `compaction-research.md`: drafted, ran `codex exec "critique this spike doc for unsupported claims, missing failure modes, and overclaiming"`. Adopted:

1. **Research-mode failure** wasn't in the first draft. Reviewer asked "what happens when exploration *is* the mission?" — added §4's exploratory-mode handling.
2. **Prior art section** initially claimed novelty without searching. Reviewer made me actually look — found MemGPT's `core_memory` and OC's existing `## Goal` slot, downgraded the novelty claim from "new" to "small but real composition of known parts".
3. **Ship order** — first draft proposed building mission-driven first. Reviewer (rightly) said "you have a stuck-session bug *now*; ship the boring fix first, validate the fancy idea on top." Reordered §7.
