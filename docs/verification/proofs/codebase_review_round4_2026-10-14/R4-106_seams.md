# R4-106 · Production "test-only" seam cleanup — evidence (2026-10-14)

Lane: **R4-106** (`FIX_PLAN_ROUND4_TASKS.md` R4-106; residual from Round-5 plan `R5-S3`).
No commit (coordinator owns commits). Three bullets delivered:

1. `TestSoulSpecConformance` (`machina/soulspec/conformance.py`) — deleted; assertions moved
   onto real seams in `tests/`.
2. The ~20 `"Tests only; kept as …"` comment sites under `machina/` — each verified and
   resolved (removed with the file, or replaced with a precise, grep-backed justification).
3. `_last_search_mode` (`machina/rag/pipeline.py`) — write-only field removed; the test that
   pinned it drives the real result-level seam instead.

## Constraint compliance — pre-edit `git status --short <file>`

Every candidate file was checked **before** editing; **none were dirty at edit time** and all
were edited. Running the check at the *end* showed some files had since become dirty from
concurrently running lanes — those are listed under "Concurrent-lane writes (not R4-106)".

No file was skipped. The nominal owners (`R4-091` skills/soulspec, `R5-N` rag/pipeline) had
not written to the R4-106 files at edit time (status clean), so no collision occurred.

## Bullet 1 — `soulspec/conformance.py` deleted (fetchable/certification claim refuted)

**Probe.** `machina/soulspec/conformance.py` shipped a `Test*`-named class whose methods were
the real assertions. Grep of the whole tree (`machina/`, `tests/`, `ui/`, docs) found **no
production consumer** — only `tests/test_soulspec.py` and `tests/test_research_phase2.py`
imported it, plus the re-export in `machina/soulspec/__init__.py`.

**Fetchable-module claim: refuted.** `README.md:663` documents SoulSpec as
"Manifest/package handling, adapters and permission merging — … **no certification claim**".
There is no documented conformance/certification contract and no runtime consumer, so the
"leave it as a fetchable module" branch does not hold. Per the task, the module is deleted.

**Surprise verified at runtime.** The review note said the class was "never collected as
tests"; pytest actually **does** collect imported `Test*` classes, so
`TestSoulSpecConformance::test_merge_semantics` / `::test_progressive_disclosure` were being
run (from both test modules) before this change:

```
$ pytest tests/test_soulspec.py tests/test_research_phase2.py --collect-only -q | grep -i conformance
tests/test_soulspec.py::TestSoulSpecConformance::test_merge_semantics
tests/test_soulspec.py::TestSoulSpecConformance::test_progressive_disclosure
tests/test_research_phase2.py::TestSoulSpecConformance::test_merge_semantics
tests/test_research_phase2.py::TestSoulSpecConformance::test_progressive_disclosure
```

**Changes.**
- **Deleted** `machina/soulspec/conformance.py` (158 lines; incl. all six
  `"Tests only; kept as the SoulSpec conformance suite"` comments).
- `machina/soulspec/__init__.py` — dropped the `TestSoulSpecConformance, ValidationResult`
  import and both `__all__` entries (public-API removal, recorded here).
- Assertions relocated:
  - `validate_schema` → `tests/test_soulspec.py::TestConformance::test_conformance_schema_validation`
    now drives the real seam, `SoulSpec.from_dict` (valid spec accepted; `{"name": ""}`
    raises `ValueError`).
  - `test_merge_semantics` → already covered verbatim by
    `tests/test_soulspec.py::TestMergeSemantics` (scalar override, list union, dict
    recursive, permissions intersection). The delegating wrapper was dropped.
  - `test_progressive_disclosure` → already covered inline by
    `TestConformance::test_conformance_progressive_disclosure_levels`; delegating wrapper
    dropped.
  - `validate_package` → `tests/test_research_phase2.py` now calls `SoulSpec.from_package`
    directly (the real seam it wrapped): the success case is asserted by the already-present
    call, and the two rejection cases use `pytest.raises(ValueError)`.

Build/import proof: `import machina.soulspec` succeeds and no longer exposes the removed
names (`R4-106_probe_focused.txt`).

## Bullet 2 — sweep of the `"Tests only; kept as …"` sites

Method for each site: plain + regex grep over `machina/` for the symbol, then over `tests/`.
Result: **every flagged symbol is defined in `machina/` but has no production caller** — only
tests consume it. Removing a test-consumed public surface would require rewriting
cross-lane-owned test files (`R4-100..105`) and drop real coverage, so each is **kept with an
explicit, verified justification** and the vague comment replaced in code (no silent keeps).
The conformance module's six sites were removed outright with the file.

