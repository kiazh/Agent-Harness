# AgentHarness runtime/feature ledger

Current release evidence and open gates are in [RELEASE_READINESS.md](docs/verification/RELEASE_READINESS.md). Counts below belong to their dated engineering snapshots.

Checked matrix: implementation → entry points → dependency → trigger → config → fallback → lifecycle → proof test. Statuses: automatic (every turn), conditional (when triggered/material exists), degraded (fallback reported), optional (explicit), offline (research-only).

## Hang fixes (2026-10-07)

| Fix | Location | Change | Proof |
|---|---|---|---|
| H-01 | `ah/api/app.py::event_stream`, `ah/gateway/server.py::_run_turn` | Learning tasks bounded join (2s timeout) on all paths; leftovers cancelled | `test_gateway.py` turn tests |
| H-02 | same | Provider close bounded (5s timeout); claim release in independent outer finally | `test_gateway.py` turn tests |
| H-03 | `ah/api/app.py::event_stream` | Auto-compaction fire-and-forget (create_task); never blocks [DONE] | `test_gateway.py` turn tests |
| H-04 | `ah/services.py::compress_session` | Ownership try/finally starts immediately after claim acquisition; all reads/config/provider/compression/persistence inside ownership lifetime | `test_handoff_regressions.py::test_acompress_llm_path_is_awaited_directly` |
| H-05 | `ah/core/agent_factory.py` | RAG pipeline awaited in async factory; injected pipeline is actual service object, never coroutine | `test_gateway.py` RAG tests (no more `'coroutine' object has no attribute 'search'`) |
| H-06 | `ah/api/app.py::event_stream` | Generator closure/cancellation contract: bounded cleanup, claim release independent of provider close, no yield after cancellation | `test_gateway.py` turn tests |
| H-07 | `ah/gateway/server.py`, `ah/api/app.py` | Sanitized progress stage tracking: turn_started, building_agent, waiting_for_provider, running_tool, joining_optional_work, closing_owned_clients, releasing_ownership, compaction_maintenance, answer_complete | Debug logs in test output |

## Correctness fixes (2026-10-07)

| Fix | Location | Change | Proof |
|---|---|---|---|
| R-01 | `ah/core/compression.py::_llm_summarize` | Removed TypeError fallback that bypassed accounting; test doubles adapted to match real provider interface | `test_handoff_regressions.py::test_acompress_llm_path_is_awaited_directly` |
| R-02 | `ah/core/agent.py` | Per-tool authority preserves inherited mode caps; intersects parent tools with agent definition | `test_audit_regressions.py` tool allowlist tests |
| R-03 | `ah/core/agent.py`, `ah/memory/consolidator.py` | Consolidation watermark advances only to last successfully processed chunk, not session's newest chunk | `test_bug9_bug15_fixes.py` consolidation tests |
| R-04 | `ah/services.py::_estimate_next_request` | Next-request estimate includes mandatory system/persona instructions; measures actual message list | `test_handoff_regressions.py` compression tests |

## Production-readiness fixes (2026-10-12, handoff P1 1–13 + P2 14–18)

All with failing-first regressions in `tests/test_production_readiness_p1.py` (20 tests) and `tests/test_production_readiness_p2.py` (21 tests); each fix verified RED→GREEN by reverting the fix and observing the new test fail.

