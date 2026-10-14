# External Round 3 runtime fixes — re-run evidence (2026-10-14)

Re-runs of the **unedited** review reproductions in
`../codebase_review_round3_2026-10-14/` after the runtime/transport/UI fixes
for findings 5, 6, 7, 8, 9, and 12. Fixing commits are not made here (no
commit); this directory holds the acceptance evidence and the gate logs.

`*.pre-fix.log` snapshots were captured on this worktree before the fixes
(they match the values recorded in `CODEBASE_REVIEW_ROUND3_2026-10-14.md`);
`*.fixed.log` are the post-fix re-runs. Finding 4's catalog account-scoping
section was already fixed at `703ad81` and is included in the re-run for
completeness; no code was changed for it.

## Run instructions

Backend scripts (from the repository root, repository virtualenv Python;
isolate `HOME` and point `MACHINA_ENV_FILE` at an empty file in a scratch
directory first, as in the review README):

```sh
HOME=<scratch>/home MACHINA_ENV_FILE=<scratch>/empty.env \
  PYTHONPATH=$PWD .venv/bin/python \
  ../codebase_review_round3_2026-10-14/<script>.py
```

`skill-cap.py` additionally uses the hard-coded `machina_test` database
(run it separately from other schema/setup work).

UI script (scratch dir with `src` symlinked to `ui/src`, `node_modules`
symlinked to `ui/node_modules`, `test/ui-tail.ts` copied from the review
directory):

```sh
cd <scratch> && node --no-warnings --experimental-strip-types test/ui-tail.ts
```

## Observed results

| Finding | Repro | Pre-fix | Post-fix |
| --- | --- | --- | --- |
| 5 image blocks | `catalog-transport.py` payload section | `content types ['input_text']`, `Image serialized as text: True` | `['input_text', 'input_image']`, `False` |
| 6 cache endpoint identity | `catalog-transport.py` two-host section | `destinations ['before.invalid']`, both responses from the first host | `['before.invalid', 'after.invalid']`, each response from its own host |
| 7 atomic skill cap | `skill-cap.py` | statuses `['approved', 'approved']`, count 2, 2 skills live | statuses `['approved', 'pending']`, count 1, 1 skill live |
| 8 OpenRouter id | `embedding.py` | request model `text-embedding-3-small` | request model `openai/text-embedding-3-small` |
| 9 overflow kill | `runner.py` | `elapsed 0.603`, `late_side_effect True`, rc 0 | `elapsed 0.05`, `late_side_effect False`, rc -9 (SIGKILL), `truncated True`, `timed_out False` |
| 12 switch-back replay | `ui-tail.ts` | `partialCopies 2`, `staleRunning true`, `completedToolVisible false` | `partialCopies 1`, `staleRunning false`, `completedToolVisible true` |

## Gate logs

* `pytest-affected.log` — chatgpt provider, providers direct, embedder local,
  learning review, mcp_server, interop, architecture ratchets:
  **247 passed**.
* `ui-check.log` — `cd ui && npm run check`: typecheck + **423 tests
  (417 pass / 6 skipped / 0 fail)** + eslint green.
* `ruff.log` — `ruff check` + `ruff format --check` on the touched Python
  files: all checks passed, all files formatted.

## Added pins

* `tests/test_chatgpt_provider.py` — `test_multimodal_user_content_maps_to_responses_image_inputs`,
  `test_plain_string_user_content_keeps_input_text_fallback`,
  `test_multimodal_payload_sends_a_real_input_image`.
* `tests/test_providers_direct.py` — `test_direct_cache_namespace_includes_endpoint_and_credential`.
* `tests/test_embedder_local.py` — `test_openrouter_fallback_keeps_the_qualified_model_id`,
  `test_qualified_large_model_keeps_its_dimension`.
* `tests/test_mcp_server.py` — `test_run_bounded_overflow_kills_the_child_during_reader_wait`.
* `tests/test_learning_review.py` — `test_concurrent_auto_apply_respects_the_daily_cap`.
* `ui/test/tail-replay.test.ts` — `switch-back reconciles a live turn instead of duplicating it`
  (App-level sequence equivalent to `ui-tail.ts`).
