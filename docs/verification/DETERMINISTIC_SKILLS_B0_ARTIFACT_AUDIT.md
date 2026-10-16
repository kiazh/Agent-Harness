# B0 — Artifact Contract and Recovery: Audit of Existing Durable-Artifact Infrastructure

- **Task:** B0-audit — verify whether existing durable-artifact infrastructure satisfies "B0 — Artifact contract and recovery" (spec §14.2, §14.6, §17-B0, acceptance at §17-B0; constraints C06–C09 at §18).
- **Date:** 2026-10-16 (America/New_York).
- **Method:** read-only source inspection (verification level 1–2) against the working tree; one targeted test snapshot (level 2–3, DB-gated parts unexecuted). No source file was modified; this document is the only file created.
- **Spec source:** `/Users/shadow/Desktop/Machina_Revised_Deterministic_Skills_and_Rolling_Context_Plan.md` (not in-repo).

## 1. Baseline (moving — repo owner active)

| Item | Value |
| --- | --- |
| HEAD commit | `b7bd6b611e5323cf0d1db0ea3e1a9f6ae1acbb2a` |
| Worktree state | dirty; modified files grew 79 → 102 → 103 during the audit window |
| `git diff` shasum (whole tree) | first seen `62c9ffb7ca20d3cadad895e2ec5c49a1c0ca86eb`; mid-audit `1bfb6f84b980f18980c2305b78d41686ec6acddf`; at write `1565b4c18e83c74e700d9c61f526e48628cbb34d` |
| `git diff --cached` | empty (`da39a3ee…`), no staged changes |
| Hot file | `machina/core/agent.py` modified (`git diff -- agent.py` shasum `32548cc683831f59f290b7d400b30f6966b4239f`, mtime 2026-10-16T03:08:29); line citations below are as-of this read — the file is owned by an active editor |

In-flight files audited (untracked = `??`, i.e. "claimed mechanisms", verified against code; all blob hashes stable throughout the read window):

| File | git state | sha1 |
| --- | --- | --- |
| `machina/core/artifacts.py` | `??` | `a02390fba03c43332d1fee4476d01a50d1dd5643` |
| `machina/tools/artifacts.py` | `??` | `9984c5cacdccb7b7bbbc7b478b0938d4e68fea77` |
| `machina/core/reducers.py` | `??` | `666a3564b1f7deb0e5b9a7434b3f1221f6ccde12` |
| `machina/core/tool_views.py` | `??` | `8c24f3fbb712d6b2acdb209751cd2cb29da52095` |
| `machina/core/projection.py` | `??` | `76561030722e0dbfe1ee2f70ff96c2982490a4b5` |
| `machina/core/agent.py` | `M` | `e7daab4ed51dc1db79b7b7278d666b1aa9c1e008` |
| supporting (read for citations) | `M` | prompt `8fd7084…`, assembler `e6ab5e9…`, compression `2460032…`, schema.sql `be46feb…`, connection.py `f8f2d48…`, usage.py `6c3a1c1…`, context_windows.py `8c76ba6…`, anthropic.py `6a3442f…` |

## 2. Checklist table

