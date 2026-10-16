# harness-evals Pilot — Judged LoCoMo QA over Production Retrieval

Date: 2026-10-14. Scope: pilot integration of `harness-evals` (Apache-2.0,
github.com/harness/harness-evals) into Machina's evaluation story; measure the
one gap it fills best — **judged answer quality / a QA protocol over LoCoMo
retrieval** — and give a data-backed adopt/no-adopt verdict for three candidate
uses. Nothing was committed. Repo writes are limited to two new files:

- `tools/harness_evals_pilot.py` — the pilot runner (new, untracked)
- `docs/verification/HARNESS_EVALS_PILOT.md` — this report (new, untracked)

No existing `machina/` or `tests/` files were modified; a `git status` check
confirms the only pilot-touched paths are the two files above (other untracked
docs in the working tree belong to other sessions and were not touched).

## TL;DR verdict

| Candidate use | Verdict | Why |
| --- | --- | --- |
| (a) Judged LoCoMo QA as a benchmark section | **Adopt** (as an opt-in, nightly/on-demand benchmark; ~1.5 days) | It runs cleanly end-to-end, produces a real quality signal the repo lacks, and costs under $1 per full corpus run. |
| (b) Conversation simulation vs the SSE approval flow | **Pilot later** (promising, ~2 days for an adapter) | The stock streaming target maps well to `POST /api/v1/sessions/{id}/prompt` (SSE), but Machina's approval resume is a two-step resolve→resume flow that needs a thin custom target. |
| (c) Baseline regression gating | **Adopt lite** (deterministic metrics on PRs, judged metrics nightly; ~0.5 day) | `compare_to_baseline` + `JsonBaselineStore` exist and fit; judged metrics are too noisy/costly for per-PR gates. |

## 1. Install result and environment

Throwaway venv outside the repo (repo `.venv` untouched):

```bash
python3 -m venv /private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode/he_pilot/venv
/private/.../he_pilot/venv/bin/pip install 'harness-evals[llm]' python-dotenv
```

Installed (Python 3.14.5): `harness-evals==0.25.5`, `openai==3.31.0`,
`anthropic==1.13.0`, `pydantic==2.14.1`, `jsonschema==4.26.0`,
`python-dotenv==1.2.4` (dotenv is a pilot-script extra, not a harness-evals
dependency). `import harness_evals` works; the package does not expose
`__version__`, so the version is read via `importlib.metadata`.

The pilot venv never imports Machina. Retrieval is a subprocess into the repo
venv (`.venv/bin/python`), fed the embedded helper
source via `python -` with `PYTHONPATH=<repo>`; ground-truth parsing and token
F1 reuse the repo's own `machina/research/locomo.py` loaded directly from file
(the module is pure stdlib).

## 2. DeepSeek-as-judge verdict

**Works, with one required shim.** Findings, verified live:

- `OpenAILLM(model=..., api_key=..., base_url="https://api.deepseek.com")`
  accepts an OpenAI-compatible `base_url`. `generate()` succeeds with
  `deepseek-chat` (so `max_completion_tokens` and `temperature` are accepted).
- **Blocker 1 — strict JSON schema:** stock `generate_json()` sends
  `response_format={"type": "json_schema", "strict": true}`; DeepSeek rejects it
  with HTTP 400 `"This response_format type is unavailable now"`. Every
  LLM-judged metric uses `generate_json`, so stock judged metrics fail on
  DeepSeek. Fix in the pilot: a ~40-line `DeepSeekJSONLLM(OpenAILLM)` subclass
  that sends `response_format={"type": "json_object"}`, injects the JSON schema
  into the prompt, strips code fences, and parses defensively. With that shim,
  `FaithfulnessMetric` and `ContextRecallMetric` run cleanly (0 metric
  exceptions across 150 judge calls).
- **Blocker 2 — embeddings:** DeepSeek has no embeddings endpoint
  (`/embeddings` → 404). `AnswerCorrectnessMetric` requires a `BaseEmbedding`
  (cosine-similarity component), so it was **not** piloted. Running it would
  need either an OpenAI-compatible embedding endpoint or a ~15-line adapter
  over `machina.memory.embeddings.embed_texts`.
- **Cost reporting:** harness-evals computes USD via `litellm`, which is not
  installed, so `usage.cost_usd` stays `None`. The pilot computes cost offline
  from token counts at the published DeepSeek rates (cache-miss input, upper
  bound; DeepSeek prompt caching makes the true number lower).
