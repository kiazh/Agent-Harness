# R4-090 — Tools cluster (read_file paging/marker, atomic write, persona, schemas)

- Date: 2026-10-14
- Agent: R4-F1 (implementation subagent)
- Writer scope: `machina/tools/base.py`, `file.py`, `harness.py`, `builtins.py`, `skills.py` + `tests/test_r4_090_tools.py`
- Proof type (matrix): adversarial (H-FS) + tool E2E
- RED artifact: `R4-090_red_pytest.txt` (10 failed / 3 passed, against the unfixed tools files)
- GREEN artifacts: `R4-090_green_pytest.txt` (13 passed), `R4-090_green_required_suites.txt` (58 passed),
  `R4-090_green_affected.txt` (515 passed), `R4-090_ruff_check.txt`, `R4-090_ruff_format.txt`
- No commit.

RED/GREEN commands (focused, shared-DB safe, `-p no:cacheprovider`):

```
.venv/bin/python -m pytest tests/test_r4_090_tools.py -q -p no:cacheprovider
.venv/bin/python -m pytest tests/test_deep_fetch_deadline.py tests/test_web_crawl.py \
  tests/test_review_web_search.py tests/test_script_dependency_boundary.py \
  tests/test_architecture_layers.py -q -p no:cacheprovider
.venv/bin/ruff check <5 tools files + test file> && .venv/bin/ruff format --check <same>
```

RED was produced by `git stash push -- <the five tools files>` (restored immediately with
`git stash pop`, verified by rerunning the suite), so the same tests run against the
unfixed code for exactly the named reasons below.

## (a) read_file: offset/limit before the size gate + truncation marker

- **Probe / root cause:** `file.py` stat'ed the whole file and raised `too large` before
  any paging (`:287-292` in the unfixed revision), and then read only
  `effective_max + 1` bytes from the start, so a deep `offset` silently returned `""`;
  a page cut off at `limit` was returned with no indication that content followed
  (`:315-317`).
- **Fix:** new `_read_page()` streams the file (64 KiB bounded line pieces) and applies
  offset/limit while scanning; the byte gate now applies to the *selected page*, and a
  page that exceeds `max_bytes` still fails closed (`File ... exceeds max size during
  read`). Out-of-window lines — including pathological multi-MB single lines — are
  consumed piece-by-piece and never buffered whole. When the page is truncated at
  `limit` and more lines exist, the result ends with
  `[read_file: showing lines A-B of TOTAL; use offset=N to continue]`; a page that
  reaches EOF carries no marker. `max_size`/`limit`/`offset` schema descriptions now
  document units/defaults; the hard 5 MB page cap is named (`_READ_MAX_PAGE_BYTES`).
- **RED:** `test_read_file_pages_a_file_larger_than_max_size`,
  `test_read_file_marker_reports_truncation_and_last_page_is_clean`,
  `test_read_file_pages_past_a_giant_line_without_buffering_it` all failed against the
  unfixed code with the early `too large` rejection / silent truncation.
- **GREEN:** the same three pass; `test_read_file_small_file_result_is_unchanged`
  anchors that marker-free results are byte-identical to the old behavior, and
  `tests/test_code_quality.py::test_read_file_rejects_oversized_file` (2048-byte single
  line, `max_size=1024`) still fails closed because the page itself exceeds the cap.

## (b) write_file: atomic temp + fsync + replace (ENOSPC cannot destroy the original)

- **Probe / root cause:** the old write path opened the target with `O_CREAT` (no
  `O_TRUNC`), validated the descriptor, then `os.ftruncate(fd, 0)` and wrote in place
  (`:357-405`). A mid-write failure (ENOSPC, crash, cancellation) left the original
  truncated/damaged.
- **Fix:** new `_atomic_write_file()` writes the complete content to a random-name
  `mkstemp` temp in the target directory (O_EXCL), `flush` + `fsync`, then `os.replace`
  and best-effort directory fsync. Failure at any point unlinks the temp and leaves the
  target untouched. The pre-existing validation probe (O_NOFOLLOW, fd/path TOCTOU
  verify, hard-link refusal) is preserved; the probe descriptor is closed before the
  rename (Windows cannot replace an open file) and existing file mode bits are carried
  onto the temp before replace.
- **RED:** `test_write_file_enospc_mid_write_preserves_original` (write handle fails
  mid-write; the old code had already truncated the original — the test captures the
  target content *at write time* and fails with `''` instead of `ORIGINAL`) and
  `test_write_file_fsync_failure_leaves_original_and_no_temp` (failure after write,
  before replace; old code never fsyncs and replaces the original).
- **GREEN:** both pass with no temp leftovers;
  `test_write_file_rewrite_preserves_existing_mode` and
  `test_write_file_success_and_nested_creation_still_work` pin the compatibility
  surface; `tests/test_handoff_regressions.py` (ancestor-swap and final-symlink
  refusals, nested create/overwrite) stays green.

## (c) harness_status._persona_summary: typed failure marker

- **Probe / root cause:** both `definition is None` and any exception returned
  `{"available": False}` (`harness.py:165-203`), even though the docstring already
  claimed "failure is never mistaken for no persona".
- **Fix:** absent definition → `{"available": False, "reason": "no_agent_definition"}`;
  resolution failure → `{"available": False, "reason": "persona_resolution_failed"}`
  plus a sanitized `record_failure("tools.harness.persona", error)` (type only; the
  exception text is never echoed).