| # | Item | Verdict | Evidence (file:line, as-of-read) | One-line gap |
| --- | --- | --- | --- | --- |
| 1 | Persist-before-truncate | **VERIFIED** (with caveats) | producer `registry.execute` → `item["result"]` `agent.py:1067,1052,1144`; `bounded_tool_view` called before any recording `agent.py:1303` vs `1315`; artifact store before reducer publish `tool_views.py:103-111` → `117-124`; chunk copy/preview after `agent.py:1377-1379`; oversize store refuses rather than storing truncated `artifacts.py:90-96` | Upstream tool caps (e.g. terminal 64 KiB, `tools/terminal.py:20,198-249`) happen before anything here sees the bytes — recovery claims cover the result string, not the true upstream output (see item 2) |
| 2 | Completeness metadata | **MISSING** | no `complete`/`upstream_truncated`/`stored_truncated` anywhere (rg: 0 hits in `machina/` and tests); `status` is an outcome label `agent.py:1308`, `artifacts.py:121`; `content_bytes`/`token_estimate` set `artifacts.py:133-134`; `result_truncated` exists only for the 65k chunk and is read only by session export `agent.py:1379`, `services.py:110-111`; `source_version` plumbed `artifacts.py:75,121,161,217` but never passed by the live caller `tool_views.py:103-109` | No way to distinguish a complete original from an upstream-truncated one; disclosure says "exact original" (`reducers.py:100-101`, `artifacts.py:49-54`) even when the tool already omitted bytes (C07) |
| 3 | Durable identity + versioning | **PARTIAL** | opaque UUID id, immutable content (only `access_count` updates) `artifacts.py:248-249`; session cascade `db/schema.sql:464`; offset paging with `total_chars`/`truncated` `artifacts.py:263-272`; handle persisted into chunk payload `agent.py:1384-1385` | Source-version binding is inert (no caller sets it); no cursor type binding filters/order/scope/retention (C09); stored `artifact_handle` is never rendered back into prompts — `assembler.py:270-276` renders prior `tool_call` chunks as `[tool_call] tool(args)` only, so cross-turn handle propagation is missing (session-wide `artifact_search` is the only cross-turn path, capped at 50) |
| 4 | Recovery ops (describe/search/read) | **PARTIAL** | bounded ops `artifacts.py:191-357` (read ≤20 000 chars `34,242-243`; search query ≤200, limit 1–20 `292-296`); re-checked per-call ACL `_authorized` `152-179`; search scoped to caller ancestry `302-312`; denial = `ArtifactError(code="denied")` `206-208`; expiry/quota `111,118-124,309,359-389`; unavailable codes `199-205,255-257,313-317`; tools `tools/artifacts.py:39-137` | Session-wide search scans only the 50 newest artifacts (`artifacts.py:310`) and returns `[]` with no disclosure → `artifact_search` prints "No artifact matches." (`tools/artifacts.py:95-96`), an empty-implies-omitted path; no cursor object exists |
| 5 | Mutation safety | **VERIFIED** | recovery = read/search/describe only; effects declared `context.read` `tools/artifacts.py:140-145`; no tool-execution path in `artifacts.py`; agent path never calls reducers without a stored handle `tool_views.py:112-121` | None for the recovery surface. (Standalone reducer callers without a handle get "re-run the tool" `reducers.py:104` — unreachable from the agent recovery path but worth fencing when the file is touched) |
| 6 | Quotas / lifetimes / store-failure | **PARTIAL** | hard per-artifact cap refuse (default 2 000 000 chars) `artifacts.py:90-96`, `config.py:277`; per-session quota with advisory lock (default 500) `artifacts.py:103-117`, `config.py:275`; retention (default 30 d) + bounded purge `artifacts.py:118-124,359-389`, `config.py:272`; additive DDL for existing DBs `db/connection.py:46-67`, `db/schema.sql:462-480`; store failure → `None` → legacy blind cap, no handle claimed `tool_views.py:112-116` | Strict C06 reading: a failed/refused store still publishes a silent blind-truncated result (pre-existing behavior, no recovery claim, no disclosure). New lossy-attributed views are correctly blocked. Also: results > max_chars or store-disabled get no artifact path at all |
| 7 | At-rest / sensitive originals | **PARTIAL** | policy documented `SECURITY.md:19`; content stored verbatim in Postgres `db/schema.sql:470`; redaction only at export/UI/log fences `services.py:66,107`, `gateway/sessions.py:160` — same posture as existing context chunks (`agent.py:1378`) | No redaction/fencing at artifact store or read; widens plaintext retention from 65 KB to 2 MB per result for 30 d with no per-artifact classification or delete/redact op |
| 8 | Accounting seam inventory (B1/B2) | **DOCUMENTED** | see §5 | No projector module exists; `machina/core/projection.py` is *evidence* dedup/budget (T5), not the B1 request projector — do not conflate |

