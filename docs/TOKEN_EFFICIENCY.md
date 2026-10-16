# Token efficiency

Machina measures and reduces unnecessary reading, repetition, and orchestration
without weakening model capability or requested output detail. This document
is the implementation map for the token-efficiency pass (2026-10-16) and the
contract each feature holds to.

Priority order (research plan §1): accurate measurement first, then cache-stable
prompts, durable artifacts with authorized recovery, deterministic reducers,
observation masking, selective retrieval, lazy schemas, and concise output.
Learned compression, routing, semantic answer caching, and cache warmers are
deliberately **not enabled** — the plan requires task-specific evidence before
any of them default on, and this checkout has no such evidence.

## What is implemented

### T0 — Usage ledger (always on)

One authoritative accounting path (`machina/core/usage.py`, `llm_usage` table):

- **Normalized token categories.** Provider blocks are parsed into a mutually
  exclusive split (`machina/core/provider/usage_map.py`):
  `prompt_tokens (logical input) = uncached + cache_read + cache_write`.
  Anthropic's separate cache categories, OpenAI's cached-subset
  (`prompt_tokens_details.cached_tokens`), DeepSeek's hit/miss split, and the
  ChatGPT Responses details each map to the same shape. Responses without
  cache reporting keep the legacy three-key shape and store NULL cache
  columns — missing is not zero.
- **Usage basis.** `usage_basis` records `provider`, `provider_partial`,
  `local_estimate`, `missing`, or `failed`; estimates never release the
  conservative reservation and are never presented as bills.
- **Task attribution.** Every row carries `requested_model`, `resolved model`
  (`model`), `purpose` (`agent`, `compression_summary`, `memory_rerank`,
  `memory_consolidation`, `skill_learning`), and session/agent ancestry.
- **Cost.** `machina/core/pricing.py` holds dated USD/MTok snapshots keyed
  `provider/model` (only DeepSeek's chat model has an in-repo verified
  snapshot; extend via `pricing_table_json`) and computes `cost_usd` +
  `price_version`. An unlisted model stays unknown = NULL cost, never a
  sibling's rate; an override that omits its own date/source is recorded as
  unverified/override, never as the snapshot's provenance. Cache-absent
  responses treat the whole prompt as uncached (the conservative upper bound).
- **Accepted completions.** The agent marks the turn-final call of a
  successful turn (`accepted = true`). The durable `usage_store.summary()`
  payload reports `acceptedCompletions`, `knownCostUsd`, `unknownCostCalls`,
  and `costPerAcceptedCompletionUsd` (= known cost / accepted turns; None
  when a turn never completed); the `machina usage` CLI table does not render
  these fields yet. Retries, failed calls, summaries, reranks, and
  consolidation calls count toward cost; only natural completions count as
  accepted.
- **Cache usage is now measurable**: cache read/write tokens are parsed for
  Anthropic (stream + non-stream), OpenAI-compatible endpoints, DeepSeek,
  ChatGPT, Copilot, and OpenRouter, so the prompt-cache pilot below can be
  evaluated on real tasks instead of guesswork.

### T1 — Cache-stable prompts (capability-gated, opt-in)

- `machina/core/provider/prompt_cache.py` holds the per-provider **capability
  profile** (mode, minimum prefix, breakpoint budget, usage fields, verified
  date). Unknown = unsupported until verified; OpenAI-compatible transport
  does not imply cache behavior.
- With `prompt_cache_enabled` on (default **off**, existing pilot), Anthropic
  requests get explicit `cache_control` breakpoints on the system block (when
  present), the last tool schema, and the **conversation tail** — the last
  message's final content block. Tool continuations append messages, so each
  iteration can read the previous prefix from cache; the 4-breakpoint budget
  is enforced. OpenRouter marks the system tail when present and otherwise
  the conversation tail for Anthropic-routed models only. Off-path payloads
  remain byte-identical (pinned by `tests/test_e1_prompt_cache.py`).
- The debug log line for attached breakpoints states the saving is
  unmeasured; use the ledger's cache columns to evaluate real turns before
  changing any default.

### T2 — Durable artifacts (on; fail-open)

`machina/core/artifacts.py` + `tool_artifacts` table:

- Oversized tool results are stored **verbatim before** any lossy view is
  published. Store refusal/failure/oversize/quota returns "no artifact" and
  the caller keeps its legacy behavior — a lossy view is never labeled
  recoverable without a stored original.
- **Authorized recovery** via `artifact_read` (character paging with the true
  total), `artifact_search` (case-insensitive literal substring with
  positions and excerpts), `artifact_describe` (metadata). Every call
  re-authorizes: a session may read artifacts of its ancestors and
  descendants (the delegation tree), never unrelated sessions.
- **Retention and quota**: `artifact_retention_days` (0 = no expiry),
  `artifact_max_per_session` (quotas refuse new stores; they never evict live
  evidence), `artifact_max_chars`. Expired rows purge opportunistically and
  answer "expired" honestly in the meantime. Rows cascade with session
  deletion.
- Durable `tool_call` context chunks carry the `artifact_handle` when one
  exists, so later turns can recover beyond the chunk copy's 65k cap.

### T3 — Deterministic reducers (on; fail-open; requires durable recovery)

`machina/core/reducers.py` converts over-cap tool results into bounded,
information-dense views — tests (counts, decisive failures, zero-collected
flags), logs (repetition grouping, ERROR/WARN verbatim), JSON (outline,
counts, verbatim head preserving lexical forms like big integers), CSV (row
count, numeric min/max, missingness), diffs (file/hunk counts, samples), code
(structural outline) — with a head+tail fallback whose omission seam is
marked. Contract:

- Deterministic; no model calls.
- Parse failure or no net saving ⇒ `changed=False` and the caller keeps its
  existing behavior; a lone process exit line is not treated as a test run.
- **Store-before-lossy-publish (agent path):** a reduced view is published
  only when the exact original was durably retained first — the disclosure
  carries a working `artifact_read` handle. Store disabled, unavailable,
  quota-refused, or oversize ⇒ the legacy blind append cap is kept and no
  recovery is claimed. There is no "re-run the tool" pointer on the agent
  path because deterministic tools would reproduce the same view.
- The head+tail fallback always keeps both ends: verdict tails survive.
- Token counts are computed after the disclosure wrapper; a view must fit the
  append budget and be smaller than the original to be published.
- Reduced views are exempt from `condense_history` (re-condensing a curated
  view would mutilate it and hide its handle). Skill pages, status snapshots,
  document search results, and the artifact paging tools themselves
  (offset-addressed) are exempt from reducers but still obey their append cap
  (a page-aware 4000-char bound for the artifact tools).

### T4 — Observation masking with handles

`condense_history` already replaced stale rounds with head+tail previews;
it now skips reducer-produced views so their structure and artifact handle
survive, and oversized results carry their handle from append time. Masking
remains deterministic text reduction — no summaries on every turn, no
recursive summarization.

### T5 — Evidence projection (on; fail-open)

`machina/core/projection.py` runs before prompt assembly: exact duplicates
(whitespace-normalized) across memory/RAG/archive evidence are removed, then
entries are kept in priority order until `retrieval_token_budget` is spent.
Conflicting or distinct passages are never deduplicated. Omitted entries are
disclosed in the prompt (`[N lower-ranked retrieved excerpts omitted by the
token budget]`); evidence is never dropped entirely — the first entry
survives even a zero-fit budget.

### T6 — Output policy (configurable)

`output_policy: legacy | concise`. `legacy` (default) keeps prompts
byte-identical to the pre-feature assembly. `concise` appends an answer-first
policy that explicitly preserves requirements, caveats, negations, numbers,
units, exact code, commands, paths, and errors, and defers to the user's
requested detail. Visible brevity is never a substitute for requested depth;
reasoning budgets are untouched.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `artifacts_enabled` | `true` | Durable originals + recovery tools |
| `artifact_retention_days` | `30` | `0` = no expiry |
| `artifact_max_per_session` | `500` | `0` = unlimited; quota refuses, never evicts |
| `artifact_max_chars` | `2000000` | Larger results keep legacy truncation |
| `reducers_enabled` | `true` | Format reducers for over-cap results |
| `retrieval_selection_enabled` | `true` | Dedup + budgeted evidence selection |
| `retrieval_token_budget` | `6000` | `0` disables the budget |
| `output_policy` | `legacy` | `legacy` or `concise` |
| `pricing_table_json` | `""` | Optional price overrides (JSON object) |
| `prompt_cache_enabled` | `false` | Existing pilot; now capability-gated with tail breakpoints |

All keys are validated (`machina/core/config.py`) and settable through the
normal config surface.

## What is deliberately not enabled

- **Learned compression / LLMLingua-style pruning** — needs local model
  evaluation and hardware-cost accounting; not shipped.
- **Model routing / reasoning-effort adaptation** — requires calibrated
  low-risk task classes and escalation cost measurement.
- **Semantic answer caching** — unsafe for mutable tasks, code edits, and
  permissions without task-identity machinery.
- **Cache warmers / keepalives** — economics (and privacy) unproven; the plan
  says keep them off initially.
- **Progressive native-tool schema deferral** — a behavioral change to every
  request; MCP already ships lazy schema discovery, and the native path needs
  provider-native deferral support and measurement first.

## Failure behavior (tested)

- Artifact store down/full/quota/oversize ⇒ blind cap, no recovery claim;
  reducers never publish an unrecoverable lossy view on the agent path.
- Reducer parse failure / no saving ⇒ unchanged, blind cap.
- Broken config reads never break a turn; each feature falls back to its own
  default (reducers/artifacts stay on, prompt caching stays off, output policy
  is legacy, projection skips itself).
- Missing provider usage ⇒ reservation keeps, NULL cache columns, NULL cost.
- Out-of-scope or expired handle ⇒ typed unavailable error, no weaker path.

## Verification

- `Machina-DevTools/tests/test_te_*.py` — ledger normalization/pricing/
  acceptance, cache profiles and tail breakpoints, artifact ACLs/expiry/
  quota/paging/search, reducer contract, projection, output policy,
  assembler omissions, and end-to-end loop views (reducer + handle paths,
  fail-open paths).
- Existing suites updated where they pinned the previous pilot shape:
  `test_e1_prompt_cache.py` (conversation-tail breakpoint), `test_usage.py`
  (totals aggregation), `test_history_condensation.py` (append cap kept out
  of the condensation scenario), `test_turnscope_cache.py` /
  `test_trace_hierarchy.py` (ledger-v2 INSERT argument positions), and
  `test_architecture_layers.py` (documented god-file/import ratchet deltas).

No live-provider savings claim is made. Enable `prompt_cache_enabled` and
compare the ledger's `cacheReadTokens`/`cost_usd` across cold/warm turns
before drawing conclusions.
