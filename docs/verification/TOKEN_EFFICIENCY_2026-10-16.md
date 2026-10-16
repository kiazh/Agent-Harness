# Token-efficiency implementation — operational evidence record

Date: 2026-10-16. Baseline revision: `b7bd6b6` (working tree changes, uncommitted).
Plan: `docs/TOKEN_EFFICIENCY.md` (implementation map); research document:
`Machina_Token_Efficiency_Research_and_Implementation (1).md` sections 1–28.

## Task/status and baseline revision

Implemented T0–T6 of the research plan's priority-ordered program (measurement,
cache-stable prompts, artifacts, reducers, masking augmentation, retrieval
selection, configurable output policy). T7–T9 experiments (learned
compression, routing, semantic answer caches, warmers, serving lane, native
tool-schema deferral) are deliberately deferred with reasons in the plan doc
("no default-on without task-specific evidence") and are individually
switchable once built.

## Requirement/invariant coverage

- Preserve exact payloads / recovery honesty → store-before-lossy-publish in
  `core/tool_views.py`; reduced views always carry a working artifact handle;
  store failure/quota/oversize falls back to the legacy blind cap with no
  recovery claim (`tests/test_te_views_integration.py`).
- Missing ≠ zero → NULL cache columns for unreported categories, NULL cost for
  unknown prices, `provider_partial` basis for partial partitions
  (`tests/test_te_usage_ledger.py`).
- Protocol integrity → tool-result content strings only; tool-call pairing and
  provider-native blocks untouched (existing provider suites green).
- Access control → per-read re-authorization through session ancestry;
  sibling/unrelated sessions denied; expiry answers `expired`
  (`tests/test_te_artifacts.py`).
- Fail-open → broken store/reducer/config never break a turn (covered at unit
  and loop level).

## Files/interfaces changed

New modules: `core/artifacts.py`, `core/reducers.py`, `core/tool_views.py`,
`core/projection.py`, `core/pricing.py`, `provider/usage_map.py`,
`provider/prompt_cache.py`, `tools/artifacts.py`.
Modified: `core/usage.py`, `core/agent.py` (thin seams only), `core/prompt.py`,
`core/assembler.py`, `core/config.py`, `core/compression.py`, providers
(anthropic/chatgpt/copilot/direct/ollama/openrouter/transport), `db/schema.sql`,
`db/connection.py` (runtime migration), `memory/{retriever,consolidator}.py`,
`skills/learning.py`, `tools/__init__.py`.
DevTools (tests): new `test_te_*.py` suites; updated `test_e1_prompt_cache.py`,
`test_usage.py`, `test_history_condensation.py`, `test_turnscope_cache.py`,
`test_trace_hierarchy.py`, `test_architecture_layers.py` (documented deltas).

## Projection/cache policy versions

Debuting versions: ledger v2 (`usage_basis`/cache split/accepted), artifact
store v1 (session-scoped ACL, 30-day default retention), reducer contract v1
(`[machina view: …]` disclosure), prompt-cache profile v1 (verified
2026-10-15), pricing snapshot v1 (`PRICE_TABLE_VERSION=2026-10-16`; DeepSeek
rates from the in-repo 2026-10-14 pilot snapshot).

## Why existing abstractions were reused

Ledger extends `UsageStore` (reserve/finish); artifacts extend the existing
context-chunk storage model rather than adding a second memory system;
reducers/views extend the existing `tool_message_limit`/`condense_history`
seams in `core/prompt.py`; cache profiles extend the E1 pilot flag and
transport breakpoint helpers; projection extends the existing retrieved-chunk
list; output policy appends to `build_system_prompt` (section builders).
No proxy, no second accounting path, no parallel compression framework.

## Tests added / commands executed

```
# new suites
PYTHONPATH=<repo> MACHINA_TEST_DATABASE_URL=... \
  .venv/bin/python -m pytest tests/test_te_*.py -q        # 75 passed (final)
# full backend suite (final run, pre-report)
pytest tests/ -q                                           # 542x passed; 6 pre-existing failures
```

