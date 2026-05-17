# Tokenizer drift: `tiktoken cl100k_base` vs real provider usage

Spike 4 of the context-compactor (qike-ms/my-ai-skills#5).

## Question

Can a single `tiktoken.cl100k_base` tokenizer give us "good enough" token
estimates across all three backends we care about (Claude Code, OpenAI Codex,
Hermes), or do we need per-backend tokenizers?

The brief's bar: **drift > 15% on any backend → we need per-backend
calibration**.

## Method

`tools/measure_drift.py` harvests `(cl100k_estimate, provider_actual)` pairs
from real on-disk sessions:

- **CC (Claude Code)** — `~/.claude/projects/*/*.jsonl`, top 5 by size.
  Each assistant turn carries `message.usage.{input_tokens,
  cache_creation_input_tokens, cache_read_input_tokens}`. Provider-actual =
  the sum of those three; estimate = `cl100k_base` count of every prior
  message's flattened text.
- **OC (OpenAI Codex)** — `~/.codex/sessions/2026/**/*.jsonl`, top 5 by size.
  Each `token_count` payload has `info.last_token_usage.input_tokens`; we
  pair it with `cl100k_base` of every prior `message` / `function_call` /
  `function_call_output` / `reasoning` payload's flattened text.
- **Hermes** — `~/.hermes/sessions/*.jsonl` + `~/.hermes/state.db`. Neither
  carries `usage.input_tokens`. **No real drift measurable.**

Two pairing modes:

- **`full`** — whole-conversation cl100k vs whole-conversation provider total.
  Mixes tokenizer drift with the system prompt + tool-schema overhead that
  lives outside the session log.
- **`delta`** — incremental: `cl100k(new_content_since_last_turn)` vs
  `actual_i − actual_{i-1}`. Cancels the fixed prefix; isolates true
  tokenizer-level drift.

Fixture: `tests/fixtures/drift_pairs.json` (counts only, no message content).

## Results

Measured 2026-05-17, 5 sessions per backend, on real working sessions.

| backend / mode | n   | median ratio (actual/est) | median signed drift | p90 |abs drift| max |abs drift| verdict |
| --- | --- | --- | --- | --- | --- | --- |
| **cc / delta** | 41 | **1.40×** | **+39.6%** | 88.3% | 4273% | **fails 15% bar** |
| cc / full   | 96 | 2.09× | +109.2% | 2409% | 5905% | dominated by missing prefix |
| **oc / delta** | 49 | **1.09×** | **+8.8%** | 955% | 5316% | borderline; long tail |
| oc / full   | 58 | 1.75× | +74.5% | 160%  | 476%  | dominated by missing prefix |
| hermes / full | 5 | 1.00× | 0.0% | 0% | 0% | **no usage persisted — synthetic** |

`delta` is the meaningful row per backend. The high `oc/delta` p90/max comes
from `token_count` events on turns where the prompt was almost entirely
re-cached prefix (delta in our reconstructed text is tiny, but the provider
counts the cached read), not from tokenizer disagreement. The **median** is
robust to that and stays near the calibration target.

### Per-backend recommendation

| backend | recommendation |
| --- | --- |
| `cc` | **Per-backend tokenizer / correction required.** `cl100k_base` undercounts Anthropic input tokens by ~40% on real sessions. Apply ×1.40 (`CORRECTION_FACTORS["cc"]`). When `anthropic` Python SDK is acceptable as a dep, switch to `anthropic.Anthropic().count_tokens(...)` for higher fidelity. |
| `oc` | **Single tokenizer with light correction.** `cl100k_base` undercounts Codex/OpenAI input tokens by ~9% on real sessions. Apply ×1.09 (`CORRECTION_FACTORS["oc"]`). Acceptable to use `o200k_base` for newer GPT-4o/o1 models once Codex confirms model id. |
| `hermes` | **Action item: persist `usage.input_tokens` per turn** in `messages.token_count` (column already exists; it's NULL for every row). Until then, drift is unmeasurable; we ship a conservative ×1.10 correction and `OVERHEAD_TOKENS["hermes"]=2000`. |

## Recommendation (TL;DR)

**Per-backend calibration is required.** A single `cl100k_base` tokenizer fails
the 15% bar on CC (medians +40% on the cleanest measurement). Codex squeaks
under 15% only at the median; the per-turn variance is large enough that we
should still apply its small ×1.09 correction.

`context_manager.token_estimator` ships with the calibrated factors and
exposes `set_correction(backend, factor, overhead)` so we can update them as
we collect more billing data.

## Follow-ups (out of scope for this spike)

1. **Hermes: persist `usage.input_tokens`** in `state.db` so the next spike
   can replace the synthetic row with real data.
2. **CC: switch to `anthropic.count_tokens`** behind a feature flag (adds
   network dependency; only valuable when accuracy matters more than offline
   operation).
3. **Long-tail in `*/delta`** comes from cache-read accounting. If we start
   making compaction decisions on a per-turn basis, model cache reads
   explicitly instead of folding them into `actual`.
4. **Re-run quarterly.** New model releases shift tokenization; refresh the
   fixture and the `CORRECTION_FACTORS` numbers.

## How to reproduce

```bash
cd ~/git/context-manager
python3 -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
python tools/measure_drift.py            # re-harvests fixture from local sessions
pytest tests/test_token_estimator.py -v  # validates calibrated drift < threshold
```
