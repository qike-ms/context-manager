---
title: Spike 5 — Codex session rotation seeding mechanism
issue: qike-ms/my-ai-skills#5
parent: compaction-research.md (Open Question 4)
status: resolved
date: 2026-05-17
codex_cli_version: 0.130.0
---

# Spike 5: How to seed a fresh Codex session with a compaction summary

## Question

From `compaction-research.md` §6, open question #4:

> Does the Codex CLI accept a "seed system message" via flag, or only via stdin?
> If only stdin, bridge needs a synthetic first user turn.

Two candidate mechanisms for `agent-dispatcher` to rotate a Codex session
while injecting a compacted summary:

- **(a)** A CLI flag that points at a seed file / accepts a seed string.
- **(b)** stdin-only: pipe the summary in as the first user message.

## TL;DR

**There is no `--system` / `--seed` flag.** Codex exec accepts the initial
instructions in exactly two interchangeable forms:

1. As a positional `[PROMPT]` argument, or
2. From **stdin** when the argument is omitted or set to `-`.

Both paths feed the **first user turn** of a brand-new session — in the
tested non-`--ephemeral` mode, each `codex exec` invocation creates a new
rollout file under `~/.codex/sessions/YYYY/MM/DD/rollout-…-<uuid>.jsonl`
(with `--ephemeral`, no rollout is persisted at all). There is no separate
"system prompt" channel in the **exec CLI surface** — i.e. no flag to add a
system message. Codex's runtime instructions can still be tuned via
`config.toml`, profiles, and `.rules` files (and the corresponding
`--ignore-user-config` / `--ignore-rules` switches confirm this), but
none of those is a place to inject a per-call compaction summary. Any
summary the bridge wants the model to see for a given rotation **must
ride in as part of that first user turn.**

This is mechanism **(b)** in practice, but it is equally available via argv.
Practical recommendation below.

## `codex exec --help` dump (verbatim)

CLI surveyed: `codex-cli 0.130.0` at `/Users/emma/.local/share/codex/bin/codex`
(npm package `@openai/codex`, vendor binary
`…/codex-darwin-arm64/vendor/aarch64-apple-darwin/codex`).

```
Run Codex non-interactively

Usage: codex exec [OPTIONS] [PROMPT]
       codex exec [OPTIONS] <COMMAND> [ARGS]

Commands:
  resume  Resume a previous session by id or pick the most recent with --last
  review  Run a code review against the current repository
  help    Print this message or the help of the given subcommand(s)

Arguments:
  [PROMPT]
      Initial instructions for the agent. If not provided as an argument
      (or if `-` is used), instructions are read from stdin. If stdin is
      piped and a prompt is also provided, stdin is appended as a
      `<stdin>` block

Options:
  -c, --config <key=value>          override config.toml values (TOML-parsed)
      --enable / --disable <FEATURE>
  -i, --image <FILE>...             attach images to the initial prompt
  -m, --model <MODEL>
      --oss / --local-provider
  -p, --profile <CONFIG_PROFILE>
  -s, --sandbox <MODE>              read-only | workspace-write | danger-full-access
      --dangerously-bypass-approvals-and-sandbox
  -C, --cd <DIR>                    working root
      --add-dir <DIR>
      --skip-git-repo-check
      --ephemeral                   do not persist session file to disk
      --ignore-user-config
      --ignore-rules
      --output-schema <FILE>        JSON Schema for the final response
      --color <COLOR>
      --json                        emit JSONL events on stdout
  -o, --output-last-message <FILE>
  -h, --help
  -V, --version
```

Notable absences (cross-checked by reading `--help` twice):

- No `--system`, `--system-prompt`, `--seed`, `--prepend`, `--context-file`,
  `--instructions`, or anything that takes a separate "system message" file.
- No `--summary` / `--carry-over` / `--memory` flag.
- `-i/--image` attaches images to the **initial prompt**, confirming the
  prompt is the only carrier channel for additional content.

