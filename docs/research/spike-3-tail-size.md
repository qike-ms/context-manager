---
title: tg-bridge — Spike 3 · Verbatim tail size
issue: qike-ms/my-ai-skills#5
status: draft
date: 2026-05-17
spike: 3
---

# Spike 3 — Right "always-keep verbatim" tail size

Companion to `compaction-research.md` §6.5 ("What's the right tail size?") and `spike-1-2-midstream-safety.md`. OpenCode's `compaction.ts:41` ships `DEFAULT_TAIL_TURNS = 2`. The compaction-research hypothesis was that for **chatty TG-bridge sessions** (where each turn is small and decisions accumulate in conversation, not in tool output), 2 is too aggressive and 6–8 is better. This spike tests that empirically against three real long sessions.

---

## 1. Question

> When the summariser runs, how many trailing turns should we keep **verbatim** (untouched) so the model can resume the in-flight goal without re-deriving recent corrections?

Concretely: replay 3 long sessions through the OC summariser at `tail ∈ {2, 4, 6, 8}`, score the resulting summaries against a quality rubric, recommend a number.

---

## 2. Method

### Sessions

All three are real Hermes sessions from `~/.hermes/sessions/`. No synthetic data. The recent M5 bridge session (S3b) was substituted for the originally-planned `session_20260517_130024_594934.json` (S3) — that one only had 2 turns (it *was* a cron job), giving zero head to summarise. S3b is the longest recent M5 work session.

| id | path | turns | domain | why representative |
|---|---|---|---|---|
| S1 | `session_20260513_203038_779a381a.json` | 62 | TG bridge: setting up H-m4 / H-m5 bots, fleet conventions | chatty, lots of user steering ("don't reset tokens again", FileVault, LaunchAgent vs Daemon) |
| S2 | `session_20260514_182122_f9d4b1.json` | 18 | TG bridge: delete openwebui, then sqlite-vec / status-bar PR | mixed: short admin task escalates into multi-PR workstream |
| S3b | `session_20260517_124413_3754c9.json` | 48 | recent M5 bridge cron + multi-PR work | longest recent M5 session; includes interrupted-turn marker (real edge case) |

### Definition of "turn"

Match OC `compaction.ts:145`–`160` `turns()`: a turn starts at each `role=="user"` message and runs up to the next user message. System messages are excluded from head rendering (they bloat input without informing the summary).

### Pipeline (`/tmp/spike3/run.py`)

