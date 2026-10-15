# L2R — memory/redaction fixes (H02, H05, H06)

Lane: L2R (replacement writer). Repo: /Users/shadow/machina. Source: docs/verification/CODEBASE_REVIEW_HOLISTIC_2026-10-15.md.
Owned files: machina/memory/redaction.py, machina/memory/retriever.py, machina/tools/memory.py,
machina/api/routes/memory.py, machina/gateway/features/memory.py (+ their tests only).

## Timeline

- [2026-10-15] Lane start (replacement writer). Read findings H02/H05/H06 and the reviewer diagnostics
  (docs/verification/proofs/holistic_review_2026-10-15/isolated_diagnostics.txt). Evidence file created.
- Recon: `embed_query()` exists at machina/memory/embeddings.py:75 and returns None when no embedder is
  configured (keyword fallback). `MemoryRetriever.retrieve()` only starts dense search when
  `query_embedding` is truthy (retriever.py:156). No typed retrieval error exists yet in the memory module.
- [2026-10-15] REPRODUCED H02 with the reviewer's exact synthetic diagnostic (`logs/h02_repro.py`):
  chunks `["sk-" + "a"*210, "b"*80 + " done."]` → batch `[REDACTED_OPENAI_KEY] done.`, stream
  `[REDACTED_OPENAI_KEY]` + 80×`b` + ` done.` — the 80-char tail is emitted raw. 80-char leak confirmed.
- [2026-10-15] RED written: tests/test_holistic_l2r_memory_fixes.py H02 tests (7 long-token cases + reviewer
  repro + many-chunk sweep). Confirmed failing on current source (char-by-char sk case leaks 9×`a` + 80×`b`).
- [2026-10-15] FIX (H02, GREEN): redaction.py `_PARTIAL_RES` bounds now mirror their complete patterns —
  all unbounded complete formats (sk family, AIza, xox, Bearer, password, generic assignment, quoted key,
  DB userinfo, `//`, name+whitespace gap, JWT) use unbounded partials; fixed-length formats keep finite caps.
  Reviewer diagnostic now stream == batch (`logs/h02_repro.py`); leak matrix all `same=True` (`logs/h02_matrix*.py`).
  Regression suites: 291 passed across redaction_adversarial, fm5_redaction, handoff_regressions,
  final_review_batch3, observability_payload_boundary, transcript_export, r5 audit redaction suites.
- [2026-10-15] RED + FIX (H05, GREEN): explicit search entry points now prepare and pass a dense
  `query_embedding`, mirroring agent.py's ordinary preparation; None keeps them keyword-only. The preparation
  is shared as `MemoryRetriever.prepare_query_embedding()` (staticmethod, retriever.py) so the tool/API/gateway
  surfaces share one call site and add no import churn. API (`GET /api/v1/memory/search`) and gateway
  (`memory.search`) responses gain `"mode": "dense+sparse" | "keyword-only"` (response-shape addition; the UI
  destructures `results`, so additive-safe). RED run: 4 H05 tests failed pre-fix; GREEN: 14 passed.
  Regression: 226 passed (gateway_memory_usage, api, api_route_table, api_bugfixes, memory suites, persona,
  shared_bus, agent_factory_memory) + 81 passed (retrieval_eval, fm7_cli, gateway, gateway_features).
- [2026-10-15] REPRODUCED H06 with the reviewer's diagnostic #2 (`logs/h06_repro.py` pre-fix output):
  synthetic `ConnectionError` from `_keyword_search` → `retrieve()` returned `[]` and the tool said
  "No memories found". RED written: 5 H06 tests failed pre-fix (typed error at retriever level, tool, API,
  gateway). GREEN after fix (`logs/h06_repro.py` now shows `MemoryRetrievalError`).
- [2026-10-15] FIX (H06, GREEN): added `MemoryRetrievalError(MachinaError, RuntimeError)` to
  machina/memory/retriever.py; `retrieve()` counts failed attempted branches and raises only when EVERY
  attempted branch failed (partial success preserved; genuinely-empty stays `[]`). Callers: tools/memory.recall
  surfaces it through its existing ToolError wrapping; API search returns HTTP 503
  "memory retrieval unavailable: …"; gateway memory.search returns RpcError(OPERATION_FAILED, …).
- [2026-10-15] Final focused green (566 passed): `logs/L2_focused_green.txt` (24 new tests + H02/H05/H06
  suites + memory/API/gateway/persona/embedding fail-open suites). Ruff check + format clean on all seven
  files (`logs/L2_ruff.txt`).