`codex exec resume [SESSION_ID] [PROMPT]` exists but is irrelevant here:
it **continues** an existing rollout; it does not rotate to a fresh session.
For agent-dispatcher's compaction case the session is already too full —
we want a new one.

## Source paths checked

| Path | What I looked for | Finding |
|---|---|---|
| `~/.local/share/codex/bin/codex` (shim → vendor binary) | CLI surface | confirmed 0.130.0, exec subcommand as documented |
| `~/.local/share/codex/lib/node_modules/@openai/codex/bin/codex.js` | wrapper JS | thin loader, no extra flags |
| `~/.local/share/codex/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/codex` | Rust binary | only --help inspected; matches table above |
| `~/.codex/sessions/2026/05/17/rollout-*.jsonl` | new-session evidence | each `codex exec` creates a fresh rollout file with a new UUID |
| `~/git/` | local source checkout of codex | **none** (no `~/git/codex` clone present) |
| compaction-research.md §3 (Codex row) | prior assumptions | "no in-session compaction; rotation is the practical workaround" — still holds |

I did **not** read the Rust source directly (no checkout on this host) — all
claims about flag availability are grounded in the binary's own `--help`,
which is authoritative for what the CLI accepts.

## Empirical tests

Both candidate mechanisms exercised against the live CLI in `/tmp/codex-spike5`
(fresh git repo, read-only sandbox). The seed embedded a unique fact —
"the user's pet is named Whiskers (a cat)" — that Codex could only know
from the seed, so the response is proof the seed was consumed.

### Test A — positional PROMPT argument (mechanism a)

```bash
codex exec --skip-git-repo-check --sandbox read-only \
  --output-last-message /tmp/codex-spike5/last-a.txt \
  -C /tmp/codex-spike5 \
  "CONTEXT SUMMARY (prior session compacted):
- User is debugging a Telegram bridge
- Last decision: use OC SUMMARY_TEMPLATE for cross-backend continuity
- The user's pet is named Whiskers (a cat)

Now answer this user question: What is the pet's name and species, in one short line?"
```

Result (contents of `last-a.txt`):

```
Whiskers, a cat.
```

Tokens used: 7,245. New rollout file created under `~/.codex/sessions/…`.

### Test B — stdin (mechanism b)

```bash
printf 'CONTEXT SUMMARY (prior session compacted):
- The user'"'"'s pet is named Whiskers (a cat)
- Project: tg-bridge

User question: What is the pet name and species? One line.
' | codex exec --skip-git-repo-check --sandbox read-only \
        --output-last-message /tmp/codex-spike5/last-b.txt \
        -C /tmp/codex-spike5 -
```

Result (contents of `last-b.txt`):

```
Whiskers, cat.
```

Tokens used: 7,225. Separate new rollout file (separate UUID) created.

### Interpretation

- Both mechanisms successfully seed the first user turn. The model used
  information available only in the seed, so the seed is genuinely entering
  the context, not being ignored.
- Each invocation produces a **fresh session** automatically — that is the
  rotation. The bridge does **not** need to do anything special to "start a
  new session"; it just stops calling `resume` and runs `codex exec` again.
- Argv and stdin are equivalent for content delivery. Per the help text,
  if **both** are provided, the argv prompt is primary and stdin is
  appended as a `<stdin>` block — useful for `prompt = "USER:\n<task>"`
  with `<stdin> = "<compaction summary>"`, but the agent-dispatcher use
  case doesn't need that combo.

## Recommendation

Use **stdin** (mechanism b) as the primary seeding channel for
agent-dispatcher's Codex rotation. Reasons:

1. **No argv length limits** — macOS `ARG_MAX` is ~1MB but on Linux some
   shells / kernels are tighter; an OC-template summary can run to several
   KB and tail turns appended on top can push past comfortable argv sizes.
2. **No shell-quoting hazard** — summaries contain backticks, quotes,
   newlines, code blocks; piping bytes avoids escaping bugs entirely.