| # | Site (symbol) | Production callers | Test consumers | Action |
|---|---|---|---|---|
| 1 | `tools/base.py` `ToolRegistry.get_tool` | none (`.get_tool_definitions` is the live one) | test_comprehensive, test_r4_090_tools, test_property_based, test_mcp_manage_tool, test_v1_repomap_tool | kept + comment |
| 2 | `gateway/server.py` `Gateway.turn_running` | none | test_gateway, test_gateway_images | kept + comment |
| 3 | `repomap/evidence.py` `Evidence.is_covered` | none | test_repomap | kept + comment |
| 4 | `memory/scorer.py` `score_with_breakdown` | none | test_memory | kept + comment |
| 5 | `memory/user_profile.py` `UserProfile.record_interaction` | none | test_memory_approval | kept + comment |
| 6 | `memory/user_profile.py` `UserProfileStore.record_interaction` | none | test_research_integration, test_profile_atomic, test_memory_approval | kept + comment |
| 7 | `db/connection.py` `initialize_growth_schema` | none | test_personality_growth, test_code_review_fixes | kept + comment |
| 8 | `memory/identity.py` `IdentityGate.contain_drift` | none | test_identity_gate | kept + comment |
| 9 | `memory/persona.py` `delete_persona` | none | test_persona_memory | kept + comment |
| 10 | `memory/forgetting.py` `should_forget` | none | test_memory, test_reversible_eviction | kept + comment |
| 11 | `memory/forgetting.py` `get_half_life` | none | test_memory | kept + comment |
| 12 | `memory/store.py` `update_importance` | none | test_memory | kept + comment |
| 13 | `memory/store.py` `delete_by_session` | none | test_memory | kept + comment |
| 14 | `connectors/mcp_client.py` `read_stderr` | none | test_connectors_client | kept + comment |
| 15 | `memory/redaction.py` `RedactionResult.was_redacted` | none (also referenced by the class docstring example) | test_memory_approval | kept + comment |
| 16 | `memory/redaction.py` `redact_dict` | none | test_memory_approval | kept + comment |
| 17 | `memory/redaction.py` `has_secrets` | none | test_memory_approval | kept + comment |
| 18 | `research/workflow_trial.py` `effect_count` | none | workflow_trial/test_workflow_trial | kept + comment |
| 19 | `core/metrics.py` `get_counter` | none | test_metrics, test_deadlock_guard, test_fm4_usage, test_bug9_bug15_fixes, test_compression, test_fm6_audit, test_fm4_metrics, test_deep_gateway_cleanup, test_scheduler, test_fm6_diagnostics, test_reversible_eviction, test_s5_turns_counters | kept + comment |
| 20 | `core/metrics.py` `get_error_count` | none | test_metrics, test_fm4_metrics | kept + comment |
| 21 | `core/session.py` `update_state` | none (production uses `update_state_fields`) | test_load_concurrency, test_basic, test_comprehensive, test_deep_session_cache, test_integration | kept + comment |
| — | `soulspec/conformance.py` (6 comments) | none | — | **removed with the file** |

Grep proof that every symbol is test-only (only definitions appear under `machina/`, the
lone incidental `assert result.was_redacted` is inside a docstring example at
`memory/redaction.py:296`): see the transcript in `R4-106_probe_focused.txt` and the table
above. `grep -rn "Tests only; kept as" machina/` now returns **no matches**.

## Bullet 3 — `_last_search_mode` removed (`rag/pipeline.py`)

**Root cause.** `RAGPipeline` assigned `self._last_search_mode` at `search()` (values
`"hybrid"`, `"keyword-only"`) but nothing ever read it. The honest fallback mode is actually
reported via `SearchResult.retrieval_mode`, set in `_finish_search(...)`.

**Changes.**
- Removed both write-only assignments (`pipeline.py:469`, `:491`). No field declaration
  existed in `__init__`.
- `tests/test_turns_ownership.py::test_rag_cold_restart_and_keyword_fallback` — the assertion
  that pinned the field (`broken._last_search_mode == "keyword-only"`) now drives the real
  seam: `assert kw and all(h.retrieval_mode == "keyword-only" for h in kw)`.

## Tests changed (only where a removed seam pinned them)

