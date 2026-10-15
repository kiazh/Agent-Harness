# Fix batch 3 (lane B) — skill-install worker join, AuditPersistence.reset(), behavioral SSE test

Date: 2026-10-15
Lane: FIX LANE (final review batch 3), lane B.
Scope: `machina/tools/repo.py`, `machina/observability/audit.py` (reset() only),
`tests/conftest.py`, `tests/test_r5_011_013_provider_transport.py`, plus one new
evidence suite. No commits.

Forbidden files were not touched by this lane:
`machina/core/provider/audit.py`, `sse.py`, `rate_limit.py`, `runtime.py`,
`config.py` have no working-tree changes (`git diff HEAD` is clean for them).

## Findings fixed

### 1. T5 / code_review #14 (P3) — skill-install writes detached on cancellation

**Defect.** `repo_stage`/`repo_activate` ran every stage through bare
`asyncio.to_thread`, so cancelling the await never stopped the worker thread.
A cancelled install could therefore finish `write_staged_plan`/`activate_plan`
after turn ownership was released — the same class as HELD-44, which was fixed
for `write_file` only (`_run_worker_joined`, `tools/file.py:498`).

**Fix.** `machina/tools/repo.py` now imports the existing helper and routes the
filesystem-effect stages through it:

- `repo_stage`: `clone_repo`, `write_staged_plan`, and the `finally`
  `shutil.rmtree` cleanup.
- `repo_activate`: `activate_plan` (the publishing install).

The read stages (`scan_repo`, `load_staged_plan`) intentionally stay on bare
`asyncio.to_thread` — they have no effects, and `tools/file.py` documents the
same read/write split (`read_file`/`list_files` unjoined). One implementation is
reused rather than copied: `machina/core/orchestrator.py` already imports the
private `_open_secure_read`/`resolve_path` from `tools.file`, so the import is
consistent with the tree and introduces no cycle (`tools.file` does not import
`tools.repo`; the package `__init__` imports `file` before `repo`).

### 2. A3 / architecture_review P3-1 (P3) — AuditPersistence start/stop asymmetry; tests poked privates

**Defect.** `tests/conftest.py` normalized a leaked audit generation by assigning
`audit_writer._owners/_task/_queue` directly and left `_ever_started` set.

**Fix.** Added `AuditPersistence.reset()` (`machina/observability/audit.py`). It
performs the documented normalization — `_owners = 0`, `_task = None`,
`_queue = None`, and `_ever_started = False` (the architecture review explicitly
asked for `_ever_started`) — and logs a warning when it discards live state
(owner count, task, or queue) so a leak stays visible. `conftest.reset_singletons`
now calls `audit_writer.reset()`; the hand-rolled logging and private writes are
gone (`import logging` became unused and was removed). Behavior for the suite is
unchanged (all suites below green).

### 3. P11 / code_review #13 (P3) — SSE test asserted on source text

**Defect.** `tests/test_r5_011_013_provider_transport.py:167-177` scanned provider
source for `'startswith("data: ")'` and the string `"sse_data"` instead of
exercising behavior.

**Fix.** Removed the source scan and the now-unused `pathlib`/`_PROVIDER_DIR`/
`_SSE_FILES`. Same coverage is now behavioral:

- `test_r5_g2_sse_parser_tolerates_missing_space` extended with leading-BOM
  parser cases (single BOM stripped, double BOM is payload).
- New `test_r5_g2_chatgpt_stream_no_space_bom_frames` drives the real Codex
  Responses stream path with spaceless `data:` frames and a BOM first frame.
- New `test_r5_g2_copilot_stream_no_space_frames` drives the real Copilot
  stream path with spaceless `data:`/`data:[DONE]`.
- New `test_r5_g2_ollama_ndjson_needs_no_sse_prefix` drives the Ollama NDJSON
  path with a bare (unprefixed) JSON line — the behavioral inverse of the old
  `"sse_data" not in ollama.py` scan.

The pre-existing direct/anthropic/openrouter behavioral tests are retained, so
all five SSE transports plus the Ollama exclusion are now proven at the
behavioral level.

## Files changed

- `machina/tools/repo.py` — join-on-cancel for the clone/write/install/cleanup stages.
- `machina/observability/audit.py` — new `reset()` method only.
- `tests/conftest.py` — `reset_singletons` uses `audit_persistence.reset()`.
- `tests/test_r5_011_013_provider_transport.py` — source-text assertions → behavioral.
- `tests/test_final_review_2026_10_15_fix_batch3.py` — new evidence suite (reset()
  semantics + repo join-on-cancel).

## Verification

All commands use `.venv` and `MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test`.

| Evidence | Result |
|---|---|
| `logs/fix_batch3_b_focused.log` | **265 passed** — r5_011_013, the new batch-3 suite, repo/fm6 repo install, skill read, audit lifecycle/shutdown/fm6, r5_018 + r5_nb audit redaction, observability payload boundary, final-review providers |
| `logs/fix_batch3_b_broad_reset.log` | **451 passed** — the focused set plus deep runtime shutdown/cleanup, gateway providers, provider close/context-audit, metrics, SSE adversarial, skill atomicity/runtime/registry-cache, uninstall, installer env/quoting |
| `logs/fix_batch3_b_traces_audit.log` | **35 passed** — traces/observability producers + hierarchy + audit-correlated observability |
| `logs/fix_batch3_b_red_probe.log` | RED sensitivity: with `_run_worker_joined` swapped for pre-fix bare `asyncio.to_thread`, the cancelled-install scenario reports `finished set after cancel = False` (worker detached); post-fix the evidence test asserts `True` |
| `logs/fix_batch3_b_ruff_check.log` | `All checks passed!` |
| `logs/fix_batch3_b_ruff_format.log` | `5 files already formatted` |

## Known unrelated / other-lane results

- `tests/test_architecture_layers.py` reports 3 failures
  (`core/provider/audit.py-219`, `core/provider/sse.py-46`,
  `test_provider_package_total_only_falls`). These are provider-file line-count
  ceilings owned by other lanes' in-progress changes; this lane touches neither
  `core/provider/*` nor the provider package. Log:
  `logs/fix_batch3_b_architecture_layers.log`.
- `tests/test_phase6.py::test_cron_job_persists_and_reschedules` fails only under
  a specific multi-file ordering (`Database not connected`). Confirmed
  pre-existing: it fails identically with `tests/conftest.py` stashed to its
  original content, and passes in isolation. Not caused by this lane.

## Residual notes

- `_run_worker_joined` is annotated `Callable[[], str] -> str` in `tools/file.py`;
  `repo.py` passes lambdas returning tuples/lists. Runtime is unaffected (no type
  gate runs); a future cleanup could generalize the annotation. Not changed here
  because `tools/file.py` is another lane's file.
- `reset()` drops (does not drain) a writer task bound to a foreign/dead loop —
  events it still held were already lost with that loop. This matches the prior
  conftest behavior and the architecture recommendation.