1. Split `messages` into turns (mirroring OC's `compaction.ts:145`–`160` `turns()` helper).
2. `head = turns[:-N]`, `tail = turns[-N:]` for `N ∈ {2,4,6,8}`. `tail_turns` is a *ceiling*, not a floor — in OC `select()` will further shrink the tail to fit `preserve_recent_tokens` (see §5 obs 5).
3. Render `head` as a plain transcript. Tool outputs truncated to **2 000 chars** (OC `TOOL_OUTPUT_MAX_CHARS = 2_000`).
4. Send `head` + an **abbreviated** version of OC's `SUMMARY_TEMPLATE` to `claude -p --model sonnet --output-format text`. Tail is **not** sent — in OC the tail is preserved verbatim downstream, so the summariser only sees the head.
5. Save each of the 12 summaries to `/tmp/spike3/<sid>_tail<N>.md` + a meta JSON with head size and summariser latency.

**Approximations vs. real OC behaviour** (matters for honesty about external validity):

- The harness's `SUMMARY_TEMPLATE` (`run.py:18`–`45`) drops the `<template>` XML tags and the `Rules:` block from OC's verbatim template (`compaction.ts:44`–`79`). The `Rules:` block tells the model *"Preserve exact file paths, commands, error strings, and identifiers"* — its absence almost certainly explains the across-the-board R4 weakness with `~/` shorthand. Real OC summaries would score higher on R4.
- The harness's `turns()` does **not** filter out messages with a `compaction` part (OC `compaction.ts:150` does). No real session in this corpus contained a prior compaction marker, so this didn't fire — but it would diverge on multi-compaction sessions.
- System messages are stripped at the harness layer; OC keeps them and passes through `MessageV2.toModelMessagesEffect` which handles them differently. Negligible effect on summary quality (system messages are mostly boilerplate), non-zero on token counts.
- Summariser is `claude -p sonnet` (CLI, print mode), not the Messages API call OC actually issues. The CLI may inject a default system prompt; we did not pin temperature.

Input-size guard: hard cap 120 000 tokens (~480 000 chars). Every run came in well under (largest = 293 KB for S3b tail=2) so no downsampling triggered for the headline runs.

### Summariser & scorer

- **Summariser:** `claude -p sonnet`. Same family as the in-session OC compaction agent default; closest available proxy.
- **Scorer:** `codex exec` with `gpt-5` (different family, different vendor) to reduce summariser-self-scoring bias. The Anthropic side hit its 5 PM rate limit mid-run, which forced the cross-family choice — but it's a happy accident: cross-vendor scoring is the correct design here.
- Scorer prompt = original head transcript + candidate summary + rubric (below). Returns one JSON line.

### Rubric (each 0 | 1 | 2)

| axis | what it measures |
|---|---|
| R1 | User corrections preserved (explicit "no / do X instead" → in Constraints or Decisions) |
| R2 | Recent tool calls reflected (last 5 calls before the tail in Done / In Progress) |
| R3 | In-flight goal captured (Goal + In Progress + Next Steps match where the session was paused) |
| R4 | File paths preserved (full paths in Relevant Files, not vague refs) |
| R5 | No hallucinated content (3 random claims spot-checked against the transcript) |

Max per summary = **10**.

---

## 3. Sessions × tail summaries (raw, audit-able)

All 12 are on disk at `/tmp/spike3/` (local artifact dir, not committed to vault). Embedded here for review with **bot tokens redacted** (`<digits>:***REDACTED***`). Discord application IDs and channel IDs are left in — they are not credentials and become public once a bot joins a server. If you spot any remaining sensitive value, raise an issue and we'll redact + force-push.

<details><summary><b>S1 · tail=2</b> — head=60 turns, 160133 chars sent, latency 37.6s</summary>

```markdown
## Goal
- Set up Hermes gateway bots (Telegram + Discord) on m4 and m5 machines, configure SOUL.md personality, and document fleet conventions.

## Constraints & Preferences
- Never ask Qi to do manual work; only acceptable manual asks are password/2FA entry
- Do not reset Discord bot tokens again
- `hermes gateway restart` must keep working (ruled out LaunchDaemon approach)
- `~/git/oc-agent-life/config/AGENTS.md` is master rules file, overrides all other docs
- All fleet repos live at `~/git/<repo>` on every machine

## Progress
### Done
- SOUL.md copied from `~/clawd/SOUL.md` on lan-emma → `~/.hermes/SOUL.md` on m4; identity changed from Emma → M4
- Discord bot **H-m4** created (app ID `1504544456815280188`, username `h_m4_tgram_bot`); all 3 privileged intents enabled; added to Happy server; token written to `~/.hermes/.env`; m4 gateway connected (`H-m4#4251`)
- Telegram bot privacy disabled for H-m4 (can_read_all_group_messages: true)
- m5 (`lan-m5`, user `emma`) fully synced from m4: `.env`, `config.yaml`, `SOUL.md` (M4→M5), profiles
- Telegram bot **H-m5** (`@h_m5_bot`, token `8718403065:***REDACTED***`) written to m5 `.env`
- Discord bot **H-m5** created (app ID `1504584587945181376`, `H-m5#4399`); all intents enabled; added to Happy server; token written to m5 `.env`
- m5 gateway installed as LaunchAgent (`hermes gateway install`); confirmed running (Telegram ✓, Discord ✓, API server ✓)
- LaunchDaemon experiment reverted; m5 back to LaunchAgent
- Auto-login attempted but blocked by FileVault on m5 — accepted limitation (option b)

### In Progress
- (none)

### Blocked
- m5 FileVault is ON → auto-login impossible → gateway only starts after manual login post-reboot

## Key Decisions
- **Telegram forum topics (not profiles)** chosen for per-project context isolation — lighter weight, shares memory/skills across topics
- **Separate bot per machine** (not shared tokens) — one Discord/Telegram token = one gateway connection, sharing causes collisions
- **LaunchAgent over LaunchDaemon** — keeps `hermes gateway restart` working natively; LaunchDaemon breaks the CLI tooling
- **FileVault tradeoff accepted** — m5 gateway will be down after reboot until login; reboots are rare

## Next Steps
- Disable Telegram privacy mode for H-m5: BotFather → `/setprivacy` → `h_m5_bot` → Disable (so H-m5 can read all group messages, not just @mentions)
- Add H-m5 to shipbench group (or Happy server) with Admin role for full perms
- Optionally alias `sudo launchctl kickstart -k system/ai.hermes.gateway` on m5 if daemon approach is reconsidered later

## Critical Context
- m5 user is `emma` (not `qike`); paths are `/Users/emma/.hermes/`
- Discord bot creation requires human captcha + 2FA solve in Chrome window; CDP automation handles the rest
- Playwright works via `/tmp/*.py` scripts run with `python3` (system Python at `/usr/local/bin/python3`); `execute_code` sandbox does NOT have playwright — always use `terminal` + file-based scripts
- Chrome CDP debug session runs on `http://127.0.0.1:9222` (started with `--user-data-dir=/tmp/cdp-discord`); still active at end of session
- H-m4 Discord token: provided by user directly (captured token was only 70 chars / truncated by regex — always use clipboard or user paste)
- DISCORD_HOME_CHANNEL on m5 `.env` line 412 points to `1504547166323212512` (copied from m4; may need updating for m5's home)

## Relevant Files
- `/Users/qike/.hermes/SOUL.md` — m4 personality (identity: M4)
- `/Users/qike/.hermes/.env` — m4 gateway config (Telegram + Discord tokens, API server key)
- `/Users/emma/.hermes/.env` — m5 gateway config (H-m5 Telegram/Discord tokens)
- `/Users/emma/.hermes/SOUL.md` — m5 personality (identity: M5)
- `~/Library/LaunchAgents/ai.hermes.gateway.plist` — gateway LaunchAgent on both machines
- `/tmp/dc_*.py`, `/tmp/dc_check.py` — Playwright CDP scripts for Discord automation (still on m4)
- `~/git/oc-agent-life/config/AGENTS.md` — master rules file (fleet-wide)
```

</details>

<details><summary><b>S1 · tail=4</b> — head=58 turns, 154673 chars sent, latency 38.8s</summary>

```markdown
## Goal
- Set up Hermes gateway bots (Telegram + Discord) on m4 and m5, with proper config/identity, running as system services.

## Constraints & Preferences
- Qi hates manual work — only ask her for passwords/2FA, automate everything else
- Don't ask Qi to do clicks or checklists
- Be concise, push back on bad decisions
- Master rules file: `~/git/oc-agent-life/config/AGENTS.md` — overrides all conflicts
- Repos live at `~/git/<repo>` on every fleet machine
- Don't reset Discord bot tokens without explicit permission

## Progress
### Done
- Copied `~/clawd/SOUL.md` from lan-emma → `~/.hermes/SOUL.md` on m4, renamed "Emma" → "M4"
- Diagnosed and fixed Telegram group privacy issue (bots showed "has no access to messages")
- Created Discord bot **H-m4** (app ID `1504544456815280188`) via Playwright/CDP on local Chrome
- Enabled all 3 privileged intents (Presence, Server Members, Message Content) for H-m4
- Added H-m4 to "Happy server" (guild `1476205423051673691`), assigned admin role
- Wrote valid H-m4 token to `~/.hermes/.env`, restarted m4 gateway — Discord ✓ connected
- Synced m4's `.env`/`config.yaml`/`auth.json`/`SOUL.md`/`profiles` to lan-m5 (user: `emma`)
- Created Telegram bot `@h_m5_bot` (token provided by Qi, token written to m5 `.env`)
- Created Discord bot **H-m5** (app ID `1504584587945181376`) via same Playwright flow
- Enabled all 3 intents, added H-m5 to Happy server, wrote token to m5 `.env`
- m5 gateway up: Telegram ✓, Discord ✓ (H-m5#4399), API server ✓
- Converted m5 gateway from user LaunchAgent → system **LaunchDaemon** (`/Library/LaunchDaemons/ai.hermes.gateway.plist`, `UserName=emma`), runs at boot — removed old user plist

### In Progress
- (none)

### Blocked
- (none)

## Key Decisions
- **Separate Discord bot per machine** (H-m4, H-m5) — one token = one WebSocket connection; shared token causes collision/dropped events
- **LaunchDaemon over LaunchAgent on m5** — user asked for boot-time start (no login required); daemon runs as `emma` to preserve `~/.hermes` path ownership
- **Telegram forum topics** chosen over Hermes profiles for project separation — lighter weight, shared memory/skills still apply across topics

## Next Steps
- Disable Telegram group privacy for `@h_m5_bot` via BotFather (`/setprivacy` → h_m5_bot → Disable) if Qi wants it in group chats reading all messages
- Note: `hermes gateway install/restart` CLI on m5 will target the LaunchAgent path, not the daemon — use `sudo launchctl kickstart -k system/ai.hermes.gateway` to restart m5's gateway; optionally alias this

## Critical Context
- Discord bot token for H-m4 was reset multiple times; final working token confirmed via `/api/v10/users/@me` returning `"username":"H-m4"`
- Discord gateway was timing out (30s) until all 3 privileged intents were properly saved; root cause was `PrivilegedIntentsRequired` error in `gateway.error.log`
- m5 user is `emma` (not `qike`) — all paths on m5 are `/Users/emma/...`
- `DISCORD_HOME_CHANNEL` on m5 is `1504547166323212512` (carried over from m4 config copy)
- Local Chrome on m4 at `http://127.0.0.1:9222` (profile `/tmp/cdp-discord`) still has Discord Developer Portal session active

## Relevant Files
- `/Users/qike/.hermes/SOUL.md` — m4 agent identity (was Emma, changed to M4)
- `/Users/qike/.hermes/.env` — m4 bot tokens (Telegram + Discord H-m4)
- `/Users/emma/.hermes/.env` — m5 bot tokens (Telegram h_m5_bot + Discord H-m5)
- `/Library/LaunchDaemons/ai.hermes.gateway.plist` — m5 system daemon plist (boot-time start)
- `/tmp/dc_*.py`, `/tmp/m5_*.py` — Playwright scripts used for Discord automation (m4's `/tmp`)
- `~/git/oc-agent-life/config/AGENTS.md` — master agent rules file (fleet-wide)
```

</details>

<details><summary><b>S1 · tail=6</b> — head=56 turns, 147560 chars sent, latency 30.7s</summary>

```markdown
## Goal
- Set up Hermes Agent gateway (Telegram + Discord bots) on m4 and m5, configure per-project Telegram topics for context separation, and apply a consistent SOUL.md persona across the fleet.

## Constraints & Preferences
- No manual work for Qi except passwords/2FA; automate everything else
- Do not reset Discord bot tokens without explicit permission
- `~/git/oc-agent-life/config/AGENTS.md` is the master rules file — overrides all other docs
- All repos live at `~/git/<repo>` on every fleet machine
- Qi wants concise, opinionated responses; push back on bad decisions

## Progress
### Done
- Copied `~/clawd/SOUL.md` from Emma → `~/.hermes/SOUL.md` on m4; replaced "Emma" with "M4"
- Saved memory: Qi hates manual work; repo conventions (`obsidian-vault`, `oc-agent-life`, `my-ai-skills`, `swe-skills`, master rules at `~/git/oc-agent-life/config/AGENTS.md`)
- Disabled Telegram privacy mode for both m4 and Emma bots; confirmed both show up correctly in "shipbench" group
- Created Discord bot **H-m4** (app ID `1504544456815280188`) via Playwright/CDP; enabled all 3 privileged intents; added to "Happy server"; set `DISCORD_BOT_TOKEN` in `~/.hermes/.env`; gateway connected as H-m4#4251
- Created Discord bot **H-m5** (app ID `1504584587945181376`); enabled all 3 privileged intents; added to "Happy server"
- Synced m4's `.env`, `config.yaml`, `auth.json`, `profiles/`, `SOUL.md` → m5 (`lan-m5`, user `emma`); patched Telegram token to `h_m5_bot` (`8718403065:***REDACTED***`); wrote Discord token; installed and started m5 gateway
- m5 gateway confirmed: ✓ Telegram (H-m5 `@h_m5_bot`), ✓ Discord (H-m5#4399), ✓ API server

### In Progress
- (none)

### Blocked
- (none)

## Key Decisions
- **Telegram: Forum Topics (not Profiles)** for per-project context separation — lighter weight; profiles reserved for cases needing walled-off memory/skills/model
- **Separate Discord bot per machine** — one token = one connection; sharing tokens causes event collisions
- **Token capture via user paste** — Playwright clipboard approach unreliable, regex truncated tokens; user pasted tokens directly

## Next Steps
- Disable Telegram privacy mode for `@h_m5_bot` via @BotFather (`/setprivacy` → Disable) so H-m5 reads all group messages, not just @mentions
- Assign H-m5 the "bots" admin role in Happy server (Server Settings → Members → H-m5)
- Set up per-project Telegram forum topics in groups and run `/sethome` in each topic for cron delivery targeting
- (Optional) Investigate agent-to-agent messaging solution for bots to see each other's messages — HTTP cross-post via gateway bridge sketched but not implemented

## Critical Context
- m5 runs as user `emma` at `~/.hermes/`; m4 is user `qike`
- Discord bot tokens are single-use display — once the modal closes, the token is gone; never reset without user approval
- Discord gateway `PrivilegedIntentsRequired` error requires all 3 intents enabled: Presence, Server Members, Message Content
- Bots cannot see other bots' messages on Telegram (platform restriction); Discord same limitation
- Chrome CDP session running on m4 at `http://127.0.0.1:9222` with Discord Developer Portal logged in (may still be running)
- Discord `DISCORD_HOME_CHANNEL` on m5 copied from m4's env (`1504547166323212512`) — this may point to the wrong channel

## Relevant Files
- `/Users/qike/.hermes/.env` — m4 bot tokens (Telegram, Discord)
- `/Users/emma/.hermes/.env` — m5 bot tokens (patched)
- `/Users/qike/.hermes/SOUL.md` — m4 persona (M4 identity)
- `/Users/emma/.hermes/SOUL.md` — m5 persona (M5 identity, sed'd from M4)
- `~/git/oc-agent-life/config/AGENTS.md` — master fleet rules file
- `/tmp/dc_*.py`, `/tmp/m5_*.py` — Playwright scripts used for Discord automation (ephemeral)
```

</details>

<details><summary><b>S1 · tail=8</b> — head=54 turns, 140422 chars sent, latency 20.8s</summary>

```markdown
## Goal
- Set up Discord bot H-m5 on lan-m5 (MacBook M5, user `emma`) mirroring m4's Hermes config, then install Hermes gateway with both Telegram and Discord bots.

## Constraints & Preferences
- Never reset Discord bot tokens again (already done it too many times)
- Don't ask Qi for manual work except passwords/2FA
- Be concise; push back on bad decisions

## Progress
### Done
- Copied `~/.hermes/.env`, `config.yaml`, `SOUL.md`, `auth.json`, `profiles/` from m4 → m5 via scp
- Swapped Telegram token in m5's `.env` to `h_m5_bot` (8718403065:***REDACTED***) — verified valid
- Removed m4's Discord token from m5's `.env` (placeholder until H-m5 bot created)
- Replaced "M4" → "M5" throughout m5's `SOUL.md`
- Navigated to Discord Developer Portal to create H-m5 app

### In Progress
- Creating H-m5 Discord application — **captcha intercepted** the "New Application" flow again

### Blocked
- Captcha on Discord Developer Portal (user needs to solve it in local Chrome window at `http://127.0.0.1:9222`)

## Key Decisions
- Each machine gets its own Discord bot token (H-m4, H-m5) — shared token causes gateway collisions
- Telegram bot `h_m5_bot` already created by Qi; token verified
- Copy m4's full config to m5 and patch tokens (rather than fresh setup)
- SOUL.md identity: "M5 — Qi's work assistant"

## Next Steps
1. Qi solves captcha in Chrome (discord.com/developers/applications), says "done"
2. Create H-m5 application + bot, reset token, enable all 3 Privileged Intents (Presence, Server Members, Message Content), save
3. Invite H-m5 to Happy server (same guild `1476205423051673691`) via OAuth URL
4. Write Discord token to m5's `~/.hermes/.env`
5. Install/start Hermes gateway on m5 (`hermes gateway install && hermes gateway restart`)
6. Verify both Telegram and Discord connections in m5's gateway logs

## Critical Context
- Chrome CDP debug port running on m4 at `http://127.0.0.1:9222`, logged into Discord Developer Portal
- H-m4 Discord app ID: `1504544456815280188`; guild ID for Happy server: `1476205423051673691`
- Playwright scripts for bot creation are in `/tmp/dc_*.py` on m4
- Discord bot creation triggers captcha after ~1 new app; requires manual solve in Chrome window
- `execute_code` tool doesn't have `playwright` module — must use `/tmp/*.py` scripts via `terminal`
- m5 SSH alias: `lan-m5`; Hermes path: `~/.hermes/` (user `emma`)
- m5 already had Hermes v0.13.0 installed; backup of original config skipped (auth.json and profiles didn't exist yet)

## Relevant Files
- `/Users/qike/.hermes/.env` — m4 gateway config (source for copy)
- `/Users/emma/.hermes/.env` on lan-m5 — patched with new Telegram token, Discord token cleared
- `/Users/emma/.hermes/SOUL.md` on lan-m5 — M4→M5 substitution applied
- `/tmp/dc_create_m5.py` — Playwright script to create H-m5 Discord app
- `/tmp/dc_check.py` — Playwright script to inspect current page state on CDP Chrome
- `/tmp/dc_intents4.py`, `/tmp/dc_token2.py` etc. — reusable bot setup scripts from H-m4 flow
```

</details>

<details><summary><b>S2 · tail=2</b> — head=16 turns, 125391 chars sent, latency 36.3s</summary>

```markdown
## Goal
- Multi-session maintenance work: cleaned up machine (OpenWebUI, Qdrant), investigated memory/search options, shipped a Hermes status-bar session-title PR, monitored older PRs, ran coding LLM benchmark analysis, and explored session-title toggle feature.

## Constraints & Preferences
- SSH to `lan-cora` requires explicit user approval each time
- No unrelated changes bundled in PRs (feishu deletions must stay off the PR branch)
- PR must strictly follow NousResearch/hermes-agent CONTRIBUTING.md conventions

## Progress
### Done
- Removed OpenWebUI (volume `open-webui`, image 6.04GB) and Qdrant (container, volume `qdrant_data`, image) from local machine
- Confirmed Hermes session/message search uses SQLite FTS5 (BM25 + trigram) — no embeddings, no vector DB
- Researched hive-memory: it's a schema-only spike, not deployable; canonical Hive memory is Mem0 OpenMemory via MCP
- Researched vector store options for semantic session search; decision: **sqlite-vec** (same file as state.db, hybrid FTS5+vec, zero new processes)
- Added session title to Hermes CLI status bar (`cli.py` + `config.py`), with `display.status_bar_title` toggle (default `True`), `_pending_title` fallback, 5 new passing tests
- Opened upstream PR **https://github.com/NousResearch/hermes-agent/pull/25817** `feat/cli-status-bar-session-title` from fork `qike-ms`
- Confirmed 2 prior PRs (#14664, #13818) were closed as implemented/duplicate (correct fixes, landed via competing PRs first)
- Set up cron watcher job `3564456447ba` ("PR-25817 watcher") running 4×/day via `~/.hermes/scripts/pr_25817_watch.sh`, delivering to Telegram; baseline saved to `~/.hermes/cache/pr_25817_baseline.json`
- Summarized coding LLM benchmark results: May-8 autopilot suite winner = deepseek-coder-v2:16b (9/12, 2.4s avg); rigorous benchmark R3 JS = gemma4:31b (13/40) ≈ qwen3.6:27b (12/40)
- Confirmed `cora-coding-bench-autopilot` and `coding-llm-benchmark` are complementary, not duplicates

### In Progress
- PR #25817 awaiting upstream review
- `coding-llm-benchmark` Phase 1 (multi-language Aider/LCB) not yet run; only smoke + R3 (JS) done

### Blocked
- Hermes semantic search spike (option 1: sqlite-vec sidecar) — design agreed, not started; blocked on SSH approval to cora for model inventory + embedding model pulls

## Key Decisions
- **sqlite-vec** chosen for semantic session search (co-located in state.db, hybrid FTS5+cosine, no new process)
- **4 embedding model shortlist**: nomic-embed-text, mxbai-embed-large, bge-m3, qwen3-embedding:4b
- **`display.status_bar_title` toggle kept** — matches convention of 8 sibling boolean display flags; no `_config_version` bump needed (additive only, `_deep_merge` backfills)
- **deepseek-coder-v2:16b** should be added as 4th model to rigorous benchmark Phase 1 to validate the autopilot suite result

## Next Steps
1. Approve SSH to `lan-cora` so embedding model inventory + pulls can proceed for semantic search spike
2. Scaffold `~/git/hermes-semantic-search` repo (src/embed.py, index.py, search.py, eval.py; data/corpus.jsonl, qrels.json)
3. User to confirm/trim 4-model shortlist (nomic / mxbai / bge-m3 / qwen3-4b) for embedding eval
4. Author qrels.json (~25 hand-labelled query→doc-id pairs, 5 categories) — user must verify relevance judgements
5. Optionally: amend `coding-llm-benchmark/PLAN.md` to add deepseek-coder-v2:16b as 4th model for Phase 1

## Critical Context
- Hermes state.db path: `~/.hermes/state.db`; 1,857 messages, 56 sessions, ~16MB, FTS5 via `messages_fts` + `messages_fts_trigram`
- PR watcher cron: job ID `3564456447ba`, script `~/.hermes/scripts/pr_25817_watch.sh`, 4× daily, delivers to Telegram; fires guaranteed reminder 2026-05-16 09:00 if PR still untouched
- Local stash `local-wip-status-bar-and-feishu` on `~/.hermes/hermes-agent` preserves prior feishu deletions and earlier local edits; `git stash pop` to restore
- Hermes repo is 94 commits behind origin/main; fast-moving (~50 PRs/day) — speed to PR is key to avoiding duplicate-close
- `~/git/cora-coding-bench-autopilot` repo is **not cloned locally**; only project notes at `~/git/obsidian-vault/projects/cora-coding-bench-autopilot/ctx.md`
- `~/git/coding-llm-benchmark` repo is **not cloned locally**; project notes + PLAN/PROGRESS/HANDOFF at `~/git/obsidian-vault/projects/coding-llm-benchmark/`; actual run data lives on cora at `~/code-bench/` and `~/bench-results/`

## Relevant Files
- `~/.hermes/hermes-agent/cli.py`: status bar snapshot + render logic (lines ~2837–3120)
- `~/.hermes/hermes-agent/hermes_cli/config.py`: `DEFAULT_CONFIG["display"]` (line ~937), `_config_version=23`
- `~/.hermes/hermes-agent/tests/cli/test_cli_status_bar.py`: status bar tests (39 tests, 5 new)
- `~/.hermes/scripts/pr_25817_watch.sh`: PR cron watcher script
- `~/.hermes/cache/pr_25817_baseline.json`: baseline snapshot for change detection
- `~/git/obsidian-vault/projects/coding-llm-benchmark/PROGRESS.md`: WI completion status for rigorous benchmark
- `~/git/obsidian-vault/projects/hive/README.md`: Hive memory canonical decision (Mem0 OpenMemory)
- `~/git/hive-memory/src/schema.py`: schema-only spike (not deployable)
```

</details>

<details><summary><b>S2 · tail=4</b> — head=14 turns, 113754 chars sent, latency 38.9s</summary>

```markdown
## Goal
- Set up semantic search for Hermes session history (embedding sidecar over state.db) and contribute a session-title status bar feature upstream to NousResearch/hermes-agent.

## Constraints & Preferences
- Keep Hermes session data local; no new persistent services unless justified
- Upstream PRs must strictly follow CONTRIBUTING.md conventions (Conventional Commits, branch naming, tests, focused scope)
- User communicates with Hermes via Telegram on this machine
- SSH to `lan-cora` requires explicit approval each time

## Progress
### Done
- Removed open-webui (volume + image); removed qdrant container, volume, image
- Researched vector store options; chose **sqlite-vec** (same file as state.db, hybrid FTS5+vec, no new infra)
- Shortlisted embedding models: nomic-embed-text, mxbai-embed-large, bge-m3, qwen3-4b
- Designed eval harness (corpus from state.db, 25 hand-built queries, nDCG@10 + Recall@10 + MRR + latency, hybrid RRF scoring)
- Added session title to CLI status bar: `_get_status_bar_snapshot` reads `SessionDB.get_session_title()` with `_pending_title` fallback; `_build_status_bar_text` prepends `❝title❞ ` at all width breakpoints
- Added `display.status_bar_title: True` toggle to `DEFAULT_CONFIG` in `hermes_cli/config.py` (verified pattern matches 8 sibling bools; no config version bump needed)
- Added 5 tests to `tests/cli/test_cli_status_bar.py`; all 44 tests pass
- Opened PR #25817: https://github.com/NousResearch/hermes-agent/pull/25817
- Created cron watcher job `3564456447ba` ("PR-25817 watcher"), 4×/day, Telegram delivery; baseline saved at `~/.hermes/cache/pr_25817_baseline.json`

### In Progress
- PR #25817 awaiting upstream review (0 comments, 0 reviews as of submission)

### Blocked
- Semantic search eval blocked on SSH access to `lan-cora` (user denied ssh; needs approval to pull embedding models and run bake-off)

## Key Decisions
- **sqlite-vec over Qdrant/LanceDB/Chroma**: zero new processes, vectors live in same state.db, hybrid SQL query, sufficient at <100k rows
- **Toggle kept** (`display.status_bar_title`): confirmed it matches upstream convention (8 sibling display bools); no version bump needed since `_deep_merge` backfills additive keys
- **Title prefix uses `❝❞` quotes** and trims gracefully at narrow widths via existing `_trim_status_bar_text`
- **Feishu deletions isolated**: stashed as `local-wip-status-bar-and-feishu`; PR branched clean from `origin/main`

## Next Steps
1. Approve SSH to `lan-cora` → run inventory (`nvidia-smi`, `ollama list`) → pull 4 embedding models → run bake-off
2. Scaffold `~/git/hermes-semantic-search` repo with eval harness
3. Build ground-truth `qrels.json` (hand-label ~25 queries against state.db corpus)
4. Wait for PR #25817 review; cron watcher will Telegram-notify on any activity; if no activity by 2026-05-16 9am, watcher sends guaranteed reminder

## Critical Context
- Hermes state.db: 1,857 messages, 56 sessions, ~16MB; FTS5 + trigram already in place for lexical search
- Two earlier PRs (#13818, #14664) were closed as "already landed" — not rejected on merit; repo ships ~50 PRs/day, speed-to-PR is the lever
- `_config_version` is 23; migrations only bump when keys rename/remove; pure additive is safe
- Pyright errors in cli.py and config.py diffs are pre-existing, not introduced by the edits
- Local stash `local-wip-status-bar-and-feishu` contains feishu tool deletions (unrelated local cruft)

## Relevant Files
- `~/.hermes/hermes-agent/cli.py`: status bar snapshot + render (lines ~2837–3120)
- `~/.hermes/hermes-agent/hermes_cli/config.py`: DEFAULT_CONFIG display section (line ~937)
- `~/.hermes/hermes-agent/tests/cli/test_cli_status_bar.py`: status bar tests (617 lines)
- `~/.hermes/hermes-agent/agent/title_generator.py`: existing auto-title generation (background thread, first exchange)
- `~/.hermes/state.db`: Hermes session/message store (corpus for semantic search eval)
- `~/.hermes/scripts/pr_25817_watch.sh`: cron watcher script
- `~/.hermes/cache/pr_25817_baseline.json`: PR state baseline for change detection
- `~/git/hive-memory/`: hive-memory MVP (schema-only, not suitable for deployment as-is)
- `~/git/obsidian-vault/projects/hive/`: Hive project docs (canonical memory backend = Mem0 OpenMemory via MCP)
```

</details>

<details><summary><b>S2 · tail=6</b> — head=12 turns, 107944 chars sent, latency 35.7s</summary>

```markdown
## Goal
- Build semantic session search for Hermes by adding a local embedding sidecar over `state.db`, and improve the Hermes CLI status bar to show AI-generated session titles.

## Constraints & Preferences
- Keep everything local/on-device; no new persistent server processes unless necessary
- PRs to NousResearch/hermes-agent must follow CONTRIBUTING.md strictly (Conventional Commits, focused PRs, tests, branch naming, no unrelated changes)
- SSH to `lan-cora` requires explicit user approval each time

## Progress
### Done
- Removed Open WebUI (volume `open-webui`, image `ghcr.io/open-webui/open-webui:main`) — no container existed
- Removed Qdrant (container `qdrant`, volume `qdrant_data`, image `qdrant/qdrant`); source file `~/git/hive/memory/qdrant_backend.py` left intact
- Researched hive-memory repo — it's a schema-and-tests-only MVP, not deployable; canonical Hive memory direction is Mem0 OpenMemory via MCP
- Researched vector store options; selected **sqlite-vec** (same `state.db` file, zero new processes, hybrid FTS5+vec in one SQL query)
- Shortlisted 4 embedding models for bake-off: `nomic-embed-text`, `mxbai-embed-large`, `bge-m3`, `qwen3-embedding:4b`
- Designed full eval harness: corpus from `state.db` (1,857 msgs / 56 sessions), 25-query gold set, nDCG@10 + Recall@10 + MRR + latency metrics, hybrid RRF scoring
- Added session title to Hermes CLI status bar: `_get_status_bar_snapshot` reads DB title with `_pending_title` fallback, gated by `display.status_bar_title` toggle; `_build_status_bar_text` prepends `❝title❞ ` at all width breakpoints
- Added `display.status_bar_title: True` to `DEFAULT_CONFIG` in `hermes_cli/config.py`
- Added 5 new tests in `tests/cli/test_cli_status_bar.py`; 44/44 pass
- Opened upstream PR **#25817**: https://github.com/NousResearch/hermes-agent/pull/25817
- Investigated prior PRs #14664 and #13818 — both closed as duplicate (correct fixes, beaten to merge by competing PRs)

### In Progress
- Semantic search eval harness design is complete; awaiting SSH access to `lan-cora` to pull embedding models and run benchmark

### Blocked
- SSH to `lan-cora` — user needs to approve (or run inventory one-liner and paste output); eval cannot proceed without it

## Key Decisions
- **sqlite-vec over Qdrant/LanceDB/Chroma**: same file as `state.db`, no sync job, brute-force cosine is sub-ms at <100k rows
- **Toggle kept**: `display.status_bar_title` matches 8 sibling boolean display config keys; no `_config_version` bump needed (purely additive, `_deep_merge` backfills)
- **Branch isolated from local cruft**: stashed feishu deletions (`git stash: local-wip-status-bar-and-feishu`) before creating the PR branch off fresh `origin/main`

## Next Steps
1. Get SSH access to `lan-cora` approved (or user pastes `nvidia-smi; ollama list; df -h /` output)
2. Scaffold `~/git/hermes-semantic-search` repo with `src/embed.py`, `src/index.py`, `src/search.py`, `src/eval.py`
3. Snapshot corpus from `state.db` → `data/corpus.jsonl`
4. Author `data/qrels.json` (25-query gold set across 5 categories)
5. Pull 4 candidate models on cora via `ollama pull`
6. Run bake-off; emit `results/summary.md` with model × metric table + recommendation

## Critical Context
- Hermes `state.db` schema: `messages(id, session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp, token_count, …)` — timestamp is `REAL` (Unix epoch), no `created_at`
- 1,857 messages, 56 sessions, ~16MB; FTS5 (standard + trigram) already indexed via triggers
- `~/.hermes/hermes-agent/` is the live install; `main` branch is 94 commits behind `origin/main` after stash/branch work
- PR #25817 is a new feature (lower duplicate risk than the two prior fix PRs that raced)
- Local stash `local-wip-status-bar-and-feishu` preserves prior local state including feishu deletions

## Relevant Files
- `~/.hermes/hermes-agent/cli.py`: status bar logic (`_get_status_bar_snapshot` ~L2837, `_build_status_bar_text` ~L3062)
- `~/.hermes/hermes-agent/hermes_cli/config.py`: `DEFAULT_CONFIG["display"]` ~L904; new `status_bar_title` key ~L937
- `~/.hermes/hermes-agent/tests/cli/test_cli_status_bar.py`: 5 new tests added at ~L82
- `~/.hermes/hermes-agent/agent/title_generator.py`: existing auto-title generation (no changes needed)
- `~/.hermes/state.db`: Hermes session/message store; corpus source for semantic search eval
- `~/git/hive-memory/`: schema-only MVP, not suitable for deployment
- `~/git/obsidian-vault/projects/hive/`: Hive project docs (PRD, DESIGN, IMPLEMENTATION-PLAN, track5-memory)
```

</details>

<details><summary><b>S2 · tail=8</b> — head=10 turns, 102903 chars sent, latency 34.9s</summary>

```markdown
## Goal
- Remove OpenWebUI/Qdrant from the machine, investigate Hermes session semantic search options, and add session title to the Hermes CLI status bar (with upstream PR).

## Constraints & Preferences
- Follow CONTRIBUTING.md conventions strictly for upstream PRs (Conventional Commits, focused PRs, no unrelated changes)
- No config version bump for purely additive defaults
- Rule 0: be concise

## Progress
### Done
- Deleted OpenWebUI volume (`open-webui`) and image (6.04GB) from Docker
- Deleted Qdrant container, volume (`qdrant_data`), and image; source file `~/git/hive/memory/qdrant_backend.py` left intact
- Researched semantic search options; decided on sqlite-vec + Ollama embeddings sidecar over `state.db`
- Designed full eval plan (4 models: nomic, mxbai, bge-m3, qwen3-4b; sqlite-vec store; nDCG@10 / Recall@10 / MRR metrics; hybrid RRF scoring)
- Added session title to Hermes CLI status bar: `_get_status_bar_snapshot` reads DB title with `_pending_title` fallback, gated by `display.status_bar_title` toggle
- Added `display.status_bar_title: True` to `DEFAULT_CONFIG` in `hermes_cli/config.py`
- Added 5 tests to `tests/cli/test_cli_status_bar.py`; 44/44 pass
- Committed to branch `feat/cli-status-bar-session-title` off fresh `origin/main`
- Pushed to fork `qike-ms/hermes-agent` and opened upstream PR: https://github.com/NousResearch/hermes-agent/pull/25817

### In Progress
- Semantic search eval (sqlite-vec + embedding model bake-off) — designed but not started; blocked on cora SSH access

### Blocked
- SSH to `lan-cora` was denied during session; cora inventory (GPU, ollama models, disk) not completed; eval cannot run until SSH is approved

## Key Decisions
- **No hive-memory deployment**: it's a schema-only MVP with no server/embeddings; Hive's canonical memory backend is Mem0 OpenMemory, not hive-memory; wrong scope for Hermes session indexing
- **sqlite-vec chosen** over Qdrant/LanceDB/Chroma: embeds vectors in existing `state.db`, zero new processes, hybrid FTS5+vec query in one SQL statement
- **Toggle kept** (not removed): verified it matches 8 sibling boolean display keys in `DEFAULT_CONFIG`; no version bump needed since `_deep_merge` backfills additive defaults
- **`_pending_title` fallback**: status bar shows auto-generated title as soon as it's available, before DB commit

## Next Steps
1. Approve SSH to `lan-cora` so cora inventory can run
2. Scaffold `~/git/hermes-semantic-search` repo (embed.py, index.py, search.py, eval.py)
3. Snapshot corpus from `~/.hermes/state.db` → `data/corpus.jsonl`
4. Build qrels ground truth (~25 queries, 5 categories)
5. Pull 4 embedding models on cora via Ollama
6. Run bake-off, produce `results/summary.md` with metric table + recommendation

## Critical Context
- Hermes `state.db`: 1,857 messages, 56 sessions, ~16MB, FTS5+trigram already in place — semantic is the gap
- Stash `local-wip-status-bar-and-feishu` on `main` branch of `~/.hermes/hermes-agent` contains prior local edits including unrelated feishu file deletions — do NOT pop unless intentional
- Pyright errors in `cli.py` and `config.py` are pre-existing (unrelated imports), not introduced by the PR changes
- `_config_version` is currently 23; no bump needed for additive bool defaults

## Relevant Files
- `~/.hermes/hermes-agent/cli.py`: status bar implementation (`_get_status_bar_snapshot` ~line 2837, `_build_status_bar_text` ~line 3062)
- `~/.hermes/hermes-agent/hermes_cli/config.py`: `DEFAULT_CONFIG["display"]` ~line 904, `status_bar_title` added after `show_cost`
- `~/.hermes/hermes-agent/tests/cli/test_cli_status_bar.py`: 617 lines, 5 new tests added at line ~83
- `~/.hermes/state.db`: Hermes session/message store (corpus for semantic search eval)
- `~/git/hive-memory/`: schema-only MVP, no server — not suitable for deployment
- `~/git/obsidian-vault/projects/hive/`: Hive project docs; canonical memory = Mem0 OpenMemory via MCP
```

</details>

<details><summary><b>S3b · tail=2</b> — head=46 turns, 293684 chars sent, latency 56.5s</summary>

```markdown
## Goal
Build and evolve the Telegram agent bridge into a proper "agent-dispatcher" with context management, fix stuck OC sessions, and design fleet orchestration architecture.

## Constraints & Preferences
- Repos live at `~/git/<repo>` (never `~/code/`)
- Obsidian: personal KB at `~/git/obsidian-vault/` root (no `qi/` subdir); `projects/` = idea parking
- Hive: engineering/operational docs at `~/git/hive/docs/`
- NEVER deploy unreviewed code — load `swe-skills/multi-model-pr-review` first
- Long tasks (>5 min) → cron jobs, NOT delegate_task (delegate dies on user message interrupt)
- Skills live in repos (`my-ai-skills/skills/` or `swe-skills/skills/`), never `~/.hermes/skills/`
- Always state log path + commit/push status after file changes
- Auto-invoke review, never ask user; before listing `ls` before grepping

## Progress
### Done
- OC sessions on M5 (emma) and ann restarted; stuck sessions deleted; bridges cleared
- Root cause documented: GitHub Copilot opus-4.7 stream stalls on heavy-cache sessions; OC has no client-side stream timeout; sync bridge has no POST timeout
- Async bridge refactor (scope b): per-chat locks, per-backend semaphores, hard `wait_for` timeouts — cron `0ab1ca8bd0cd` delivered, 3 blocking issues fixed across 3 commits (012ff69, 28749a1, 5cc8c06), Codex + claude-aws cleared (2 of 3 judges valid; OC review was a silent empty session — stdin trap)
- `swe-skills/code-eval` patched with `opencode run` STDIN trap warning (a10b53f)
- `my-ai-skills/skills/autonomous-dev` updated: replaced dead `code-review` + `python-code-review` refs with `code-eval` + `multi-model-pr-review`; `python-code-review` was deleted last night (a1603e3) but stale ref remained
- Architecture doc: `~/git/obsidian-vault/projects/agent-dispatcher/architecture.md` (430c4f8) — continuous compaction, throwaway-PoC pruning (auto /rewind), pluggable MemoryBackend, `~/.agent-dispatcher/` data layout
- Spark 4-node cluster handoff: `~/git/obsidian-vault/projects/spark/handoff.md` (69f3acd)
- Mission-alignment idea logged: `~/git/obsidian-vault/inbox/2026-05-17-mission-alignment-all-agents.md` (200c0e4)
- cot-proxy SKILL.md updated with 5-config Round 2 benchmark including config E; gemma4 plain recommended as default (b6df82f)
- GH issue `qike-ms/my-ai-skills#5` logged: intelligent context compaction (3 phases)
- Telegram group "Hermes M5" (`-1003873388511`) joined; topic `docs-organization` created at `thread_id=46`
- Memory rule updated: always `ls` before grepping, load multi-model-pr-review before reviewing, long tasks → cron

### In Progress
- **Async bridge refactor PR** (`feature/bridge-async-concurrency`, SHA 5cc8c06): OC review skipped (stdin trap discovered); 2-of-3 judges cleared; `autonomous-dev` ref still has dead `code-review` path at line 26/36/203/207 — needs follow-up patch
- **Compaction research cron** `0b0bd07b6e5b`: still running autonomously, no output yet
- **Agent-dispatcher scaffold cron** `b4f2eac73aa2`: scheduled, not yet delivered
- **Doc org sign-off**: proposal sent to Hermes M5 `#docs-organization` (thread_id=46), awaiting approval

### Blocked
- OC re-review of bridge 5cc8c06 skipped per user ("skip re-review, move on") — branch may be deployable but lacks 3rd judge

## Key Decisions
- **Agent-dispatcher architecture**: thin dispatcher owns context+memory only; backend sessions ephemeral (create/call/delete per turn); backends interchangeable mid-conversation
- **MemoryBackend**: pluggable interface (`HermesMemoryBackend`, `SqliteMemoryBackend`, `NoopMemoryBackend`); Hermes is one adapter, not a requirement
- **Compactor**: continuous background worker (never threshold-triggered); keep last N messages verbatim always; throwaway-PoC pruning = auto /rewind via special LLM prompting
- **Telegram topics**: key sessions on `(chat_id, message_thread_id or None)`; DM fallback = None; native Telegram topics, no `/topic` slash commands
- **Data layout**: `~/.agent-dispatcher/` mirrors Hermes convention (one dotdir, all config+data+source)
- **Fleet orchestrator**: Hermes hub-spoke beats Kubernetes (K8s already decommissioned; workloads aren't pods; Hermes has 80% of needed infra)
- **Gemma4:31b plain (config A)** is the default inference recommendation — same 6/6 correctness, 4.4-18× faster than any scaffold/qwen config
- **`opencode run` STDIN trap**: ignores stdin entirely; must use positional arg + `-f` for file input

## Next Steps
1. Fix remaining dead `code-review` refs in `my-ai-skills/skills/autonomous-dev/SKILL.md` (lines 26, 36, 203, 207)
2. Await cron deliveries: compaction research (`0b0bd07b6e5b`) and agent-dispatcher scaffold (`b4f2eac73aa2`)
3. Get doc-org sign-off in Hermes M5 `#docs-organization` then move `projects/agent-dispatcher/` to `~/git/hive/docs/agent-dispatcher/`
4. Open PR for `feature/bridge-async-concurrency` after confirming 2-of-3 review verdict acceptable, or run OC review correctly via `multi-model-pr-review` skill
5. Deploy async bridge to M5 + ann (currently on old sync code, working fine post-OC-restart)
6. Write fleet orchestrator design doc to `~/git/hive/docs/fleet-orchestration/hermes-hub-vs-k8s.md`
7. Discuss dispatcher architecture in Hermes M5 "tg-bridge / dispatcher" topic (thread_id TBD — user needs to send message in each topic)

## Critical Context
- **`opencode run` STDIN trap**: `echo "$DIFF" | opencode run` silently creates empty session, ignores stdin. Use `opencode run "prompt" -f /tmp/diff.patch`. Detection: check `tokens.input` via `/session` API — real reviews show thousands of tokens, empty sessions show ~5
- **OC auto-compact is TUI-only**: headless `opencode serve` never compacts automatically; sessions grow unbounded. `POST /session/<id>/summarize` with `{"providerID": ..., "modelID": ...}` is the server-side API; `POST /session/<id>/abort` returns `true`; `DELETE /session/<id>` fully removes
- **`tokens.cache.read` ≠ context size**: it's cumulative cross-turn billing metric. Real context = `tokens.input` of last turn (OC replays full history each call)
- **Bridge async blockers were**: subprocess leak on `wait_for` cancel, shared `state` dict race, `_chat_locks` setdefault race — all fixed in 5cc8c06
- **`python-code-review` skill**: deleted (a1603e3, last night) but stale refs remain in `autonomous-dev/SKILL.md` lines 26/36/203/207 pointing to `software-engineering/code-review/` path
- **Hermes M5 group**: chat_id=`-1003873388511`; `docs-organization` topic at thread_id=46; 3 other topics exist but thread_ids unknown until first message sent in each

## Relevant Files
- `~/git/my-ai-skills/scripts/telegram-agent-bridge.py` (feature/bridge-async-concurrency, 5cc8c06): async refactor, not yet deployed
- `~/git/obsidian-vault/projects/agent-dispatcher/architecture.md`: full dispatcher+context-manager design
- `~/git/swe-skills/skills/multi-model-pr-review/SKILL.md`: correct multi-model review pattern (acde727, 7f39111)
- `~/git/swe-skills/skills/code-eval/SKILL.md`: STDIN trap warning added (a10b53f)
- `~/git/my-ai-skills/skills/autonomous-dev/SKILL.md`: has 4 remaining dead refs to `code-review`/`software-engineering/code-review/` path
- `~/.hermes/logs/gateway.error.log`: Copilot/opus-4.7 ReadTimeout evidence (was root cause)
- `~/git/my-ai-skills/cot-proxy/SKILL.md`: Round 2 benchmark with all 5 configs (b6df82f)
- `~/git/obsidian-vault/projects/spark/handoff.md`: 4-node DGX Spark bring-up plan for next agent
```

</details>

<details><summary><b>S3b · tail=4</b> — head=44 turns, 281730 chars sent, latency 56.8s</summary>

```markdown
## Goal
Build an agent-dispatcher system (replacing the telegram bridge) with intelligent context management, while fixing the current sync bridge, validating the cot-proxy benchmarks, and researching fleet orchestration approaches.

## Constraints & Preferences
- Repos at `~/git/<repo>`, NEVER `~/code/`
- Skills live in repos (`swe-skills/` or `my-ai-skills/skills/`), NOT `~/.hermes/skills/`
- Obsidian = personal KB + idea-park; `hive/docs/` = multi-machine engineering docs
- NEVER deploy unreviewed code; always load `swe-skills/multi-model-pr-review` before reviewing (no freelancing)
- Long tasks → cronjob (not delegate_task — delegate dies on parent interrupt)
- Always state log path + commit/push status after file changes
- Concise replies per Rule 0

## Progress
### Done
- **cot-proxy Round 2 + Config E benchmark:** 5-config bake-off (A–E), dual-judge; gemma4:31b plain (config A) is recommended default — 144s, 6/6 correct, 4.4–18× faster than alternatives. SKILL.md updated (b6df82f), PR #4 commented
- **OC wedge diagnosed and fixed:** root cause = GitHub Copilot opus-4.7 stream stalls on heavy-cache sessions; OC has no client-side timeout; bridge was sync + pinned stale session forever. OC restarted on M5 (PID 29796) + ann; stale session IDs cleared from bridge state files; both bridges restarted
- **Bridge async refactor:** `feature/bridge-async-concurrency` on `my-ai-skills` — asyncio main loop, per-chat `Lock`, per-backend `Semaphore` (OC=3, others=2), hard `wait_for` timeouts, structured error replies, stdlib-only (SHA 5cc8c06 after 3 fix cycles). Two-of-three reviewers approved (Codex + claude-aws). OpenCode reviewer was silently broken (stdin trap — see below)
- **OpenCode stdin trap fixed in docs:** `opencode run` ignores stdin entirely; message must be positional arg or `-f` for files. Warning added inline to `swe-skills/skills/code-eval/SKILL.md` (a10b53f). Redundant standalone skill removed (3cffdf6)
- **Multi-model-pr-review skill:** pre-existing from last night at `swe-skills/skills/multi-model-pr-review/SKILL.md` (acde727, 7f39111) — already had correct opencode pattern
- **Agent-dispatcher architecture doc:** `obsidian-vault/projects/agent-dispatcher/architecture.md` (430c4f8) — covers dispatcher design, continuous background compactor, throwaway-PoC auto-/rewind pruning, pluggable MemoryBackend (Hermes = one adapter), `~/.agent-dispatcher/` data layout, Telegram topic keying `(chat_id, thread_id or None)`
- **Spark cluster handoff:** `obsidian-vault/projects/spark/handoff.md` (69f3acd) — 4-node bring-up plan, 2-node phase 1 ready
- **Mission-alignment idea logged:** `obsidian-vault/inbox/2026-05-17-mission-alignment-all-agents.md` (200c0e4)
- **Hermes M5 group created:** chat_id=-1003873388511; `docs-organization` topic created (thread_id=46); doc-org proposal sent there
- **GH issue #5 opened:** `qike-ms/my-ai-skills#5` — intelligent context compaction (3-phase plan)
- **Memory updated:** cronjob > delegate_task rule; always load multi-model-pr-review; obsidian paths corrected (no `qi/` prefix); NEVER deploy unreviewed rule

### In Progress
- **Compaction research cron** (`0b0bd07b6e5b`): researching Hermes/OC/OpenClaw/CC compaction + mission-driven pruning spike; output → `obsidian-vault/projects/agent-dispatcher/` (still running)
- **Agent-dispatcher scaffold cron** (`b4f2eac73aa2`): creating `qike-ms/context-manager` + `qike-ms/agent-dispatcher` repos, scaffolding both, auto-review; fires ~5 min after bridge fix

### Blocked
- **Async bridge not deployed:** needs proper 3-of-3 review (OC review was bogus); user said "skip re-review, move on" — deploy decision deferred. Branch exists at `feature/bridge-async-concurrency` SHA 5cc8c06

## Key Decisions
- **Gemma4:31b plain = default local LLM** (not cot-proxy): same 6/6 correctness, 4.4–18× faster. Proxy scoped to qwen/deepseek-r1 family where native thinking burns budget
- **Agent-dispatcher architecture**: thin dispatcher owning context+memory only; backends are ephemeral (stateless per-turn); sessions deleted after invocation
- **Pluggable MemoryBackend**: Hermes is one adapter (not a requirement); `NoopMemoryBackend` default so dispatcher runs standalone
- **Telegram topics**: use native `(chat_id, message_thread_id or None)` — DM fallback is None, group-with-topics is thread_id; user created "Hermes M5" group (-1003873388511)
- **Data dir**: `~/.agent-dispatcher/` mirrors Hermes convention
- **Fleet orchestrator**: Hermes hub-spoke beats Kubernetes — K8s decommissioned for good reasons, Hermes has 80% already, workloads aren't pods
- **Doc org**: obsidian = personal KB + idea-park; `hive/docs/` = active multi-machine engineering; per-project docs inside each repo
- **Skills in repos**: wrong to create skills in `~/.hermes/skills/`; always commit to `swe-skills/` or `my-ai-skills/skills/`

## Next Steps
1. Await compaction research cron + agent-dispatcher scaffold cron deliveries
2. Sign off on doc-org proposal in Hermes M5 `#docs-organization` topic (thread_id=46) so hive/obsidian/repo split is locked in
3. Move `obsidian/projects/agent-dispatcher/` → `hive/docs/agent-dispatcher/` (pending doc-org approval)
4. Add `/cc /new` `/oc /compact` passthrough commands to bridge (small PR, `my-ai-skills#3`)
5. 2-node Spark bring-up: Phase 1 = physical + `ib_send_bw` RDMA check; need Spark OS version + cable type from user
6. Fleet orchestrator design doc → `hive/docs/fleet-orchestration/hermes-hub-vs-k8s.md` (m4 meeting prep)
7. Update running cron prompts to write to `hive/docs/` not obsidian once doc-org is approved

## Critical Context
- **OpenCode stdin trap**: `opencode run` ignores stdin; must pass message as positional arg or `-f file`; `echo | tee -a` to pre-existing file silently produces stale "reviews". All prior OC reviews this session were bogus. Warning added to `code-eval` SKILL.md
- **OC compaction gap**: OC's auto-compact is TUI-only, not server-side. Headless `opencode serve` sessions grow forever. `POST /session/<id>/summarize` with `{providerID, modelID}` is the manual API (confirmed working). `tokens.input` of last turn ≈ current context size (NOT `tokens.cache.read` which is cumulative billing metric)
- **Continuous compaction principle**: keep last N messages/tool-calls verbatim always; pre-compact older turns asynchronously; throwaway-PoC auto-/rewind = detect abandoned branches, replace with 1-sentence conclusion
- **Bridge single-threading root cause**: sync `for upd: handle_message()` blocks on subprocess/HTTP calls; fixed in 5cc8c06 but not yet deployed
- **lan-cora reachable only via Tailscale IP** `100.119.8.108` (not LAN `192.168.1.226`) for httpx async on macOS
- **`--no-verify` needed for obsidian commits**: pre-commit hook hangs

## Relevant Files
- `~/git/my-ai-skills/cot-proxy/SKILL.md`: cot-proxy validation + Round 2 + Config E benchmark results
- `~/git/my-ai-skills/scripts/telegram-agent-bridge.py`: current sync bridge (main); async fix on `feature/bridge-async-concurrency` (SHA 5cc8c06, not deployed)
- `~/git/obsidian-vault/projects/agent-dispatcher/architecture.md`: dispatcher + context-manager design (move to hive pending)
- `~/git/obsidian-vault/projects/spark/handoff.md`: 4-node Spark bring-up handoff
- `~/git/swe-skills/skills/multi-model-pr-review/SKILL.md`: correct 3-reviewer pattern (use this for all reviews)
- `~/git/swe-skills/skills/code-eval/SKILL.md`: added stdin-trap warning for opencode
- `~/git/hive/docs/fleet-orchestration/`: target for fleet orchestrator design doc
- `~/.hermes/logs/gateway.error.log`: Hermes gateway errors including copilot timeouts
- `~/Library/Logs/telegram-agent-bridge.log`: bridge activity log on M5
```

</details>

<details><summary><b>S3b · tail=6</b> — head=42 turns, 268876 chars sent, latency 67.8s</summary>

```markdown
## Goal
Build an agent-dispatcher system (replacing the current Telegram bridge) with intelligent context management, and debug/fix the existing sync bridge while designing fleet orchestration architecture.

## Constraints & Preferences
- Skills live in repos (`~/git/my-ai-skills/skills/`, `~/git/swe-skills/skills/`), never `~/.hermes/skills/`
- Obsidian (`~/git/obsidian-vault/`) = personal KB + idea-park for new projects; no `qi/` prefix — all content at root
- `~/git/hive/docs/` = fleet engineering docs (multi-machine operational)
- NEVER deploy unreviewed code; auto dual-judge (codex + claude-aws + opencode) before any deploy
- Long-running tasks → cronjob, NOT delegate_task (delegate dies on user message interrupt)
- Obsidian projects/ = idea-parking; graduated projects move to hive/docs/ or own repo
- Always state log path + commit/push state after file changes
- `opencode run` ignores stdin; use positional arg + `-f FILE` for diffs
- lan-cora reachable from M5 only via Tailscale IP `100.119.8.108` (LAN blocked for httpx async on macOS)

## Progress
### Done
- **Wedge root cause found + fixed**: Copilot opus-4.7 stream stalled on heavy-cache sessions → OC had no client stream timeout → bridge sync loop blocked. Restarted OC on M5 (PID 29796) and ann via systemd; cleared stale session IDs from both bridge state files; bridges restarted and healthy
- **Bridge async refactor**: Wrote scope-b async rewrite (feature/bridge-async-concurrency, commit 1642886) — per-chat asyncio.Lock, per-backend Semaphore (OC=3, CC/CODEX/HERMES=2), hard wait_for timeouts, structured error replies, stdlib-only
- **3-way review of async branch**: All 3 reviewers (Codex, claude-aws/Sonnet-4-6, OpenCode/Opus-4.7) independently flagged same 3 BLOCKING issues (subprocess leak on cancel, shared state race, chat-lock setdefault race)
- **Bridge async fix cron delivered**: Cron `0ab1ca8bd0cd` fixed all 3 blockers + followups across 3 commits; Codex + claude-aws both cleared (NO BLOCKING). Final SHA: 5cc8c06. OC re-review skipped per user request
- **Hermes M5 group**: Created `docs-organization` topic (thread_id=46); sent doc-org proposal there
- **Architecture doc**: `obsidian-vault/projects/agent-dispatcher/architecture.md` (commit 430c4f8) — covers continuous compaction, throwaway-PoC auto-/rewind, pluggable MemoryBackend, `~/.agent-dispatcher/` data layout, Telegram-native topic keying, stateless backends
- **code-eval skill patched**: Added stdin trap warning inline at OpenCode section in `swe-skills/skills/code-eval/SKILL.md` (a10b53f)
- **Removed duplicate skill**: `my-ai-skills/skills/opencode-non-interactive-review/` deleted (3cffdf6); content folded into code-eval
- **cot-proxy SKILL.md updated**: Full Round 2 + Config E benchmark; gemma4:31b plain recommended as default (b6df82f)
- **Spark handoff**: `obsidian-vault/projects/spark/handoff.md` (69f3acd)
- **Mission-alignment idea**: `obsidian-vault/inbox/2026-05-17-mission-alignment-all-agents.md` (200c0e4)
- **GH issue #5**: Created `qike-ms/my-ai-skills#5` for intelligent context compaction

### In Progress
- **Compaction research cron** `0b0bd07b6e5b`: Running autonomously — researching Hermes/OC/OpenClaw/CC compaction internals + mission-driven pruning spike; delivers 2 docs to obsidian when done
- **Agent-dispatcher scaffold cron** `b4f2eac73aa2`: Scheduled (fires 5 min after session start) — creates `qike-ms/context-manager` + `qike-ms/agent-dispatcher` repos, scaffolds both, auto-reviews, does NOT migrate live bridge

### Blocked
- **async bridge deploy**: Waiting on cron scaffold completion + user testing. 2 of 3 reviews passed (5cc8c06); OC review skipped per user
- **Doc-org restructure**: Awaiting sign-off in Hermes M5 #docs-organization (move agent-dispatcher arch from obsidian → hive/docs)

## Key Decisions
- **Dispatcher architecture**: Bridge evolves to `agent-dispatcher` (new repo `qike-ms/agent-dispatcher`); thin — only context + memory; backends (OC/CC/Codex/Hermes) called stateless (POST → reply → DELETE session); per-machine deployment via fleet-manage Ansible
- **Context-manager**: Separate library repo `qike-ms/context-manager`; pluggable `MemoryBackend` interface — Hermes is ONE adapter (NoopMemoryBackend default), not hard requirement
- **Compactor**: Continuous background worker (not threshold-triggered); keep last N messages verbatim always (no "Dory surprises"); throwaway-PoC pruning = auto-/rewind via LLM with special prompting; mission-aware rescoring on mission change
- **Telegram topics**: Use native `(chat_id, message_thread_id or None)` as session key — DM uses None fallback; Hermes M5 group created
- **Data layout**: `~/.agent-dispatcher/` mirrors `~/.hermes/` convention (config.yaml, context.db, memory/, cache/, logs/, state.json)
- **Fleet orchestrator**: Hermes hub-spoke beats Kubernetes (K8s already decommissioned, workloads aren't pods, wrong abstraction for 8 trusted machines on a tailnet)
- **OC TUI auto-compact**: Is a TUI-client feature, not server feature — headless OC server has no autonomous compaction; bridge must manage this explicitly. `POST /session/<id>/summarize` with `{providerID, modelID}` body works (~7s, returns `true`)
- **opencode run stdin trap**: `opencode run` ignores stdin; use `-f FILE` for diffs in automated pipelines; `tee -a` to pre-existing files masks failures

## Next Steps
1. Wait for compaction research cron (`0b0bd07b6e5b`) to deliver — feeds agent-dispatcher scaffold design
2. Sign off on doc-org proposal in Hermes M5 #docs-organization (move arch doc from obsidian → hive/docs/agent-dispatcher/)
3. Delete empty `~/git/fleet-orchestrator/` directory once doc-org is settled
4. Test async bridge (5cc8c06) on M5 — user needs to send messages via Hermes M5 group once deployed
5. Deploy async bridge to M5 (checkout feature/bridge-async-concurrency, launchctl kickstart bridge) — needs explicit deploy command
6. Write fleet-orchestrator design doc to `hive/docs/fleet-orchestration/hermes-hub-vs-k8s.md` (content ready in session)
7. Write `/cc /new` and `/oc /compact` passthrough command support (issue #5 phase 2 or small PR)
8. Phase 2 benchmark: 30+ tasks × 4 configs × 4 models (cot-proxy work)

## Critical Context
- **M5 group**: "Hermes M5" group chat_id=`-1003873388511`; topic `docs-organization` = thread_id=46; 3 other topics exist but thread_ids unknown until messages sent
- **OC session management**: `POST /session/<id>/abort` → `true` (cancels in-flight turn); `DELETE /session/<id>` → removes session (both confirmed on this OC v1.15.3)
- **OC `/summarize` endpoint**: `POST /session/<id>/summarize` with `{"providerID":"github-copilot","modelID":"claude-sonnet-4-5"}` — replaces history with LLM summary (~7s). `tokens.cache.read` ≠ context size; use `tokens.input` of last turn to estimate current context vs model window
- **Bridge state files**: M5=`~/.cache/telegram-agent-bridge.state`; ann=`~/.cache/telegram-agent-bridge.state` on `nvidia@ann`; both have OC session cleared
- **ann OC**: restarted via `systemctl --user restart opencode-serve`; bridge active per systemctl
- **opencode run**: ALWAYS use positional arg + `-f FILE` for diffs. `echo | opencode run` silently creates ~5-token empty session, exits 0 — fake success
- **code-eval (swe-skills)**: the right skill for multi-model code review; already documents Codex + claude-aws + opencode invocation patterns; stdin trap now documented there
- **Hermes compaction**: `agent/context_compressor.py` — rolling, auto-triggers at token pressure threshold; `/compact [focus]` manual. OC: TUI-only auto-compact; headless has no equivalent. CC: auto-compact at ~95% window + `/compact` manual. Codex: no auto-compact
- **pre-push hook** on obsidian-vault sometimes hangs; use `--no-verify` to bypass

## Relevant Files
- `/Users/emma/git/my-ai-skills/scripts/telegram-agent-bridge.py`: async refactor (5cc8c06 on feature/bridge-async-concurrency), NOT yet deployed
- `/Users/emma/git/obsidian-vault/projects/agent-dispatcher/architecture.md`: dispatcher design doc (commit 430c4f8); may need to move to hive/docs pending sign-off
- `/Users/emma/git/obsidian-vault/projects/spark/handoff.md`: 2-node DGX Spark bring-up plan
- `/Users/emma/git/swe-skills/skills/code-eval/SKILL.md`: multi-model review patterns + opencode stdin trap warning (a10b53f)
- `/Users/emma/git/my-ai-skills/cot-proxy/SKILL.md`: Round 2 benchmark results, gemma plain recommended (b6df82f)
- `~/.hermes/logs/gateway.error.log` + `errors.log`: Hermes runtime logs, ReadTimeout evidence from Copilot blip
- `~/Library/Logs/telegram-agent-bridge.log` + `.err`: M5 bridge runtime logs
- `/Users/emma/.hermes/hermes-agent/agent/context_compressor.py`: Hermes compaction impl (reference for research cron)
- `/Users/emma/.hermes/hermes-agent/gateway/run.py`: Hermes gateway (17k LOC) — per-session-key async model, busy-session policy, `_handle_active_session_busy_message`
```

</details>

<details><summary><b>S3b · tail=8</b> — head=40 turns, 256213 chars sent, latency 53.9s</summary>

```markdown
## Goal
Build and evolve a Telegram bridge into a proper per-machine agent-dispatcher with intelligent context management, while fixing the current async bridge and planning fleet orchestration.

## Constraints & Preferences
- Repos at `~/git/<repo>` (never `~/code/`)
- Skills live in repos (`my-ai-skills/skills/` or `swe-skills/skills/`), not `~/.hermes/skills/`
- NEVER deploy unreviewed code — auto dual-judge (codex + claude-aws + opencode) BEFORE deploy
- Long tasks that survive user messages → cron job, NOT delegate_task
- Obsidian paths: `~/git/obsidian-vault/` root (no `qi/` subdir); `projects/<name>/` for idea-parking
- Engineering/operational docs → `~/git/hive/docs/` (NOT obsidian)
- Always state log path + commit/push status after file changes
- `--no-verify` needed for obsidian vault pre-push hook (hangs)
- Concise replies, no filler

## Progress
### Done
- OC restart on M5 (PID 29796) + ann: wedge cleared, stuck sessions deleted, bridges restarted
- Bridge async refactor built (scope b): per-chat locks, per-backend semaphores, hard timeouts — 3 blocking issues flagged by all reviewers (subprocess leak on cancel, shared state race, chat-lock race)
- Fix cron (`0ab1ca8bd0cd`) ran, applied fixes, re-reviewed — Codex + claude-aws cleared (SHA 5cc8c06)
- OpenCode reviewer was BROKEN throughout (invoked with stdin which it ignores; all OC "approvals" were bogus)
- `opencode-non-interactive-review` skill created in `my-ai-skills/skills/`, local `~/.hermes/skills/` copy deleted (e12c377)
- Agent-dispatcher architecture doc committed to `obsidian-vault/projects/agent-dispatcher/architecture.md` (430c4f8) — covers: continuous compaction, no-Dory principle, throwaway-PoC auto-/rewind, pluggable MemoryBackend, `~/.agent-dispatcher/` data layout
- Mission-alignment idea logged: `obsidian-vault/inbox/2026-05-17-mission-alignment-all-agents.md` (200c0e4)
- Spark handoff doc: `obsidian-vault/projects/spark/handoff.md` (69f3acd)
- GH issue `qike-ms/my-ai-skills#5` filed for intelligent context compaction (3-phase)
- cot-proxy SKILL.md updated with full 5-config Round 2 results + gemma-plain recommendation (b6df82f)
- Hermes M5 Telegram group created (chat_id `-1003873388511`); `docs-organization` topic created (thread_id=46); doc-org proposal sent there
- `opencode-non-interactive-review` skill pushed to my-ai-skills main (e12c377)

### In Progress
- **Compaction research cron** (`0b0bd07b6e5b`): researching Hermes/OC/OpenClaw/CC compaction + mission-driven pruning; output → `hive/docs/agent-dispatcher/` (not obsidian — cron prompt may have wrong path)
- **Agent-dispatcher scaffold cron** (`b4f2eac73aa2`): fires 5min after bridge fix; creates `qike-ms/context-manager` + `qike-ms/agent-dispatcher` repos, scaffolds both, auto-reviews
- **OpenCode proper re-review** of bridge SHA 5cc8c06 running via `proc_1ffebfcf5dc1` with `-f /tmp/bridge-diff.patch`
- **Bridge async branch** (`feature/bridge-async-concurrency`) at SHA 5cc8c06: Codex + claude-aws cleared, waiting on valid OC review before deploy

### Blocked
- Deploy of async bridge: waiting on valid OC review result (`proc_1ffebfcf5dc1`)
- Agent-dispatcher scaffold cron path: compaction research cron may write to obsidian instead of hive/docs — needs verification when it delivers

## Key Decisions
- **Async bridge: DO NOT DEPLOY until OC review is valid** — prior OC "approvals" were fake (stdin ignored)
- **Agent-dispatcher architecture**: thin dispatcher (context+memory only), backends ephemeral (stateless per-call), bridges backends share no persistent state
- **Pluggable MemoryBackend**: Hermes is one adapter (not a hard dependency); `NoopMemoryBackend` is default
- **Telegram topics**: use native `(chat_id, message_thread_id or None)` keying — DM has no thread_id, falls back to single default session
- **Data dir**: `~/.agent-dispatcher/` mirrors `~/.hermes/` convention
- **Fleet orchestrator**: Hermes hub-spoke wins over K8s — workloads aren't pods, hardware is trusted/fixed, Hermes has 80% already, K8s was previously decommissioned
- **Doc org**: obsidian = personal KB + idea-parking; hive/docs = fleet engineering ops; swe-skills = generic practices; my-ai-skills = AI-tool-specific skills
- **Compactor design**: continuous background (not threshold), last N verbatim always, throwaway-PoC auto-/rewind via special LLM prompting, mission-driven pruning
- **OpenCode review invocation**: must use `-f <file>` not stdin pipe (`opencode run` silently ignores stdin)
- **Skills in repos**: `my-ai-skills/skills/` for AI-tool-specific; `swe-skills/skills/` for generic engineering practices

## Next Steps
1. Wait for `proc_1ffebfcf5dc1` OC review result; if BLOCKING issues found, fix and re-review; if clear, deploy async bridge to M5 + ann
2. Verify compaction research cron output path (should be `hive/docs/agent-dispatcher/`, may have written to obsidian)
3. Move `obsidian/projects/agent-dispatcher/architecture.md` → `hive/docs/agent-dispatcher/` (doc org fix)
4. Get topic thread_ids for Hermes M5 group (user needs to send one message per topic)
5. Write fleet orchestrator design doc to `hive/docs/fleet-orchestration/hermes-hub-vs-k8s.md`
6. Log cross-fleet dispatcher ambition to `hive/docs/fleet-orchestration/` (connects to m4 meeting)
7. Optionally add `dual-judge-code-review` generic practice skill to `swe-skills/skills/`

## Critical Context
- **`opencode run` IGNORES stdin** — must use positional arg or `-f <file>`. All OC reviews in this session before `proc_1ffebfcf5dc1` were invalid.
- **Bridge async fix 3 blocking issues** (all now fixed at 5cc8c06): subprocess orphan on `wait_for` cancel (no try/finally), shared `state` dict race across tasks, `_chat_locks.setdefault` race
- **Hermes M5 group**: chat_id=`-1003873388511`; `docs-organization` topic thread_id=46; 3 other topics exist but thread_ids unknown (need a message from user in each)
- **OC auto-compact is TUI-only**: headless `opencode serve` has no auto-compact; sessions grow forever unless bridge calls `POST /session/<id>/summarize` with `{providerID, modelID}` body
- **`tokens.cache.read` ≠ context size**: cumulative billing metric across all turns; real context size = `tokens.input` of last turn; need model-window registry to compute % used
- **Cron jobs active**: `0b0bd07b6e5b` (compaction research), `b4f2eac73aa2` (dispatcher scaffold) — both running autonomously, deliver to this chat
- **Ann OC**: restarted via `systemctl --user restart opencode-serve`, state file cleared at `/home/qike/.cache/telegram-agent-bridge.state`
- **lan-cora reachable only via Tailscale** `100.119.8.108` (LAN `192.168.1.226` blocked for httpx async on macOS)
- **Obsidian pre-push hook**: hangs — use `--no-verify` for obsidian commits

## Relevant Files
- `~/git/my-ai-skills/scripts/telegram-agent-bridge.py`: current sync bridge (running on M5 + ann); async refactor at `feature/bridge-async-concurrency` SHA 5cc8c06
- `~/git/my-ai-skills/skills/opencode-non-interactive-review/SKILL.md`: new skill documenting OC stdin gotcha
- `~/git/obsidian-vault/projects/agent-dispatcher/architecture.md`: dispatcher architecture (needs move to hive/docs)
- `~/git/hive/docs/fleet-orchestration/`: existing fleet orchestration docs dir (right home for hub-vs-k8s doc)
- `~/git/my-ai-skills/cot-proxy/SKILL.md`: Round 2 bake-off results, gemma-plain recommendation
- `/tmp/review-opencode-v2.json`: proper OC review in progress (proc_1ffebfcf5dc1)
- `/tmp/bridge-diff.patch`: diff of async refactor fed to OC reviewer
- `~/.hermes/logs/gateway.error.log`: Hermes gateway errors; shows Copilot provider timeouts
- `~/Library/Logs/telegram-agent-bridge.log`: M5 bridge logs
```

</details>


---

## 4. Scores

Scored by `gpt-5` via `codex exec` (different vendor than the summariser → reduces self-bias). The `scorer note` column below paraphrases / truncates the scorer's `notes` field for table fit; verbatim JSON is in `/tmp/spike3/scores_codex.json`. Latency in the means table below covers the summariser pass only; scorer latency is not measured.

| session | tail | R1 | R2 | R3 | R4 | R5 | total | scorer note (truncated) |
|---|---|---|---|---|---|---|---|---|
| S1 | 2 | 2 | 2 | 2 | 1 | 2 | **9** | `~/` shorthand instead of absolute paths |
| S1 | 4 | 2 | 2 | 2 | 2 | 2 | **10** | accurate, complete |
| S1 | 6 | 2 | 2 | 2 | 1 | 1 | **8** | one Discord bot-visibility claim unsupported |
| S1 | 8 | 2 | 2 | 2 | 2 | 2 | **10** | matches paused state, concrete paths |
| S2 | 2 | 2 | 2 | 2 | 1 | 2 | **9** | `~/` shorthand again |
| S2 | 4 | 2 | 1 | 2 | 2 | 1 | **8** | misses latest profile-help check; inferred PR-velocity claim |
| S2 | 6 | 2 | 2 | 2 | 2 | 2 | **10** | captures decisions + blocked cora eval |
| S2 | 8 | 2 | 2 | 2 | 2 | 2 | **10** | SSH-blocked state captured accurately |
| S3b | 2 | 2 | 2 | 2 | 2 | 1 | **9** | mild hallucination risk from omitted middle |
| S3b | 4 | 2 | 2 | 1 | 2 | 0 | **7** | invented status claims |
| S3b | 6 | 2 | 2 | 2 | 2 | 0 | **8** | mission-alignment item not in transcript |
| S3b | 8 | 2 | 2 | 2 | 2 | 2 | **10** | accurate, current, preserves corrections |

### Mean total by tail size

| tail | mean / 10 | values | summariser latency mean (s) |
|---|---|---|---|
| 2 | **9.00** | 9, 9, 9 | 43.5 |
| 4 | 8.33 | 10, 8, 7 | 44.8 |
| 6 | 8.67 | 8, 10, 8 | 44.7 |
| **8** | **10.00** | 10, 10, 10 | **36.5** |

`tail=8` wins on quality across all three sessions **and** is the fastest summariser pass (head is 2 turns shorter than `tail=2`'s head, so the LLM has less to summarise). `tail=4` and `tail=6` are mid-table because they straddle the boundary where small corrections from the very-late head get *dropped from the verbatim tail* but *also collapsed into bullet points in the summary*, losing the "no, do X instead" framing.

---

## 5. Observations

1. **`tail=8` wins on this corpus** (3/3 perfect scores; no other tail achieves perfect on more than one session). The "OC default is fine" prior is **invalidated** for chatty TG bridge sessions. The hypothesis in `compaction-research.md` §6.5 holds.
2. **The R5 (no-hallucination) axis is what separates 4/6 from 8.** When the boundary cuts mid-decision, the summariser fills the gap by interpolating ("mission-alignment note", "PR velocity") — fixed by widening the tail so the boundary lands in a quieter spot.
3. **R4 (full paths)** is the weakest axis across the board (lots of `~/…` instead of `/Users/emma/…`). This is a **template wording** problem, not a tail-size problem: add "use absolute paths, not `~/` shorthand" to the SUMMARY_TEMPLATE Rules block (`compaction.ts:75`).
4. **Latency does NOT increase with bigger tails — it decreases.** Counter-intuitive but mechanical: a larger tail means a smaller head, so less text to summarise. The 12-run mean latency at `tail=8` was 36.5 s vs 43.5 s at `tail=2`.
5. **The effective tail cap is `preserve_recent_tokens`, not turn count.** OC's `select()` (`compaction.ts:247`–`296`) clamps the verbatim tail to `preserveRecentBudget` — defaults to `clamp(usable(model) * 0.25, MIN=2_000, MAX=8_000)` tokens (`compaction.ts:138`–`143`). `tail_turns: 8` is a *ceiling on turn count*, not a guarantee of 8 verbatim turns — short chatty turns will all fit; long tool-heavy turns get split via `splitTurn()` (`compaction.ts:163`–`186`) or clipped. (`PRUNE_PROTECT = 40_000` is unrelated — it gates the separate `prune()` pass that erases old tool outputs, not the compaction tail.) This token-budget clamp is the safety net that makes "go bigger" cheap.

---

## 6. Recommendation

**Set `compaction.tail_turns = 8` for the tg-bridge default on chatty TG sessions** (the only regime this spike tested).

Two **untested hypotheses** to validate before deploying:

- For tool-heavy sessions (heuristic: `tool_result_msgs / total >= 0.7` over the last 10 turns), 8 may waste budget on long tool outputs the summariser already truncates via `TOOL_OUTPUT_MAX_CHARS`. Plausibly drop to **4**. **Not tested in this spike** — the corpus is all chatty/admin. Needs its own bake-off on a tool-dense corpus before shipping.
- For Codex backend (no in-session compaction; rotate-only), the "tail" is really just the reseed prompt + one prior exchange. Plausibly **2** is enough. Also untested here.

### Config snippet — OpenCode (global / per-project config file)

```json
{
  "compaction": {
    "tail_turns": 8
  }
}
```

**Do not also set `preserve_recent_tokens` unless your OC model window is ≥ 32k.** When `cfg.compaction.preserve_recent_tokens` is absent, `preserveRecentBudget()` (`compaction.ts:138`–`143`) self-tunes to `clamp(usable(model) * 0.25, 2_000, 8_000)`. Setting it explicitly to 8000 pegs the budget at the hard max regardless of model — fine on opus/sonnet (200k+) but on a small-window model it can make the verbatim tail eat the whole prompt.

Set via OC's standard config resolution (`Config.Service`, read by `compaction.ts:252` `cfg.compaction?.tail_turns` and `compaction.ts:140` `cfg.compaction?.preserve_recent_tokens`). The current OC `POST /session/<id>/summarize` HTTP handler (`server/routes/instance/httpapi/handlers/session.ts:257`–`275`) only accepts `{providerID, modelID, auto?}` — **there is no per-call compaction-config patch** in today's API. The tg-bridge therefore must either:

1. Set `tail_turns: 8` at the OC config level for sessions it owns (preferred — simple, durable), or
2. File an upstream feature request to add `{compaction?: {tail_turns?: number, preserve_recent_tokens?: number}}` to the `SummarizePayload` schema in `server/routes/instance/httpapi/groups/session.ts`. Until then, runtime per-call overrides are not available.

The `select()` budget will still clamp at `MAX_PRESERVE_RECENT_TOKENS = 8_000` regardless, so worst-case cost is bounded.

---

## 7. Limitations & threats to validity

- **n=3 sessions** is small. Confidence interval on the `tail=8` lead is wide. The signal is robust enough (3/3 perfect scores, 0/3 perfect for any other tail) that I'm willing to commit a default, but anyone optimising past this should re-run with ≥ 10 sessions across both chatty and tool-heavy bins.
- **Cross-vendor scoring is honest but not free of bias** — gpt-5 may systematically over-credit certain rhetorical patterns Sonnet uses. A third opinion from a local model would harden the rubric. Spot-checked 4/12 cells manually; all scorer judgements were consistent with my read.
- **Sonnet ≠ session model.** Each tg-bridge session can be Opus / Sonnet / GPT-5 / Gemini; summariser quality may vary. OC's compaction agent default is Sonnet-family so this matches the production case for the OC backend specifically, but Hermes/Codex sessions will differ.
- **S2 only has 18 turns total**, so its `tail=8` head was only 10 turns — that bin is light on signal. The S1 / S3b results carry the weight. The `tail=4` global mean (8.33) is also dragged disproportionately by S2's 8/10 outlier; tail=4 may be better than the mean suggests in non-degenerate sessions.
- **Scorer saw the head but not the tail.** This matches the question (does the summary cover the head?) but means R3 ("in-flight goal") is evaluated relative to where the head ends, not where the conversation actually paused. Small asymmetry; acceptable.
- **`~/` vs absolute path penalty** is a template fix, not a tail-size fix. R4 noise across the board is mostly that.

---

## 8. Next spike

**Spike 4a — Re-run with verbatim OC template.** The harness used an abbreviated SUMMARY_TEMPLATE missing OC's `Rules:` block (which already tells the model *"Preserve exact file paths, commands, error strings, and identifiers"*). Re-run the 12-cell experiment with `run.py`'s `SUMMARY_TEMPLATE` replaced by the verbatim string at `compaction.ts:44`–`79`. **Strong prior:** R4 jumps to 2/2 across the board, possibly closing the tail=4/6 gap with tail=8. The tail=8 conclusion may survive or may shrink — needs measuring.

**Spike 4b — Template tightening (only if 4a still shows R4/R5 gaps).** Patch OC's template Rules block to add:

1. *"Use absolute paths (`/Users/...`); never `~/`."*
2. *"Cite the tool name when a Done/InProgress bullet derives from a tool call."*
3. *"Mark unverified claims with `(inferred)` instead of asserting."*

**Spike 4c — Tool-heavy corpus bake-off.** This spike covered only chatty TG sessions. Pick 3 long tool-heavy coding sessions (Codex / heavy file-editing) and re-run at tail ∈ {2, 4, 8}. Validate (or invalidate) the §6 hypothesis that 4 is better than 8 on that regime.

---

## 9. Sources

- OpenCode compaction core: `~/git/opencode/packages/opencode/src/session/compaction.ts` — constants at lines 37–43 (`DEFAULT_TAIL_TURNS=2`, `MIN/MAX_PRESERVE_RECENT_TOKENS=2_000/8_000`, `PRUNE_PROTECT=40_000`, `TOOL_OUTPUT_MAX_CHARS=2_000`), `SUMMARY_TEMPLATE` at lines 44–79, `turns()` at 145–160, `preserveRecentBudget()` at 138–143, `select()` at 247–296. Branch `dev` @ commit `09549661e111f768331e01cc278ffa2f2e32d9e5` (snapshot 2026-05-17).
- OpenCode HTTP summarize handler: `~/git/opencode/packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts:257`–`275` (`SessionHttpApi.summarize` → `compactSvc.create({sessionID, agent, model, auto})`; **no** compaction-config patch field). Payload schema: `server/routes/instance/httpapi/groups/session.ts:90`.
- Sibling spike docs: `compaction-research.md` §6.5; `spike-1-2-midstream-safety.md`.
- Experiment harness, raw outputs, scorer transcripts: `/tmp/spike3/{run.py,score_codex.py,*_tail*.md,*_tail*.meta.json,scores_codex.json,scores_summary.md}`.
- Sessions: `~/.hermes/sessions/session_20260513_203038_779a381a.json`, `~/.hermes/sessions/session_20260514_182122_f9d4b1.json`, `~/.hermes/sessions/session_20260517_124413_3754c9.json`.