- [2026-10-15] Full-suite visibility: `-x` run stops on another lane's in-flight ratchet
  (`tests/test_a5_turn_flow.py::test_agent_py_shrank_below_its_pre_extraction_size`: agent.py 2269 > ceiling
  2267 — core/agent.py is outside this lane). Architecture ratchet `test_deferred_import_count_ratchet` is at
  721 > cap 717: pre-existing overrun from other lanes; this lane's final footprint is +1 function-level import
  (`prepare_query_embedding`'s lazy `embed_query` bind, the agent.py pattern), down from +3 before the shared
  helper refactor.
- [2026-10-15] FULL SUITE (no `-x`, `logs/L2_full_suite_no_x.txt`): **5108 passed, 8 failed, 1 skipped,
  1 error**. Every failure is outside this lane: 4 ratchet/census failures from other lanes' in-flight edits
  (agent.py 2269 > 2267 ceiling, copilot.py ceiling, provider package total, deferred-import census 721 > 717
  — the benchmark tool test asserts the same census); 2 `test_gateway.py` session tests and 1
  `test_multi_worker_fencing.py` error pass in isolation (full-suite ordering pollution). The benchmark's
  retrieval section (which exercises the modified retriever) passed with faithfulness 1.0. No failure touches
  the redaction/retriever/tool/API/gateway-memory files.

## Per-finding report

### H02 (P1, security) — streaming redaction emitted the tail of long secret-shaped tokens

- **Reproduced?** Yes, exactly as the reviewer described. Chunks `["sk-" + "a"*210, "b"*80 + " done."]`:
  batch `[REDACTED_OPENAI_KEY] done.`; stream emitted `[REDACTED_OPENAI_KEY]` + 80×`b` + ` done.`
  (`logs/h02_repro.py`; matrix of 8 formats in `logs/h02_matrix.py`, 5 in `logs/h02_matrix2.py`).
- **Root cause:** `_PARTIAL_RES` end-anchored partials had finite caps (`{0,200}`, `{0,500}`, …) while the
  corresponding complete patterns are unbounded (`{20,}`, `{16,}`, `[^@]+`). Once a still-growing token
  outgrew the cap the partial stopped matching, the prefix was emitted as a marker, and the continuation had
  no identifying lead — it streamed raw. Also reachable via >10-space assignment gaps (`\s{1,10}` vs `\s*`)
  and long DB userinfo.
- **Fix:** every partial whose complete pattern is unbounded is now unbounded too (recognized until the
  delimiter; marker emitted only with the token whole). Fixed-length formats (GitHub 36, PAT 255, AKIA 16,
  SSN 9) keep finite caps; PEM handling unchanged (verified the BEGIN-fragment hold already covers long
  markers). Policy comments and the class docstring updated. No state machine added — matching the existing
  R4-089 digit-run precedent, only emission timing changes, never final output.
- **Files:** machina/memory/redaction.py.
- **Tests:** tests/test_holistic_l2r_memory_fixes.py (`test_h02_long_token_stream_matches_batch` for 7 cases at
  chunk sizes 1/7/40 + 2-way splits, `test_h02_reviewer_diagnostic_long_sk_tail_never_emitted`,
  `test_h02_stream_state_survives_delimiter_across_many_chunks`).
- **Callouts:** none for behavior (stream output now equals batch); unbounded tokens are withheld until their
  delimiter, so emission of such a run can lag for long outputs — the same accepted trade-off as the R4-089
  digit run. Residual note: `_PEM_WORD_RE` keeps its 40-char cap by design; verified not reachable as a leak
  because the `rfind("BEGIN")` branch holds any unterminated marker fragment.

### H05 (P2) — explicit semantic searches never requested dense retrieval

- **Reproduced?** Yes (source trace + RED tests): all three entry points called `retrieve()` without
  `query_embedding`; with a working embedder (monkeypatched `embed_query`) the retriever captured no
  embedding and stayed keyword-only.
- **Fix:** new `MemoryRetriever.prepare_query_embedding(query)` (staticmethod) wraps `embed_query()` and is
  called by `tools/memory.recall`, `api/routes/memory.search_memory`, and
  `gateway/features/memory.memory_search`; the prepared vector is passed to `retrieve(query_embedding=…)`.
  None (no embedder / embedding failure) leaves retrieval keyword-only. API and gateway responses add an
  honest `"mode"` field (`dense+sparse` / `keyword-only`, same vocabulary as `embedding_status()`).
- **Files:** machina/memory/retriever.py, machina/tools/memory.py, machina/api/routes/memory.py,
  machina/gateway/features/memory.py.
- **Tests:** `test_h05_recall_tool_prepares_query_embedding`, `test_h05_recall_tool_keyword_only_without_embedder`,
  `test_h05_api_search_requests_dense_and_reports_mode`,
  `test_h05_api_search_reports_keyword_only_without_embedder`,
  `test_h05_api_http_reports_mode_and_uses_embedding` (through the ASGI app),
  `test_h05_gateway_search_requests_dense_and_reports_mode`; updated
  tests/test_gateway_memory_usage.py class mocks to configure the new static method.
- **Callouts — response-shape change:** `GET /api/v1/memory/search` and gateway `memory.search` now include
  `"mode"`. Additive; the UI reads `results` only. Dense retrieval can now add a compute/embedder call to
  explicit searches — identical to ordinary agent preparation.

### H06 (P2) — total retrieval failure was reported as a valid empty result

- **Reproduced?** Yes, reviewer diagnostic #2 (`logs/h06_repro.py` pre-fix): `ConnectionError` from the
  keyword branch → `retrieve()` returned `[]`, tool said "No memories found".
- **Fix:** `MemoryRetrievalError(MachinaError, RuntimeError)` added to the memory module (retriever.py).
  `retrieve()` tracks attempted/failed branches and raises only when every attempted branch failed; partial
  success returns the surviving branch's results, genuinely-empty stays `[]`. Callers: tool → existing
  `ToolError` wrap; API → HTTP 503 `memory retrieval unavailable: …`; gateway →
  `RpcError(OPERATION_FAILED, …)`.
- **Files:** machina/memory/retriever.py, machina/api/routes/memory.py, machina/gateway/features/memory.py
  (tools/memory.py needed no change — its existing error handler already surfaces it).
- **Tests:** `test_h06_all_branches_failing_raises_typed_error`,
  `test_h06_dense_and_sparse_both_failing_raises`, `test_h06_partial_success_kept_when_keyword_branch_fails`,
  `test_h06_partial_success_kept_when_dense_branch_fails`, `test_h06_genuinely_empty_result_stays_empty`,
  `test_h06_recall_tool_surfaces_total_failure_as_error`,
  `test_h06_api_search_surfaces_total_failure_as_503` (direct call),
  `test_h06_api_http_surfaces_total_failure_as_503` (through the ASGI app),
  `test_h06_gateway_search_surfaces_total_failure_as_rpc_error`.
- **Callouts — behavior change:** total retrieval outages now raise/propagate instead of looking like "no
  memories found" (the previously reported P2). Agents' ordinary context preparation already catches this
  class of exception and proceeds without memory (now with a logged warning instead of silence). The unowned
  CLI `machina memory-search` will surface the exception as an error rather than printing "No memories found"
  — intentional per the finding; a friendlier CLI message would be a follow-up outside this lane.

## Files changed (lane L2R only)

- machina/memory/redaction.py — H02.
- machina/memory/retriever.py — H05 helper, H06 typed error + raise logic.
- machina/tools/memory.py — H05.
- machina/api/routes/memory.py — H05 + H06.
- machina/gateway/features/memory.py — H05 + H06.
- tests/test_holistic_l2r_memory_fixes.py — new, 24 test cases (H02 9 / H05 5 / H06 10).
- tests/test_gateway_memory_usage.py — mocks updated for `prepare_query_embedding`.

## Evidence index

- `logs/L2_red_evidence.txt` — verbatim pre-fix RED output for H02/H05/H06.
- `logs/h02_repro.py` — reviewer diagnostic; pre-fix leak, post-fix equal.
- `logs/h02_matrix.py`, `logs/h02_matrix2.py`, `logs/h02_pem.py` — format matrix + PEM/gap probes.
- `logs/l2_probe.py` — robustness probes (large benign run, 100k-char token withheld then redacted,
  lead-tail flush, task-hex left-boundary).
- `logs/h06_repro.py` — reviewer diagnostic #2; pre-fix `[]`, post-fix `MemoryRetrievalError`.
- `logs/L2_focused_green.txt` — 566 focused tests green.
- `logs/L2_ruff.txt` — ruff check + format clean (owned source + tests).
- `logs/L2_full_suite_no_x.txt` — full-suite failure inventory (see callout).
- RED evidence (verbatim): H02 char-by-char `AssertionError: chunks of 1 diverged:
  '[REDACTED_OPENAI_KEY]aaaaaaaaabbbbb…'`; H05 4 failed `assert None == [0.5]`; H06 5 failed `DID NOT RAISE`.
- Reviewer source: docs/verification/proofs/holistic_review_2026-10-15/isolated_diagnostics.txt.