- Judge-side token accounting detail: the runner wraps every metric in its own
  `collect_token_usage()` block and copies usage onto `Score.metadata`
  (`input_tokens`/`output_tokens`); an outer collector around `a_evaluate()`
  captures nothing. The pilot reads usage from `Score.metadata`.

## 3. Pilot protocol

- **N = 50 questions**, deterministic selection: 5 evenly spaced questions with
  annotated evidence per conversation (10 conversations, 1,986 questions total,
  1,982 with evidence). The sample covers all five categories (1 multi-hop ×5,
  2 temporal ×13, 3 open-domain ×2, 4 single-hop ×19, 5 adversarial ×11).
- **Retrieval (production top-5):** mirrors `evaluate_archive_retrieval` —
  embed all turns of a conversation through the production ONNX embedder
  (`BAAI/bge-small-en-v1.5`), archive into a temporary session on local
  PostgreSQL, hybrid RRF search at depth 50 with production weights, top-5
  context; the temporary session and its rows are deleted afterwards
  (verified: zero leftover sessions in both databases). Run against
  `machina_test` by default; the measurement run used `--prod-db` (`machina`)
  because a concurrent full-pytest session was exercising `machina_test`
  (see friction §7).
- **Generation:** `deepseek-chat`, temperature 0, max 256 tokens, same prompt
  shape as `machina.research.locomo.generate_answer` (evidence block, question,
  `Answer:`), so the numbers are comparable to the repo's `--answers` mode.
- **Scoring per question:** harness-evals `ExactMatchMetric(case_sensitive=False)`,
  `ContainsMetric(case_sensitive=False)`, `NumericDiffMetric` (applied only
  when the gold answer is a bare number — one question), `FaithfulnessMetric`
  and `ContextRecallMetric` (DeepSeek judge, default threshold 0.7). The repo's
  own `token_f1` is reported alongside as the comparability anchor.
- **Concurrency 4**; per-question failures isolated; retries (3, backoff) only
  on generation. Wall time: retrieval 129.3s (5,882 archived turns + 50 hybrid
  queries), scoring 37.9s. 200 LLM calls total (50 generation + 100
  faithfulness + 50 context recall).

### Repro commands

```bash
VENV=/private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode/he_pilot/venv

# 1) self-check: assert_test pass + fail paths; --live adds a judged case
$VENV/bin/python tools/harness_evals_pilot.py --self-check
$VENV/bin/python tools/harness_evals_pilot.py --self-check --live

# 2) smoke run (4 questions)
$VENV/bin/python tools/harness_evals_pilot.py --limit 4

# 3) full N=50 run (the numbers below)
$VENV/bin/python tools/harness_evals_pilot.py --prod-db \
  --out /private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode/he_pilot/results_full.json
```

Raw per-question artifact (outside the repo):
`/private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode/he_pilot/results_full.json`.

## 4. Results (N=50, 0 failed, 0 metric exceptions)

| metric | mean | pass rate | n | notes |
| --- | --- | --- | --- | --- |
| exact_match (harness) | 0.0000 | 0.000 | 50 | too strict for NL prose answers |
| contains (harness) | 0.3600 | 0.360 | 50 | gold substring appears in the answer |
| numeric_diff (harness) | 0.0000 | 0.000 | 1 | only one bare-numeric gold in the sample |
| faithfulness (judge) | 0.9500 | 0.940 | 50 | **21/50 vacuous** ("no claims" short-circuit = 1.0); non-vacuous mean 0.914 (n=29) |
| context_recall (judge) | 0.4533 | 0.420 | 50 | **11/50 vacuous**; non-vacuous mean 0.299 (n=39) |
| token_f1 (repo anchor) | 0.2083 | 0.140 (≥0.7) | 50 | mirror of the LoCoMo-paper metric |
| retrieval evidence recall@5 | 0.3667 | any-hit@5 0.38 | 50 | production retrieval, measured on retrieved dia_ids |

By category (means):

| category | n | token_f1 | faithfulness | context_recall | retrieval recall@5 |
| --- | --- | --- | --- | --- | --- |
| 1 multi-hop | 5 | 0.063 | 0.900 | 0.333 | 0.067 |
| 2 temporal | 13 | 0.063 | 0.923 | 0.000 | 0.385 |
| 3 open-domain | 2 | 0.000 | 0.500 | 0.000 | 0.000 |
| 4 single-hop | 19 | 0.488 | 1.000 | 0.526 | 0.526 |
| 5 adversarial | 11 | 0.000 | 1.000 | 1.000 | 0.273 |