## 3. Gap list (ordered by dependency — B1/B2 blockers first)

1. **G1 — Upstream-truncation honesty (blocks all recovery claims).** Tools truncate before the agent sees results (terminal 64 KiB `tools/terminal.py:20,198-249`; repomap `tools/repomap.py:27`; rag passages `tools/rag.py:396-403`), and no `upstream_truncated` flag exists. The reducer/artifact disclosure claims the exact original is recoverable. Fix at the store contract (`artifacts.py`) + call site (`tool_views.py`) + schema. Needed first: B1 "verified recoverable views" is unsound while a view can overstate coverage.
2. **G2 — Storage-failure fallback is silent.** `tool_views.py:112-116` happens after store refusal/failure; result is a blind cap with no "unrecoverable" marker. Strict C06 acceptance ("storage failure blocks lossy publication") is only partially met: lossy *attributed* publication is blocked; silent truncation remains. Decide: keep legacy cap but mark it, or block. B1 uses this seam.
3. **G3 — Content revision binding inert.** `source_version` never populated (`tool_views.py:103-109`); B3 restart/revision checks and C09 need it. Also define what a "cursor" is for search (currently stateless newest-first). 
4. **G4 — Cross-turn handle propagation.** Chunk payload stores `artifact_handle` (`agent.py:1384-1385`) but no reader exists; `assembler.py:270-276` drops result + handle from "Recent Activity". Cross-page expansion is only within `artifact_read` and via capped `artifact_search`. Also disclose the 50-artifact scan bound (`artifacts.py:310`).
5. **G5 — Empty-vs-unavailable edge in search.** `[]` can mean "no match in the 50 newest" (`artifacts.py:302-318`); tool text implies a definitive absence (`tools/artifacts.py:95-96`). Add cap disclosure and/or raise as `unavailable` when the scan bound is hit.
6. **G6 — At-rest policy for sensitive originals.** Decide redaction-on-store, opt-out kinds, or an explicit operator-scoped retention story beyond `SECURITY.md:19`; keep consistent with existing chunk fencing.
7. **G7 — Runtime acceptance evidence (B0 acceptance).** Cross-session denial, quota, expiry/purge, and tools-paging tests are DB-gated and were **skipped** in this environment (§4). B0 acceptance cannot be closed on source inspection alone (§19 level 1 ≠ level 4).

## 4. `te_*`/artifact test snapshot — CURRENT OBSERVED (not suite truth; §19)

- Command: `PYTHON=<venv> ./run-tests.sh tests/test_te_artifacts.py tests/test_te_projection.py tests/test_te_reducers.py tests/test_te_review_fixes.py tests/test_te_usage_ledger.py tests/test_te_views_integration.py tests/test_te_prompt_policy.py tests/test_te_cache_profiles.py -q -rs -p no:cacheprovider` from `/Users/shadow/Machina-DevTools`, `MACHINA_REPO=/Users/shadow/machina`, interpreter `/Users/shadow/machina/.venv/bin/python` (pytest 9.1.1), `PYTHONDONTWRITEBYTECODE=1`, temp HOME per `run-tests.sh`.
- Result snapshot at 2026-10-16 ~14:22: **89 collected, 77 passed, 12 skipped, 0 failed (0.88 s)**.
- Per-file collected: artifacts 9, cache_profiles 9, projection 7, prompt_policy 9, reducers 15, review_fixes 13, usage_ledger 22, views_integration 5.
- All 12 skips = `MACHINA_TEST_DATABASE_URL not set` — 8 in `test_te_artifacts.py` (incl. `test_access_follows_delegation_tree_not_siblings:126`, quota, expiry/purge, tools paging, store guards) and 4 in `test_te_usage_ledger.py`.
- The passing set includes the store-failure fallback (`test_te_views_integration.py:199`, mocked store) and the non-DB artifact contract tests. Counts are a point-in-time snapshot of a dirty tree; they are not a claim about the suite.