Pre-existing failures (verified failing on pristine `b7bd6b6` via a stashed
tree, unrelated to this change): `test_security.py::TestSecretRedactionOnDirectWrites`
(2), `test_self_knowledge.py` (2), `test_self_knowledge_completeness.py` (1),
`test_code_intelligence_tool.py::test_existing_self_knowledge_completeness_pins_still_hold`
(1). No regression attributable to this change.

Post-record update (same day): the separate self-knowledge ladder and
redaction fixes landed after this run, so all four items above now pass on
the working tree (re-run: `test_self_knowledge_completeness.py` 6 passed,
`test_self_knowledge.py` + the pin test 13 passed/2 skipped,
`TestSecretRedactionOnDirectWrites` 2 passed). The ledger's NP-03
"census green" claim is current; this list stays as the baseline record.

## Provider-reported versus estimated usage

- Provider-reported: Anthropic (input/output + cache read/creation), OpenAI
  family (`prompt_tokens_details.cached_tokens`), DeepSeek (hit/miss), ChatGPT
  (`input_tokens_details.cached_tokens`), Copilot, OpenRouter.
- Local estimates: vendor streams without usage reporting (flagged
  `local_estimate`, reservation retained).
- Cost: dated estimates only; DeepSeek is the sole built-in price entry (repo
  snapshot); everything else NULL until `pricing_table_json` is set. No
  figures are presented as invoices.

## Cold/warm conditions

No live-provider runs were performed. `prompt_cache_enabled` stays off (the
existing pilot decision); the ledger now measures cache read/write columns so
cold/warm evaluation can be run on real turns before any default changes.

## Task acceptance results and regression cases

- Reviewer passes: CodeRabbit pass 1 (4 findings — 1 major OpenRouter usage
  drop, 3 minor) all fixed; pass 2 (1 finding — estimate basis labeling)
  fixed; pass 3 run on the settled tree.
- Subagent reviews: adversarial review (10 invariants) and code-quality
  review. All substantive findings fixed or resolved:
  - store-before-lossy-publish enforced (was: reduced view could publish
    without a durable original for 1–4 KB results); disclosure no longer
    claims "re-run" on the agent path.
  - artifact paging tools exempt from reducers (a reduced `artifact_read`
    page defeated its own recovery path).
  - head+tail fallback now always keeps the verdict tail and marks the seam.
  - assembler omission disclosure leads the section (survives truncation).
  - partial usage partitions retain reported values with `provider_partial`.
  - quota counts only live rows; expired rows cannot permanently refuse a
    session.
  - `_authorized` fetches metadata only (no 2 MB content pull to authorize).
  - `mark_accepted` bounded to a 30-minute window.
  - tests-detector no longer claims a test run from a lone exit-code line;
    `_pack` sorts by priority; omission phrasing singular/plural fixed.
  - provider-package ratchet arithmetic corrected (5447 measured).

## Artifact/permission/retention checks

ACL (self + ancestors + descendants, siblings denied), expiry → `expired` /
unavailable, quota refusal without eviction, oversize refusal, store
disabled/unavailable → None, purge removes expired rows, cascade on session
delete. Documented widened retention surface in `SECURITY.md`.

## Unverified assumptions

- Real-provider cold/warm cache savings (unmeasured; the pilot stays off).
- Reducer view quality on real agent tasks (behavioral equivalence not
  claimed; only deterministic-contract tests and loop-level tests exist).
- `retrieval_token_budget=6000` is a conservative default chosen from section
  budgets, not from task measurements.
- OpenRouter's pass-through of message-tail `cache_control` follows the
  existing E1 pilot assumption; re-verify against provider docs before
  enabling.

## Rollout/rollback decision

Artifacts/reducers/projection are additive and fail-open; disable via
`artifacts_enabled`, `reducers_enabled`, `retrieval_selection_enabled` (each
falls back to pre-feature behavior). Prompt caching remains opt-in. Output
policy remains `legacy` by default. No database rollback needed: new columns
and the `tool_artifacts` table are additive; disabling features leaves durable
truth intact.

## Next action

Run the provider-cache pilot cold/warm with the ledger columns on real turns;
collect accepted-completion cost baselines before considering any default
flip or T7–T9 experimentation.