| Item | Location | Change | Proof |
|---|---|---|---|
| P1-01 | `ah/core/orchestrator.py::delegate` | `agent = None` before factory; `close_agent_provider` only when `agent is not None`; original exception propagates | `test_orchestrator_default_factory_raise_preserves_original`, `test_orchestrator_injected_factory_raise_no_close` |
| P1-02 | `ah/core/config.py::Config.get` | Empty/whitespace env values treated as absent for normal keys; `reasoning_effort=""` preserved as explicit provider-default | `test_empty_model_env_falls_back`, `test_empty_provider_env_falls_back`, `test_reasoning_effort_empty_preserved`, `test_nonempty_env_overrides` |
| P1-03 | `ah/core/config.py::Config.set` | Rejects keys not in `DEFAULTS` with `ValueError` naming the key + valid keys; no partial mutation | `test_unknown_config_key_rejected`, `test_valid_key_still_works` |
| P1-04 | `ah/services.py::maybe_auto_compact` | Outer `except` records `record_failure("services.maybe_auto_compact")`; claim-acquisition failure also recorded (degraded deferral, never unhandled) | `test_maybe_auto_compact_records_failure` |
| P1-05 | `ah/services.py::_estimate_next_request` | `definition_prompt = ""` initialized before the first fallible block; every path falls back safely | `test_estimate_survives_session_lookup_failure` |
| P1-06 | `ah/services.py::status_summary`, `ah/db/schema.sql`, `ah/core/models.py`, `ah/core/serialization.py`, `ah/core/context.py`, `ah/rag/pipeline.py` | New `context_chunks.document_source` column (stable document identity); pipeline + `add_chunk(s)_batch` persist it; status counts `COUNT(DISTINCT COALESCE(document_source, id::text))` instead of distinct payloads | `test_status_doc_count_query_uses_document_source`, `test_document_count_distinct_sources` |
| P1-07 | `ah/services.py::status_summary` | Detection-failure fallback is `autoCompaction=disabled`, `memoryExtraction=unknown`, `reranker=unknown` (never `ready`/`enabled` without evidence); base-dict config reads made failure-safe (`_safe_config_value/_bool`) | `test_status_fallback_never_ready`, `test_status_never_raises_on_partial_config_failure` |
| P1-08 | `ah/core/context.py::get_recent_context` | `ORDER BY created_at DESC, id DESC` matching all other context queries | `test_recent_context_ordering_sql` + deterministic-tiebreak coverage in existing context tests |
| P1-09 | `ah/core/compression.py::_truncate_compress` | `None` timestamps sort at the epoch floor (`0001-01-01 UTC`, matching eviction index) with `(ts, id)` deterministic tiebreak; chunks never dropped for missing timestamps | `test_truncate_compress_none_timestamps` |
| P1-10 | `ah/services.py` | New typed `CompressOutcome(status, result, error)` (`no_op/completed/stale_ownership/stale_snapshot/failed/busy`) via `compress_session_with_status`; legacy `compress_session` wrapper preserves `result/None`-or-raise behavior | `test_compress_outcome_statuses` |
| P1-11 | `ah/core/approval_resume.py::resume_action_stream` | `.get("result_preview", "")` with `"continuation ended"` fallback; no `KeyError` | `test_resume_action_stream_missing_preview` |
| P1-12 | `ah/connectors/mcp_client.py::_retryable_connect_error` | `httpx2` import guarded; missing optional dep returns `False` (stdlib classification only), never masks the original error with `ImportError` | `test_retryable_does_not_mask_without_httpx2` |
| P1-13 | `ah/connectors/service.py::tools_list` | Structured `{"tools": [...], "truncated": bool}`; no non-tool marker inside the array; existing service tests updated | `test_tools_list_structured` + updated `tests/test_connectors_service.py` |
| P2-14 | `ah/core/context.py::add_chunks_batch`, `ah/rag/pipeline.py::index_document` | Batch `text[]::vector[]` cast verified against real PostgreSQL 16.15 + pgvector: embeddings, all-`None`, 10+10 concurrent batch+single, dimension-mismatch fails loudly, `document_source` round-trips | `test_batch_vector_insert_with_embeddings`, `test_batch_insert_all_none_embeddings`, `test_batch_insert_concurrent_with_single_inserts`, `test_unsupported_vector_dimension_fails_loudly`, `test_batch_insert_preserves_document_source` |
| P2-15 | `ah/core/runtime.py::startup/refresh` | Provider/sandbox/toggle detection runs via `asyncio.to_thread`; blocking docker/socket probes never stall the loop; timeouts still degrade | `test_startup_and_refresh_run_detection_off_loop`, `test_detection_runs_while_a_slow_probe_blocks_the_thread`, `test_probe_timeout_and_missing_docker_degrade`, `test_ollama_probe_timeout_reports_unavailable` |
| P2-16 | `ah/core/scheduler.py::JobStore.finish` | Mock/production paths share `_finish_status/_finish_next_run/_finish_fencing_ok`; token fencing, paused park, cron reschedule, error outcomes identical | `test_scheduler_claimed_finish_wrong_and_missing_token`, `test_scheduler_correct_token_finish`, `test_scheduler_paused_approval_finish`, `test_scheduler_cron_reschedule`, `test_scheduler_error_finish_records_error` |
| P2-17 | `ah/core/job_scripts.py::_execute_script_command` | Windows `stdin.write/drain/close` transport errors caught as bounded `RuntimeError("script startup failed: …")` with process cleanup; no unhandled `BrokenPipeError` | `test_broken_pipe_is_startup_failure`, `test_broken_pipe_on_drain_and_close` |
| P2-18 | `ah/core/metrics.py::JsonFormatter` | Extras restricted to an allowlist (operation/IDs/counts/error class); values bounded (500 chars), secret-redacted, numerics preserved; prompts/completions/args/results/credentials/raw errors dropped | `test_json_formatter_drops_sensitive_extras`, `test_json_formatter_keeps_bounded_diagnostics`, `test_json_formatter_bounds_and_redacts_allowed_values`, `test_json_formatter_preserves_standard_fields` |