| File | Change |
|---|---|
| `tests/test_soulspec.py` | drop `TestSoulSpecConformance` import; drop the two delegating wrappers; drive `SoulSpec.from_dict` in the schema test |
| `tests/test_research_phase2.py` | drop import; `validate_package` → `SoulSpec.from_package` (+ `pytest.raises`) |
| `tests/test_turns_ownership.py` | `_last_search_mode` → `SearchResult.retrieval_mode` |

## Files changed (R4-106)

Sources (17): `machina/soulspec/__init__.py`, `machina/soulspec/conformance.py` (**deleted**),
`machina/rag/pipeline.py`, `machina/tools/base.py`, `machina/gateway/server.py`,
`machina/repomap/evidence.py`, `machina/memory/{scorer,user_profile,identity,persona,forgetting,store,redaction}.py`,
`machina/db/connection.py`, `machina/connectors/mcp_client.py`,
`machina/research/workflow_trial.py`, `machina/core/{metrics,session}.py`
(16 comment-only + 1 module deletion + `pipeline.py` field removal).
Tests (3): `tests/test_soulspec.py`, `tests/test_research_phase2.py`,
`tests/test_turns_ownership.py`.
Evidence: this file + `R4-106_probe_focused.txt`, `R4-106_green_seam_suites.txt`,
`R4-106_green_seam_suites_retry.txt`, `R4-106_arch_triage.txt`, `R4-106_ruff.txt`.

## Files skipped

**None.** All candidate files were clean at edit time.

### Concurrent-lane writes (not R4-106)

Between the pre-edit check and the post-edit gate, other lanes (`R5-GH` provider transport,
`R5-B` atomic write, `R5-N` audit redaction, UI lanes) modified files R4-106 did not touch:
`machina/core/agent.py`, `machina/core/provider/{anthropic,chatgpt,copilot,direct,ollama,openrouter,rate_limit}.py`,
`machina/tools/file.py`, `machina/rag/chunker.py`, and several `ui/src/*` files. These
account for the pre-existing architecture/UI failures below.

## Verification

| Check | Command | Result |
|---|---|---|
| Import/collection probe | `python -c "import machina.soulspec …; '_last_search_mode' not in pipeline"` | OK |
| Full collection | `pytest tests/ --collect-only -q` | 4732 collected, 0 errors |
| Focused suites | `pytest test_soulspec test_research_phase2 test_turns_ownership test_metrics test_repomap test_fm4_metrics test_s5_turns_counters -q` | **82 passed** |
| Seam-consumer suites (DB) | 28 files incl. test_memory, test_identity_gate, test_persona_memory, test_profile_atomic, test_research_integration, test_memory_approval, test_compression, test_scheduler, test_deep_gateway_cleanup, test_gateway, test_load_concurrency, test_comprehensive, test_property_based, test_r4_090_tools, workflow_trial, test_soulspec_* | **615 passed, 5 errors**; all 5 = `asyncpg DeadlockDetectedError` at DB setup (shared `machina_test` contention) |
| Deadlock retry (isolation) | `pytest tests/workflow_trial/test_workflow_trial.py tests/test_soulspec_runtime.py -q` | **26 passed** |
| Architecture guards | `pytest tests/test_architecture_layers.py -q` | soulspec/import guards **pass**; 11 god-file-ceiling failures are all in concurrently-edited provider/agent files (see below) |
| Ruff | `ruff check` (20 files) | All checks passed |
| Format | `ruff format --check` (20 files) | 20 files already formatted |

### Architecture failures triage — not R4-106

`test_god_file_ceilings_only_fall[...]` (9 files) + `test_provider_package_total_only_falls`
fail only on provider/agent files. `R4-106_arch_triage.txt` shows the working-tree counts
against each ceiling; `machina/core/provider/audit.py` (219 > 147) and
`machina/core/agent.py` (2064 > 2057 at **HEAD**, 2110 in-work) already exceed their ceilings
from other lanes' committed/uncommitted work, and the provider-package files
(`rate_limit`, `openrouter`, `ollama`, `direct`, `anthropic`, `chatgpt`, `copilot`) sit exactly
at their ceilings at HEAD and overflow only in the dirty working tree. R4-106's diff touches
**none** of these files (diffstat above). The soulspec-related architecture guards
(`test_module_level_core_imports_only_shrink`) pass.

`tests/test_personality_growth.py::test_ui_checks_with_real_gateway_restart` fails because
`npm run check` fails on concurrently-dirty `ui/src/*` files — unrelated to R4-106.
