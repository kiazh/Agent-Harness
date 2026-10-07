# AgentHarness runtime/feature ledger

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

Shared lazy singleton (`tools/rag.py`), caller scope on every tool, explicit `agent_id` on index. No implicit host scan — only indexed session docs. Redaction before store. `embedding_model/dims` recorded per chunk; dimension mismatch fails loudly (reindex). `IdentityReranker` labeled passthrough; Cohere when key set. Empty results never cached; mutations invalidate per-session cache. Keyword-only fallback works without second paid key.

## Compaction/eviction

Auto at safe turn boundary (`maybe_auto_compact`, ownership released): budgets stored rows, threshold-triggered, no-progress loop guard, archive-before-replace, exact-ID replacement, concurrent-write safe, paginated. Eviction (`evict_old_chunks`, `eviction_max_tokens`) is reversible archive, distinct from summarization. Real-tokenizer tests incl. CJK.

## Skills/agents/research/hooks

Catalog in prompt, bodies on demand (`skill_read`); packaged wheel (`force-include skills`) + Docker `COPY skills` + `importlib.resources` fallback. Learning/curation budgeted toggles. Delegation = same factory/broker/accounting/cancellation; child caps = definition ∩ parent (`cap_child_authority`). RL/GRPO/LoCoMo/conformance = explicit offline workflows. Plugins run in-process (trusted boundary, documented).

## Usage/observability

Primary answers via `usage_store` reservations; secondary calls (consolidation/summarize/rerank) audit-logged with model/tokens (budgets enforced on primary turns; secondary bounded single-flight). Latency/error/cache counters per stage. Redaction consistent incl. streaming tail-buffer. Status reports derived state (see `services.status_summary`).

## Permissions

Policy (`policy.py`) separate from backends (host/container). Modes: `ask` default, `workspace`, `sandbox`, `full` (explicit session grant, FULL HOST shown, sensitive caps always explicit). Normalization resolves symlinks/cwd/digests; material change revalidates; secrets never in payloads. Grants session/path/digest-scoped; no vague always-allow. Consumption atomic (DB `FOR UPDATE` + mem flag). Agent principals can never approve. Denial = structured result, no re-request loop (digest dedupe). Remote RPC cannot gain host control: `approvals.resolve` requires human principal + session binding.

## Transports

TUI: compact card (action/cwd/target/backend/caps) + diff for edits + picker (allow once/deny); cancel/revoke during wait; session-bound IDs. HTTP: `GET /api/v1/approvals`, `POST /api/v1/approvals/{id}/resolve` (auth + ownership); poll-based recovery; SSE carries needs_approval marker, resume = fresh fenced claim. Headless: pregranted only, else durable `needs_approval` pause, lease freed, fresh claim + revalidation on resume. No SQL held across waits.

## Setup/platforms

First run: detect workspace/config/providers/DB/sandbox; ask only missing essentials; auto-generate secrets `0600`; reuse DB; loopback-only pgvector + generated creds; `ah init` migrate once. No Docker needed for host modes; sandbox absence reported, never silent downgrade. One dotenv resolver honors `AH_ENV_FILE`. Config survives upgrades. Spaces/Unicode/drive letters/argv supported; no `/proc` reliance; PowerShell/cmd via argv. Stopped DB containers reused, volumes persistent, never delete user data. Sandbox mounts rw only when approved; `/tmp` writable; no docker-socket mount.

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