## Debug pass (2026-10-12, user-requested full look-through)

| Item | Location | Change | Proof |
|---|---|---|---|
| D-01 | `ah/core/config.py`, `ah/db/lazy.py` (new), `ah/core/usage.py`, `ah/core/context.py`, `ah/memory/{approval,identity,persona,retriever,store,user_profile,growth}.py` | Import-cycle fix: first-`ah.db.connection` import order ran `config.load` while `connection` was partial → `ImportError` → the **entire config file was silently discarded** (wrong model/provider/mode). Nine cycle members now bind `db`/`parse_command_count` via the import-safe `ah.db.lazy` proxy (single `parse_command_count` impl moved there, re-exported); `Config.load` isolates per key so one unreadable key can never nuke the file | `tests/test_config_import_cycle.py` (5 tests; 3 verified RED on old code, 2 lock-ins), subprocess-tested in db/memory/gateway-first orders |
| D-02 | `ah/core/agent.py::_turn_model` | Precedence was provider→session (wrong window on divergence); now session→provider→config per the existing spec test (failed on clean HEAD) | `tests/test_context_windows.py::test_effective_turn_budget_prefers_session_then_provider_then_config` |
| D-03 | `ah/permissions/store.py::_db`, `_grant_live` | DB-helper failure now `record_failure`s (memory fallback is explicit via `is_durable`); present-but-unparseable `expires_at` fails closed, matching `policy._grant_usable`; NULL stays live | `tests/test_debug_permissions_hardening.py` (6 tests; 3 verified RED on old code) |
| D-04 | `tests/test_code_review_fixes.py::TestCircularImport` | Old test asserted top-level `from ah.db.connection import db` (the D-01 bug); now asserts lazy binding + no top-level connection import | updated test green |
| D-05 | `OPERATIONS.md` | Documented: gateway does not self-migrate; code+schema deploys must run `ah init` before starting workers (this exact gap caused a live-turn `UndefinedColumnError` outage during the P1 series; prod DB migrated manually and smoke-tested) | gateway log + smoke test |

## Runtime