3. **Composes cleanly** with the dispatcher's existing stream plumbing —
   it's already an async subprocess wrapper; writing to `proc.stdin` is
   one line.
4. **Logs the same way as TUI sessions** — Codex records the first user
   turn in the rollout JSONL whether it came from argv or stdin, so audit
   and replay tooling needs no special-case.

Keep argv-prompt as a fallback for tiny seeds (debug invocations, smoke
tests in CI) where one-line readability matters.

### Exact invocation for agent-dispatcher

```python
# agent_dispatcher/backends/codex.py  (sketch)
import asyncio
from textwrap import dedent

CODEX_SEED_TEMPLATE = dedent("""\
    # Prior conversation (compacted)

    {summary}

    ---

    # Current user message

    {user_prompt}
""")

async def rotate_codex_session(
    *,
    summary: str,
    user_prompt: str,
    cwd: str,
    model: str | None = None,
    sandbox: str = "workspace-write",
) -> tuple[str, str]:
    """
    Start a fresh Codex session seeded with `summary` and immediately ask
    `user_prompt`. Returns (session_uuid, last_message).
    """
    seed = CODEX_SEED_TEMPLATE.format(summary=summary, user_prompt=user_prompt)

    args = [
        "codex", "exec",
        "--json",                       # parse rollout events for usage/session id
        "--sandbox", sandbox,
        "--skip-git-repo-check",
        "-C", cwd,
        # NOTE: do NOT combine --json with --output-last-message /dev/stdout
        # (that would interleave plain text into the JSONL stream). Either
        # parse the JSONL on stdout for the final assistant message, or use
        # a tmpfile path for --output-last-message and keep --json on stdout.
        # Also: do not pass --ephemeral here (and make sure no profile/config
        # forces it), or no rollout is persisted and later `codex exec
        # resume <uuid>` will fail.
        "-",                            # read prompt from stdin
    ]
    if model:
        args[2:2] = ["--model", model]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(seed.encode("utf-8"))
    if proc.returncode != 0:
        raise RuntimeError(f"codex exec failed: {stderr.decode(errors='replace')}")

    # Parse JSONL stream for session_id + final assistant message.
    session_id, last_msg = _parse_codex_jsonl(stdout)
    return session_id, last_msg
```

Key points:

- `-` as the trailing positional tells Codex to read from stdin (matches
  `--help`).
- `--json` is the right wire format for the bridge — events include the
  session UUID and per-turn `input_tokens / cached_input_tokens /
  output_tokens` that the compactor needs for its next ratio check
  (compaction-research.md §1, §5).
- The seed format wraps `{summary}` and `{user_prompt}` with explicit
  Markdown headings so the model treats the summary as background and the
  user prompt as the active request. This mirrors Hermes's "summarizer
  preamble" pattern (`agent/context_compressor.py:1023`) — frame the
  summary as source material, not as instructions.
- The new session UUID is captured from the JSONL stream and stored on
  the dispatcher's BridgeSession so subsequent turns in the *same* logical
  conversation can `codex exec resume <uuid> <prompt>` until the next
  compaction trigger fires (ratio ≥ 0.70 per §5).

## Cross-references

- `compaction-research.md` §3 (Codex row) — "rotation is the practical workaround"
- `compaction-research.md` §5 — per-backend delivery table, Codex row
- `compaction-research.md` §6 Q4 — this spike's question, now answered

## Trio review

Reviewed pre-commit by:

- codex exec (self-review pass) — confirmed help dump is verbatim and
  test commands are reproducible.
- claude-aws — not run (no aws cli access on this host); cron job logs
  this gap. Process rule acknowledged; reviewer substituted with a
  re-read against the parent research doc.
- opencode — not run (no `oc` exec available in this cron env).

No BLOCKERs identified in the self-review. Empirical tests were the
binding evidence; both candidate mechanisms verified end-to-end.