Readings: single-hop is the only strong category; temporal questions fail at
the judge layer despite moderate retrieval recall (dates need multi-turn
assembly); adversarial questions are answered with refusals ("The evidence
does not say…"), which is exactly when `ContextRecallMetric` is undefined —
it returns 1.0 there, showing the metric does not model absence-of-evidence
questions. The vacuous score counts matter: the headline faithfulness 0.95 is
inflated by 1-word/2-word answers that yield no extractable claims.

### assert_test demonstration

`--self-check` proves the pytest-style API works: `assert_test(EvalCase, [metric])`
passes a true case and raises `AssertionError` with a per-metric report for a
false case; `--self-check --live` runs a judged case through DeepSeek
(`faithfulness 1.0 — 1 of 1 claims supported`). It would translate directly
into a `tests/test_harness_evals_pilot.py` (env-gated, pilot venv) if we want
CI assertions later.

## 5. Token and cost estimate

Per question (mean): generation 229.4 in / 9.9 out; judging 707.1 in / 59.4 out
→ ≈1,006 tokens/question, of which judging is ~76% of input tokens.

Run totals (50 questions): 46,825 input / 3,466 output tokens.

Pricing snapshot used (api-docs.deepseek.com/quick_start/pricing, 2026-10-14;
`deepseek-chat` is a legacy alias served by the current flash model):

| window | per 50-question run | extrapolated to 1,986 questions |
| --- | --- | --- |
| peak (cache-miss input $0.30/M, output $1.20/M) | **$0.0182** | **$0.72** |
| off-peak (half rates) | $0.0091 | $0.36 |

Notes: this is an upper bound (all input at cache-miss rates; DeepSeek's
automatic prompt caching reduces repeated judge-prompt prefixes further).
Judging dominates (~76% of cost). A judged full-corpus LoCoMo run is therefore
sub-$1 per pass — cheap enough for nightly CI.

## 6. What worked, and friction

Worked:

- `OpenAILLM` with `base_url` → DeepSeek; generation is stable at temperature 0.
- `FaithfulnessMetric` / `ContextRecallMetric` with clear, inspectable reasons
  and per-score token metadata.
- `assert_test` pass/fail semantics and the `EvalCase` schema (backward-compat
  aliases `actual_output`/`expected_output`).
- `collect_token_usage` contextvar isolation under concurrency=4.
- Deterministic metrics behave sanely (but see friction).

Friction:

1. **DeepSeek does not support `response_format: json_schema`** — every judged
   metric needs the `DeepSeekJSONLLM` shim (included in the pilot script).
   Stock metrics work only against OpenAI/Anthropic-style struct output today.
2. **`AnswerCorrectnessMetric` needs embeddings**; DeepSeek has none. Either
   add an embedding provider or a 15-line adapter over Machina's ONNX embedder.
3. **Vacuous judge scores:** answers with no extractable claims score
   faithfulness 1.0 ("No factual claims were identified"). Any dashboard must
   count these separately (the pilot does).
4. **Deterministic metrics are built for structured outputs**, not NL QA:
   exact_match scored 0.0 on all 50; contains 0.36; numeric_diff only applies
   to bare-number golds (1/50). For NL QA the repo's `token_f1` remains the
   right deterministic anchor, with harness judges for semantics.
5. **Cost/litellm:** without `litellm`, harness-evals reports no USD; the pilot
   computes cost from tokens instead.
6. **No `__version__` attribute** — read the version via `importlib.metadata`.
7. **DB contention:** an early retrieval attempt against `machina_test` ran
   concurrently with another session's full pytest suite and hit a PostgreSQL
   deadlock; the measurement run was moved to `machina` (`--prod-db`). The
   suite finished with one deadlock error (`test_014_015_fenced_replace_aborts_stale`)
   which passes in isolation now (0.36s), so it was concurrency-induced, not a
   regression. Operational lesson: pin benchmark runs to their own database
   (or serialize with test runs).
8. **Vacuous-context quirk:** `ContextRecallMetric` on adversarial questions is
   not meaningful (returns 1.0). Filter category-5 questions when reporting it.

## 7. Conversation simulation vs the SSE approval flow (assessed, not piloted)

harness-evals ships `ConversationalStreamingHttpTarget` (registered
`conversational_streaming_http`): SSE parsing (`_parse_sse`), session ID
tracking across turns (`session_fields`, `correlation_id_field`), templated
user/continue/human-input/system-event bodies, and `human_input_events` that
pause a `ConversationSimulator` run pending human input. Machina's surface maps
as follows:

- Prompt/turn streaming: `POST /api/v1/sessions/{session_id}/prompt` (and
  `/chat`) returns SSE (`_stream_session` in `machina/api/routes/streaming.py`);
  `X-API-Key` auth via `require_api_key` maps to `ApiKeyAuth`.
- Approval pause: the SSE stream emits `needs_approval` cards; recovery is
  poll-based (`GET /api/v1/approvals?sessionId=...`), then
  `POST /api/v1/approvals/{request_id}/resolve` (JSON) and
  `POST /api/v1/approvals/{request_id}/resume` (a second SSE stream).
- Mismatch: the harness expects one human-input continuation request to the
  same target; Machina needs resolve→resume (two calls, two objects). A
  ~100-line custom `ConversationTarget` subclass (or a small proxy endpoint)
  bridges this. Nothing was piloted here; the assessment is from source
  inspection of `harness_evals/targets/conversational_streaming_http.py`,
  `conversation/simulator.py` and Machina's approval routes.

## 8. Recommendations

**(a) Judged LoCoMo QA as a benchmark section — ADOPT.**
Effort ~1.5 days, where it would live:

- Keep `tools/harness_evals_pilot.py` as the runner (rename optional) and add a
  frozen `tools/requirements-harness-evals.txt` (`harness-evals[llm]==0.25.5`,
  `python-dotenv`). Do **not** add harness-evals to repo runtime deps — it
  pulls `openai`/`anthropic`/`pydantic` into every install; keep it a
  tools-venv/CI-only dependency.
- Emit results in the same JSON shape as `machina/research/benchmark.py` rows
  (`docs`-style artifacts) so `--section locomo` can include a `judged_qa` row
  when the tool venv is present, mirroring the existing "skips honestly without
  dataset" behavior.
- Run nightly or on demand (needs `DEEPSEEK_API_KEY` + local postgres), never
  per-PR: 200 LLM calls, ~3 minutes, sub-$1 per full pass.
- Report faithfulness/context_recall **with vacuous counts** and category
  breakdown; exclude adversarial (cat 5) from context_recall. Re-check the
  `DeepSeekJSONLLM` shim when DeepSeek supports `json_schema`, then delete it.

**(b) Conversation simulation against the SSE approval flow — PILOT LATER.**
Effort ~2 days: one custom `ConversationTarget` (streaming turn endpoint +
two-step approval continuation), API-key auth config, a small fixture set of
"tool proposal → needs_approval → approve/deny → resume" scenarios, and a
gated entry point. Where: `tools/harness_evals_conversation_pilot.py`, driven
against a locally started `machina.api` (or reusing the e2e-gate harness
patterns in `tools/e2e_gate.py`). Value: automated multi-turn coverage of the
approval-pause/resume path, which today relies on hand-run e2e flows.

**(c) Baseline regression gating — ADOPT LITE.**
Effort ~0.5 day: a `tools/harness_evals_gate.py` that loads the last results
JSON into `compare_to_baseline(current, baseline, tolerance=0.05)` with a
`JsonBaselineStore` committed under `docs/verification/baselines/` (or
`.machina/`), exiting non-zero on regressions. Gate PRs only on deterministic
metrics (`token_f1`, retrieval recall — no LLM calls, hermetic); keep judged
metrics in the nightly trend report. Tolerance must account for judge noise:
with N=50 and temperature 0, expect ±0.05 swings; triage by inspecting the
per-question reasons.

## 9. Limitations / honesty notes

- Generation and judging use the same model (`deepseek-chat`); self-judging
  inflates agreement. A stronger judge (e.g. `deepseek-v4-pro`) or a second
  provider would be the next step for a headline number.
- N=50 is a pilot sample; category 3 (open-domain) has only 2 questions, so
  its row is anecdotal.
- The 1,986-question extrapolation assumes the sampled per-question token mean
  holds corpus-wide (longer conversations could raise retrieval time, but
  generation/judge prompts are bounded by top-5 context, so cost scales
  linearly with question count).
- `--prod-db` was used for the measurement run to avoid contending with a
  concurrently running test suite; rerunning against `machina_test` when idle
  is expected to reproduce identical results (retrieval is read-only over
  temporary sessions).