| Feature | Implementation | Entry points | Dependency | Trigger | Config | Fallback | Lifecycle | Proof |
|---|---|---|---|---|---|---|---|---|
| Service graph | `ah/core/runtime.py::RuntimeServices` | CLI/TUI/HTTP/jobs/delegation via `startup()` | DB optional | process start | — | degraded capabilities, never crash | `shutdown()` closes audit+DB, cancels tracked tasks | `test_runtime_startup_never_raises` |
| Agent factory | `ah/core/agent_factory.py::build_agent_for_session` | chat/gateway `_run_turn`/REST/jobs/delegation | provider key for chat | every turn | `model/provider/agent_id/max_iterations` + definition overrides | raises provider error (chat only) | owned provider closed exactly once | `test_researcher_factory_blocks_write_tools` |
| Turn coordinator | `ah/core/turns.py::SessionTurnCoordinator` + fns | gateway submit/REST claim/scheduler/compress/delete | DB `running` claim or in-process | every turn/mutation | — | in-process-only (documented) | `end_turn` idempotent | existing 409 tests |
| Approval-aware ownership | `TurnOwnership.wait_for_approval` | broker handler (gateway TUI) | none | permission wait | `approval_timeout` (compute timeout paused) | headless → structured pause | cancel stops waits, frees lease | `test_approval_timeout_distinct` |

## Context/history

Active context (`context_chunks`), archive (`context_archive`), summaries (`compression_summary`), documents (`chunk_type=document`). Cancellation/failure/denial/disconnect flush history + interruption marker (`_flush_pending_cancellation_safe`). Stable ordering: `created_at,id` tiebreak + fork preserves timestamps. Exports state limits (history `limit=200`, context `50–500`, export full but redacted).

## Memory

`memory_enabled` gates auto-retrieval; explicit `remember/recall` tools stay available when disabled. Query embeddings via `ah/memory/embeddings.py` (dense+sparse when key, keyword-only + honest status otherwise). Dedup/importance/persona/redaction/quarantine/provenance/approval unchanged. Agent-scoped retrieval. Consolidation watermark `mem_consolidated_up_to` → idempotent; bounded single-flight; gated by `memory_consolidation_enabled`. Forgetting = retrieval weight (`_merge_results` × strength), not scheduled deletion.

## RAG/embeddings

Shared lazy singleton (`tools/rag.py`), caller scope on every tool, explicit `agent_id` on index. No implicit host scan — only indexed session docs. Redaction before store (reserved embedding fields win over caller metadata). `embedding_model/dims` recorded per chunk; dimension AND model identity enforced on search (cross-model comparison fails explicitly with migration guidance; mixed keyword-only rows unaffected). `IdentityReranker` labeled passthrough; Cohere selected only with a working key (identity-derived status). Empty results never cached; mutations invalidate per-session cache. Keyword-only pipeline constructs without keys (NULL embeddings, honest mode) — zero-vector fabrication never used. Document-only retrieval scope enforced in dense/sparse/fallback/rerank/cache paths; conversation recall stays a separate API. Compression and eviction never touch document chunks (separate lifecycle).

## Compaction/eviction

Auto at safe turn boundary (`maybe_auto_compact`, ownership released): budgets stored rows, threshold-triggered, no-progress loop guard, archive-before-replace, exact-ID replacement, concurrent-write safe, paginated. Replacement validates the live mutation token AND the exact captured ID set inside one transaction (stale ownership/snapshot abort with rollback). Eviction (`evict_old_chunks`, `eviction_max_tokens`) is reversible archive, distinct from summarization, and never selects documents. Session state merges transactionally per-field (`update_state_fields`); execution-critical reads bypass the display cache (`get_fresh`). Real-tokenizer tests incl. CJK.

## Skills/agents/research/hooks

Catalog in prompt, bodies on demand (`skill_read`); packaged wheel (`force-include skills`) + Docker `COPY skills` + `importlib.resources` fallback. Learning/curation budgeted toggles. Delegation = same factory/broker/accounting/cancellation; child caps = definition ∩ parent (`cap_child_authority`). RL/GRPO/LoCoMo/conformance = explicit offline workflows. Plugins run in-process (trusted boundary, documented).