## 5. Accounting seam inventory for B1/B2 (document only, no edits)

- **Effective provider/model capacity:** `prompt.py:130 effective_turn_budget()` (alias `_effective_turn_budget` `:142`), resolved per turn in `_prepare_context` via provider override `agent.py:572-575` or fallback `:577`; provider base `core/provider/base.py:16-19`; OpenRouter override `core/provider/openrouter.py:65-68`; lookup `core/context_windows.py:76` + `openrouter_context_window:119`; ChatGPT catalog `core/provider/catalog.py:468 window_for`. Consumed at `agent.py:1843` (run) and `agent.py:2060` (run_stream).
- **Request counting (local basis):** `prompt.py:145 request_context_tokens()` (alias `:167`): tiktoken over JSON messages + `provider.transport._tools_payload` (`transport.py:246`) + 16 + 8·len(messages) + output reservation (`reservation_max_tokens` hook `prompt.py:154`) + visual tokens. Used in the pre-call guard `agent.py:1844-1845` / `2061-2062`.
- **Usage reservation / request accounting:** `_reserve_usage` `agent.py:494-521` → `usage_store.reserve` `usage.py:160-254` (estimate `_estimate_tokens` `usage.py:49-61`; per-request counts from `COUNT(usage.id)` over session/agent ancestry scopes `usage.py:198-216`); limits `usage.py:36-46`; completion `usage_store.complete_call` `usage.py:79`.
- **Two provider-call sites (where a projector must slot in):**
  - non-streaming: `_call_llm_with_retry` `agent.py:359-415`, provider call at `usage_store.complete_call` `:380`; invoked from `run()` at `:1868`.
  - streaming: `_stream_llm_with_retry` `agent.py:417-492`, provider call at `self.provider.stream_complete` `:445`; invoked from `run_stream()` at `:2096`.
  - Slot: after `_condense_history` (`agent.py:1841` / `2058`; impl `prompt.py:202`) and before the capacity guard `:1843-1845` / `:2060-2062`, so the projected request is what is counted and sent. No projector exists today; `core/projection.py` is unrelated (T5 evidence dedup/budget).
- **Accepted-completion attribution:** `tool_views.py:133 mark_turn_accepted` → `usage.py:397 mark_accepted`; called `agent.py:1939` (run) and `agent.py:2205` (run_stream).
- **Provider-specific signed/native requirements a projector must preserve:** assistant native blocks copied onto messages `prompt.py:85-90,188-191`; Anthropic must replay signed `thinking`/`redacted_thinking` verbatim `core/provider/anthropic.py:111-121`; tool_result blocks are merged into user messages and must stay ordered/pairwise `anthropic.py:95-109`; thinking budget payload `anthropic.py:172-178,205-250`; OpenAI-style tool replies must stay in provider call order (`agent.py:1234-1238`). Prompt-cache breakpoints are applied at wire time by `provider/prompt_cache.py` (transport import `transport.py:262-273`), so projecting before the provider call also precedes cache marking.

## 6. Unexecuted checks (explicit)

1. Cross-session recovery denial **at runtime** (DB-gated, `test_te_artifacts.py:126` skipped; source path verified only).
2. Quota/expiry/purge and artifact tools paging at runtime (same 8 skipped tests).
3. >50-artifact search empty-result behavior (source-analysed, not exercised).
4. Real-DB storage-failure fallback (only the mocked-store path passed).
5. Oversize (>2 000 000 chars) end-to-end turn through the refusal path.
6. Provider-native block survival through a (future) projector — B1/B2 not implemented; nothing to test yet.
7. At-rest encryption/redaction posture beyond doc inspection (`SECURITY.md:19`).
8. Byte-exact comparison of artifact content vs tool-execution result across the real execution path (source-verified ordering only).

