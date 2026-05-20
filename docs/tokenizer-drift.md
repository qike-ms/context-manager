# Tokenizer drift: `tiktoken cl100k_base` vs real provider usage

Spike 4 of the context-compactor (qike-ms/my-ai-skills#5).

## Question

Can a single `tiktoken.cl100k_base` tokenizer give us "good enough" token
estimates across all three backends (Claude Code, OpenAI Codex, Hermes), or
do we need per-backend calibration?

The brief's bar: **drift > 15% on any backend (median, real sessions) → we
need per-backend calibration**.

## Method

`tools/measure_drift.py` harvests `(cl100k_estimate, provider_actual)` pairs
from real on-disk sessions:

- **CC (Claude Code)** — `~/.claude/projects/*/*.jsonl`, 5 largest projects.
  Each assistant turn carries `message.usage.{input_tokens,
  cache_creation_input_tokens, cache_read_input_tokens}`. Provider-actual =
  the sum of those three (cache vs fresh is a billing optimization, not a
  tokenization difference); estimate = `cl100k_base` count of every prior
  message's flattened text.
- **OC (OpenAI Codex)** — `~/.codex/sessions/2026/**/*.jsonl`, 5 largest
  rollouts. Each `token_count` payload carries
  `info.last_token_usage.input_tokens` (per-request total; `cached_input_tokens`
  is a subset, not additive); paired with `cl100k_base` of every prior
  `message` / `function_call` / `function_call_output` / `reasoning`
  payload's flattened text.
- **Hermes** — `~/.hermes/sessions/*.jsonl` + `~/.hermes/state.db`. Neither
  carries `usage.input_tokens`. `state.db.messages.token_count` is NULL for
  every row. **No real drift measurable** — synthetic row only.

Calibration model: ordinary least-squares regression

    actual_tokens ≈ slope * cl100k(text) + intercept

per backend. `slope` captures the tokenizer ratio plus how text scales billing;
`intercept` captures the per-request fixed cost (system prompt + tool schemas +
cache prefix) that isn't visible in the session log.

A naive earlier attempt computed per-turn deltas `actual_i - actual_{i-1}`;
this was wrong because `usage.input_tokens` is a per-request total, not a
running cumulative counter, so the delta has no clean semantics. The regression
approach is robust to that and is what the code ships with.

Fixture: `tests/fixtures/drift_pairs.json` (counts only, no message content).

## Results

Measured 2026-05-17, 5 sessions per backend, on real working sessions.

| backend | n | uncalibrated median drift | fit (slope, intercept, r²) | calibrated median drift | calibrated p90 drift |
| --- | --- | --- | --- | --- | --- |
| **cc** | 85 | **+98.7%** | (0.096, 48 784, 0.36) | **13.4%** | 166% |
| **oc** | 86 | **+54.9%** | (1.258, 15 158, 0.99) | **4.6%** | 9.4% |
| hermes | 5  | n/a (synthetic) | (1.10, 2 000, n/a) | n/a | n/a |

Uncalibrated `cl100k_base` blows past the 15% bar on both real backends (CC
+99%, OC +55%). With per-backend `(slope, intercept)` calibration the median
drops well under 15% on both.

### Why CC has high p90 variance

OC's fit is excellent (r²=0.99): the prompt text the model processes is
essentially what we see in the session log, so a linear model nails it.

CC's fit is poorer (r²=0.36) because Anthropic's
`cache_creation_input_tokens` is per-API-call state — when the SDK decides
to refresh the cache, a single turn can spike from "almost free cache read"
to "create-25K-token-prefix-from-scratch" with no visible change in the
prompt text we observe. That's not tokenizer divergence; it's caching
policy invisible from the log. The median is robust to those outliers and
that's the number the spike's decision bar cares about.

### Per-backend recommendation

| backend | recommendation |
| --- | --- |
| `cc` | **Per-backend calibration required.** Bare `cl100k_base` undercounts Anthropic input by ~99% at the median (cache + tool-schema prefix dominates). Apply `CALIBRATION["cc"] = (slope=0.096, intercept=48_784)`. When `anthropic.count_tokens()` becomes acceptable as a runtime dep, switch to it for tighter p90. |
| `oc` | **Per-backend calibration required.** Bare `cl100k_base` undercounts by ~55% at the median. Apply `CALIBRATION["oc"] = (slope=1.258, intercept=15_158)` for ~5% median error and ~10% p90. |
| `hermes` | **Action item: persist `usage.input_tokens`** in `state.db.messages.token_count` (column exists; values are NULL). Until then drift is unmeasurable; we ship a conservative `CALIBRATION["hermes"] = (slope=1.10, intercept=2_000, measured=False)` and let callers see `measured=False` to decide whether to trust it. |

## Recommendation (TL;DR)

**Per-backend calibration is required.** A single `cl100k_base` tokenizer
fails the 15% drift bar on both CC and OC by a wide margin (medians +99%
and +55%). The `(slope, intercept)` model in
`context_manager.token_estimator` brings real-session median drift to
13.4% (CC) and 4.6% (OC).

`set_calibration(backend, slope, intercept)` lets us refresh the numbers as
new measurements come in; it raises `ValueError` on unknown backend names
so a typo can't silently overwrite the shared `default` calibration.

## Follow-ups (out of scope for this spike)

1. **Hermes: persist `usage.input_tokens`** in `state.db` so the next spike
   can replace the synthetic row with real data.
2. **CC: switch to `anthropic.count_tokens()`** behind a feature flag (adds
   network dependency; only valuable when p90 accuracy matters more than
   offline operation).
3. **Held-out validation set.** This spike calibrates and validates on the
   same fixture sessions. When we have a steady inflow of fresh sessions we
   should keep 1-of-5 per backend as a held-out validation set and refresh
   the fit weekly.
4. **Re-run quarterly.** New model releases shift tokenization; refresh the
   fixture and the calibration constants.

## How to reproduce

```bash
cd ~/git/context-manager
python3 -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
python tools/measure_drift.py            # re-harvests fixture from local sessions
pytest tests/test_token_estimator.py -v  # validates calibrated drift < threshold
```

## Review trail

Trio-reviewed pre-merge by codex + opencode (claude-aws was the third
intended reviewer but hit a model 404 in this iteration; codex and opencode
agreed unanimously on the BLOCKER list):

- **BLOCKER 1**: methodology — earlier "delta" mode used `actual_i - actual_{i-1}`
  which is meaningless given that `usage.input_tokens` is per-request, not
  cumulative; cache reads further contaminated it. Resolved by switching to
  linear regression and including cache as part of `actual`.
- **BLOCKER 2**: `set_correction("typo")` silently overwrote the shared
  `default` bucket via `_norm_backend` fallback. Resolved: `set_calibration`
  uses strict resolution and raises `ValueError` on unknown backends.
- **BLOCKER 3**: tiktoken-missing fallback path inflated `estimate_tokens("")`
  to 1 token. Resolved: `_cl100k` short-circuits empty input to 0.