## Usage/observability

Primary answers via `usage_store` reservations; secondary calls (consolidation/summarize/rerank) audit-logged with model/tokens (budgets enforced on primary turns; secondary bounded single-flight). Context estimates are documented conservative approximations over the session's effective tool allowlist + definition prompt (never exact request accounting). Consolidation returns typed `ConsolidationResult` (complete/empty/partial/failed) and the durable watermark advances only from fully-processed contiguous checkpoints; extraction failures and partial writes stay retryable (idempotent via content dedup). Extracted memories get redaction-before-embedding ingestion through the shared query embedder (same enforced space; keyword-only preserved without keys) plus an idempotent backfill queue. Latency/error/cache counters per stage. Redaction consistent incl. streaming tail-buffer and RAG metadata (reserved embedding fields win over caller metadata). Status reports derived state (see `services.status_summary`): reranker identity comes from the instantiated shared component (Cohere selected only with a working key; rotation rebuilds the pipeline), never key presence alone.

## Permissions

Policy (`policy.py`) separate from backends (host/container). Modes: `ask` default, `workspace`, `sandbox`, `full` (explicit session grant, FULL HOST shown, sensitive caps always explicit). Session activation (`scope=session`, default) never mutates the global default — future sessions unaffected; only explicit `scope=global` changes the persistent default newly created sessions inherit (`ah/core/session_mode.py`; `tests/test_audit_remediation.py::test_001_*`). Sensitive capabilities match by exact identity and require coverage of EVERY required cap by live digest-bound grants — one grant never authorizes others; substring matching never used. Digests use versioned length-prefixed canonical encoding (`DIGEST_VERSION=v1`) binding operation/tool/backend/mode/scope/argv/content identity/destinations/timeout/sorted capabilities; legacy digests fail closed (fresh approval). Normalization resolves against the shared workspace root (same identity at proposal and execution); material change revalidates; backends consume the broker-stamped immutable execution context, never re-resolved globals (no silent sandbox→host switch). Secrets never in payloads; file-write cards show argv/timeout plus redacted content preview + diff (content preview only where content is the proposal). Durable approvals persist the versioned canonical proposal + sanitized display (`proposal`/`display` columns); legacy rows without proposals never resume — fresh approval instead. Grants session/path/digest-scoped; no vague always-allow. Consumption atomic (DB `FOR UPDATE` + mem flag). Agent principals can never approve. Denial = structured result, no re-request loop (digest dedupe). Remote RPC cannot gain host control: `approvals.resolve` requires human principal + session binding.

## Transports

TUI: compact card (exact argv — never a bare shell string — plus cwd/target/backend/timeout/network/content digest + redacted preview/diff for edits) + picker (allow once/deny); cancel/revoke during wait; session-bound IDs; `message.stopping` keeps the UI blocked during cleanup (terminal `message.complete` means ownership released, exactly once). HTTP: `GET /api/v1/approvals`, `POST /api/v1/approvals/{id}/resolve` (auth + ownership); poll-based recovery; typed `needs_approval` SSE events with request/session binding + sanitized proposal (distinct from failure), resume = fresh fenced claim. Headless: pregranted only, else durable `needs_approval` pause (typed `AgentResponse.needs_approval` — never prose matching), lease freed, fresh claim + revalidation on resume. Jobs park in durable `paused_approval` state (excluded from due claims) and resume only via explicit `jobs.resume`. No SQL held across waits. Compute and approval deadlines tracked separately (human waits pause compute accounting; approval timeout stays broker-bounded).

## Setup/platforms