## 7. Exact next actions for B0 completion

1. `machina/core/artifacts.py` — add completeness metadata (`complete`, `upstream_truncated`, `stored_complete`, counts, revision) to the store contract; validate revision input; optionally a cursor (id+revision+offset window) for search.
2. `machina/db/schema.sql` **and** `machina/db/connection.py` (idempotent `_TOOL_ARTIFACTS_MIGRATION`) — additive columns, kept in sync.
3. `machina/core/tool_views.py` — record known upstream-truncation markers (terminal/repomap/rag) at store time; mark the blind-cap fallback as unrecoverable (or block per C06 decision).
4. `machina/core/agent.py` (hot, coordinate with owner) — pass `source_version` and any tool-reported completeness into the store call; keep store-before-record order.
5. `machina/tools/artifacts.py` — surface completeness/revision in `artifact_describe`; disclose the search scan bound; distinct text for empty vs unavailable.
6. `machina/core/assembler.py` — render stored `artifact_handle`/recovery pointer for prior `tool_call` chunks (cross-turn expansion) without bloating the recent window.
7. `Machina-DevTools/tests/test_te_artifacts.py`, `test_te_views_integration.py` — extend for flags/cursors/fallback markers; run the DB suite in CI so B0 acceptance evidence is level 4, not level 1.

## 8. Handoff block (spec §20)

```text
Task ID/status: B0-audit — COMPLETE (source inspection) / runtime acceptance PARTIAL (DB-gated tests skipped)
Baseline commit and dirty paths: HEAD b7bd6b6; worktree dirty and moving (diff shasum 62c9ffb7 → 1565b4c1 during audit); in-flight: core/artifacts.py, tools/artifacts.py, core/reducers.py, core/tool_views.py, core/projection.py (all untracked), core/agent.py (M)
Requirement IDs implemented: B0 partially (durable store, ACL, quotas, recovery tools, store-before-lossy-publish); C06 partial, C07 gap, C08 met, C09 partial
Exact source/API/schema changes: none by this audit (read-only); this document added at docs/verification/DETERMINISTIC_SKILLS_B0_ARTIFACT_AUDIT.md
Existing abstractions reused: sessions ancestry (delegation_schema.sql), LazyDb, config keys, registry tool effects, reducer disclosure idiom
Backward compatibility/migration: additive DDL in db/schema.sql + db/connection.py runtime migration; feature flags artifacts_enabled/reducers_enabled
Red behavioral observation: none executed; DB-backed acceptance (cross-session denial, quota, expiry) not runnable here (MACHINA_TEST_DATABASE_URL unset)
Green commands, runtime, exit codes, pass/fail/skip counts: te_* snapshot 89 collected / 77 passed / 12 skipped / 0 failed, 0.88 s, pytest 9.1.1 (see §4; snapshot, not suite truth)
Real consumers covered: agent run/run_stream tool-append seam (agent.py:1303,1315); tools registered via tools/__init__.py:8
Tokens/latency/quality measurements and estimate basis: none (no provider calls made)
Authorization/provenance/privacy checks: per-call ancestry ACL verified in source (artifacts.py:152-179); cross-session denial runtime test skipped; at-rest posture PARTIAL (plaintext, SECURITY.md:19)
Counterexamples and known limitations: upstream-truncated originals disclosed as "exact"; silent blind-cap on store failure; search 50-cap empty result; inert source_version; dead artifact_handle
Unexecuted checks and environment blockers: §6 (DB-gated runtime tests; no DB DSN)
Rollback path: disable artifacts_enabled/reducers_enabled → legacy blind cap; DB columns/table additive and inert if unused
Next task and first action: B1 prerequisites — add completeness/upstream flags (machina/core/artifacts.py + tool_views.py) and mark the store-failure fallback before wiring any projector
```