- **RED:** `test_persona_summary_separates_absent_from_failure` and
  `test_harness_status_surfaces_persona_resolution_failure` failed (both states were
  the identical `{"available": False}`).
- **GREEN:** both pass; the failure payload contains the typed reason and not the
  exception text.

## (d) additionalProperties: declaration ⇔ validator, no drift

- **Probe / root cause:** `_validate_tool_args` always rejected unknown arguments
  (`base.py:234-261`), while 27/31 registered schemas sent to the model omitted
  `additionalProperties` (JSON Schema's implicit default is *true*) — the model was
  shown a schema the runtime would not honor.
- **Direction chosen:** declare `additionalProperties` on every model-facing schema.
  `get_tool_definitions()` now normalizes each schema through `_declared_parameters()`
  (copy; non-boolean values become `false` because per-property schemas cannot be
  enforced); `_validate_tool_args` honors an explicit `additionalProperties: true`
  and stays fail-closed otherwise. `tool.parameters` itself is untouched, so existing
  identity assertions on registrations are unaffected.
- **RED:** `test_every_model_facing_tool_schema_declares_additional_properties` failed
  on the first definition without the key; `test_additional_properties_true_is_honored_by_the_validator`
  failed because the validator rejected extras even for a schema declaring `true`.
- **GREEN:** both pass; the contract test walks every registered definition and checks
  the runtime validator agrees with the declaration in both directions.

## (e) real parameter schemas for the five schema-less tools

- **Probe / root cause:** `web_search`, `web_extract`, `search_files`, `skill_list`,
  `skill_read` relied on `_infer_schema` (placeholder descriptions = parameter names,
  no ranges/defaults).
- **Fix:** explicit `parameters` on each registration with units/ranges/defaults:
  - `web_search.query` (string), `limit` 1–10 default 5;
  - `web_extract.url` (public http(s));
  - `search_files.pattern` (max 200 chars), `path` default `"."`, `file_glob` default `"*"`;
  - `skill_list.query` default `""`, `offset` minimum 0 default 0;
  - `skill_read.skill_name` required, `offset` minimum 0 default 0, `limit` 1–8000 default 800,
    `query` optional heading jump.
- **RED:** `test_five_tools_declare_typed_parameters_with_ranges_and_defaults` failed on
  the inferred placeholder descriptions (and missing `minimum`/`maximum`/`default`).
- **GREEN:** passes.

## Verification runs (this tree)

| Command | Result |
|---|---|
| `pytest tests/test_r4_090_tools.py` (13 adversarial tests, RED) | 10 failed, 3 passed → `R4-090_red_pytest.txt` |
| `pytest tests/test_r4_090_tools.py` (GREEN) | 13 passed → `R4-090_green_pytest.txt` |
| required suites: deep_fetch_deadline, web_crawl, review_web_search, script_dependency_boundary, architecture_layers | 58 passed → `R4-090_green_required_suites.txt` |
| affected sweep: code_quality, handoff_regressions, comprehensive, tool-cache/redos, execution-context boundary, skills (runtime/fm6/ecc/registry-cache/workflows), self_knowledge, repo_install, web_crawl/web_extract pinned fetch, research file-tools | 515 passed → `R4-090_green_affected.txt` |
| ruff check / ruff format --check (5 files + test) | clean → `R4-090_ruff_check.txt`, `R4-090_ruff_format.txt` |

Resume re-verification at current HEAD (no source edits this pass; artifacts regenerated):
`test_r4_090_tools.py` 13 passed, required suites 58 passed, ruff clean; affected sweep
501 passed excluding the live-DB `test_self_knowledge` streaming test (see contention note).

Notes on shared-DB contention (not caused by this diff; each passes in isolation):
`tests/test_http_approval_resume.py` hit `asyncpg DeadlockDetectedError` while other
agents ran schema work against the shared test DB (8 passed / 2-4 errors depending on
the moment). `tests/test_property_based.py::TestAsyncTokenBucketProperties::test_tokens_never_negative`
fails a Hypothesis function-scoped-fixture health check in the untouched rate limiter;
`test_llm_response_creation` passed in isolation.
Resume pass: `test_self_knowledge.py::test_live_agent_inspects_own_state_without_approval_or_truncated_evidence`
failed with `DeadlockDetectedError` (AccessExclusiveLock vs RowShareLock on shared relations —
concurrent DDL from other agents on the shared test DB), retried once in isolation per protocol
and still deadlocked. This lane touches no DB code (`record_failure` is in-process metrics +
logger only, verified at `core/metrics.py:139-142`); classified environmental.

## Residuals / watch-outs

- Reporting the total line count costs one read to EOF per paged call (O(file) I/O,
  memory bounded by the 64 KiB line chunk plus the page byte cap). If a future task
  pages multi-GB files routinely, cap the census and switch the marker to `N+`.
- The `minimum`/`maximum` keywords in the new schemas are advisory (the runtime
  validator does not enforce numeric ranges), matching `search_documents`' existing
  convention; the tool bodies still clamp/validate (`web_search` clamps 1–10).
- `additionalProperties: true` remains an explicit opt-in for future tools; no
  built-in tool uses it. Dict-valued `additionalProperties` schemas are normalized to
  `false` deliberately (unimplemented at runtime).
- `write_file` still performs the O_CREAT validation probe before writing the temp; a
  rejected new target is cleaned up as before (strict no-effects preserved). Temp files
  are transient and random-named; they are removed on every failure path.
- No docs outside this proof were edited; the task index/ledger status is left to the
  orchestrator (no commit made).