First run: detect workspace/config/providers/DB/sandbox; ask only missing essentials; auto-generate secrets `0600`; reuse DB; loopback-only pgvector + generated creds; `ah init` migrate once. Installer never resets an existing Postgres role password (dedicated `agentharness` role provisioned once with generated creds; pre-existing roles preserved) and reads API keys with terminal echo disabled (restored on all exits). No Docker needed for host modes; sandbox absence reported, never silent downgrade. One dotenv resolver honors `AH_ENV_FILE`. Config values are typed + bounds-validated (`validate_value` shared by Config/gateway/CLI/env/file paths) — invalid values rejected without partial state/writes. Config survives upgrades. Spaces/Unicode/drive letters/argv supported; no `/proc` reliance; PowerShell/cmd via argv. Stopped DB containers reused, volumes persistent, never delete user data. Compose mounts an explicit writable app-home volume (`ahhome`, non-root owned) while code/root stay read-only, and forwards provider selection + per-provider keys explicitly (no secrets in image layers). Sandbox mounts rw only when approved; `/tmp` writable; no docker-socket mount. Pinned fetch keeps per-hop SSRF pinning with aggregate header byte/count caps and an overall wall-clock deadline.

## Status

`ah status` + gateway `status` derive: mode/backend/workspace, chat, memory retrieval mode+reason, extraction, RAG doc count, reranker label, compaction, sandbox, jobs awaiting approval, research=offline.

## Entry-point matrix (addendum §6)

Same shared factory/coordinator/broker/scope/ownership on every path. Evidence = code + test IDs.

| Feature | CLI (`ah chat`) | TUI/gateway | HTTP | Jobs | Delegation | Disabled/fallback proof |
|---|---|---|---|---|---|---|
| Agent factory/definition/authority | `cli/__init__.py::_chat` → `build_agent_for_session` | `server.py::_run_turn` → same factory (fail-closed, no weak fallback) | `app.py::prompt_session` → same factory | `scheduler.py::_build_agent` + orchestrator `_default_agent` + `cap_child_authority` | same factory; child tools = definition ∩ parent | unknown specialist → `AgentDefinitionError` (`test_unknown_specialist_is_not_general_agent`) |
| Turn/mutation ownership | n/a (single turn) | `_prompt_submit` token claim; cancel releases; `_run_turn` finally releases | pre-header `try_begin_turn`, SSE finally `end_turn` | `_execute` claims session turn; busy → reschedule error | child sessions claim independently; parent recording claim-free | crash → expiry reclaim (`test_crash_expiry_recovery_and_stale_release`); stale release rejected |
| Memory retrieval/extraction | factory injects retriever/consolidator; `memory_enabled` gate; watermark | same (shared factory) | same (shared factory) | jobs run `agent.run` (same `_prepare_context`) | child agents via factory | disabled → skipped; keyword-only reported (`embedding_status`) |
| Embeddings + fallback | `embed_query` (None → keyword) | same | same | same | same | `test_embedding_fallback_reported` |
| RAG + scope | shared singleton; scope vars in agent loop | same | same + explicit `agent_id` | same | same | disabled → `[]`; empty index → `empty-index` |
| Compaction/archive | `maybe_auto_compact` (CLI single-shot n/a) | post-turn auto + `/compress` (mutation claim) | post-stream auto + RPC compress | n/a (jobs are short) | n/a | disabled → skipped; no-progress guard; keyset snapshot + exact-ID replace |
| Broker + modes | `--mode`/`ah mode`; headless → structured pause | `/mode`, approval card, `/approvals` | approval endpoints + ownership checks | pause as `needs_approval`, fresh claim resume | capped authority; agent principals never approve | sandbox absent → explicit refusal, never silent host |
| Usage/lifecycle/cancel | `close_agent_provider` in finally | join learning tasks, then close owned | same + auto-compact post-release | owned close + turn release; loss cancels task | deadline/hop inheritance; cancel propagates | shared/injected never closed (`test_shared_provider_not_closed_by_non_owner`) |

## Production completion changes (2026-10-09)

Release status is tracked in [the completion ledger](docs/plans/PRODUCTION_COMPLETION_PROGRESS.md).
The implementation evidence below does not assert that an external deployment,
alert receiver, or production backup/restore has passed.

| Boundary | Current behavior | Proof |
|---|---|---|
| Mode | Durable per-session mode; atomic grant/mode transitions; fresh worker reads | `test_session_mode_persistence.py`, `test_multi_worker_fencing.py` |
| HTTP approval | Exact original tool resume, fresh fenced turn, one-time consumption | `test_http_approval_resume.py` |
| Extraction | Per-hop pinned connection, private IP rejection, aggregate deadline/size bounds | `test_web_extract_pinned_fetch.py`, `test_deep_fetch_deadline.py` |
| Ownership | Failed renewal cancels effects; failed DB release cannot emit terminal success | `test_rest_ownership_loss.py`, `test_claim_failure_boundary.py` |
| Lifecycle | Resistant work stays tracked and owned until retirement; cleanup failures visible | `test_turn_completion_lifecycle.py`, `test_provider_close_visibility.py` |
| Wait budget | Active/overlapping approvals pause compute time while ownership renews | `test_approval_budget_active_wait.py`, `test_approval_wait_renewal.py` |
| Scripts | Administrator allowlist, reviewed entry snapshot, declared dependency revalidation | `test_script_dependency_boundary.py` |
| Protocol | Single schema/generated TUI types; negotiated compatible v1/v2 | `test_protocol_conformance.py` |
| Config | Explicit precedence, mounted/backend resolution, persist-before-activate rotation | `test_configuration_precedence.py` |
| Diagnostics | Audit omits raw conversation/tool payload; exact known-secret and pattern redaction | `test_observability_payload_boundary.py` |
| Migration | UTF-8 atomic schema application, repeated fresh/upgrade checks, SQL compilation | `test_schema_conformance.py` |
| Smoke | Actual PostgreSQL session/job lifecycle, authenticated metrics, SSE ownership/provider cleanup | `test_production_smoke.py` |

See [the threat model](SECURITY.md) and [the deployment/recovery runbook](OPERATIONS.md)
for enforced guarantees, operator requirements, and explicit limitations.

## Finish-off (2026-10-10, post-merge `6b5ccaa`)

Code-level leftovers closed on top of the production-completion merge; the
v1.0.0 gate stays open until DB/Docker/staging verification per
`docs/plans/PRODUCTION_COMPLETION_PROGRESS.md` tasks 12/15/16.

| Boundary | Change | Proof |
|---|---|---|
| RAG metadata | `_redact_metadata` recurses via the shared redactor (nested dicts/lists/tuples/sets/bytes), matching its documented claim | `test_audit_remediation.py::test_012_metadata_redacted_and_reserved_fields_win`, `npm --prefix ui` n/a |
| SSE redaction | HTTP `_redact_value` handles `set`/`bytes` in addition to str/dict/list/tuple | `test_observability_payload_boundary.py` |
| Audit sanitizer | `_sanitize_value` handles `bytes`/`set`, fixes `sk-or-` class to include `-_`, adds GitHub/Slack/JWT patterns | `test_observability_payload_boundary.py` |
| Profiles | `user_profile` redacts display/user IDs, preferences (recursive), topics, and in-memory setters before persistence | code + `test_observability_payload_boundary.py` pattern |
| Gateway approval | `approvals.resolve` accepts `cancelled`/`expired` (parity with HTTP) and optionally binds `sessionId` | `test_http_approval_resume.py` pattern, `test_approvals_ownership.py` |
| Gateway completion | `message.complete` carries `needsApproval` summary when the turn ends paused (parity with HTTP `done`) | protocol schema `needsApproval?`, UI `transcript` reconciliation |
| TUI contract | `transcript.apply` explicitly handles `turn.started`, `needs_approval`/`permission.required`, `approval.resolved`, `approval.resumed`, `turn.ownership_lost`, `turn.cleanup_pending`; `app.onEvent` notices `approval.resumed`/`turn.started` | `npm --prefix ui test` (87 passed, 3 DB-skipped), `typecheck`, `lint` |
