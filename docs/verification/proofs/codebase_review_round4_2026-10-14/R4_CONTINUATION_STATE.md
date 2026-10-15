# R4 continuation state (post-compaction handoff) — 2026-10-14

Read this first after any context compaction. Source-of-truth order:
`FIX_PLAN_ROUND4_TASKS.md` (tasks) → `R4_STAGE1_LEDGER.md` / `R4_STAGE2_LEDGER.md`
(completed stages) → this file (live state) → `R4_SESSION_LEARNINGS.md` (how to work).

## UPDATE — commit series executed (latest state)
15 commits on top of `cdc5232` (newest first):
`6f0df5b` R4-022/023 UI honesty · `e2f42ce` R4-063/064 observability · `2929f88` vendor
compliance (Gemini+Claude removal, Kimi add; includes `core/config.py` + `ui/features/models.ts`,
amended) · `a0c8ef4` env-file/secrets · `64d1f91` store fencing (chatgpt+copilot) · `63f45b0`
grader · `e08fddc` resolver · `cd6d312` embedder · `5e4f284` anthropic · `6a9daff` compaction ·
`ef55400` core pipeline · `bc9ccea` UI session/approval · `1695f7c` policy/approvals *(contains
the known load-failing jitter case R4-DW is fixing)* · `10ef89b` clipboard · `056a5f7` ratchet.

REMAINING uncommitted:
(a) `machina/memory/redaction.py` + `tests/test_redaction_adversarial.py` — R4-REDACT in flight;
    after its report: flip markers, run, commit "R4-016: redaction precision".
(b) broker/store + R4-DW tests + proof — after R4-DW report: commit "R4-066b: close the
    approval deadline window".
(c) the docs commit (all round-4 docs/proofs) — last.

Then: sequential stage-3 gate (backend first, UI alone) → Gemini history strip → push branch +
PR. The detailed per-commit recipes below remain valid for (a)–(c).

## Repo state
- Branch `main`, protected; remote `origin` = https://github.com/kiazh/Machina-Harness.git.
- **5 pre-existing unpushed commits** contain a Gemini client id/secret in
  `machina/security/gemini_auth.py` (commit d173bb6) — GitHub push protection blocks any push of
  this lineage. Fix planned: strip via filter-branch/filter-repo BEFORE pushing (range
  d173bb6^..HEAD), verify `git log -S` empty, then push a branch + PR. **Do not click "allow".**
- Tests: `MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test`
  only. Always `set -o pipefail` in gating commands.

## Round-4 commit series (dependency-safe, file-cluster, repo-style messages)
DONE: 056a5f7 R4-001 ratchet · 10ef89b clipboard · 1695f7c policy+approvals(*note: committed
with one load-triggered failure in `test_decide_at_deadline_invariant_never_mismatches_durable
[1.0]`; root-cause agent R4-DW is fixing → follow-up commit; see "agents") · bc9ccea R4-024 +
R4-065 UI · ef55400 core pipeline (R4-010/017/018/019/022/027/064-span).

REMAINING (same recipe: pipefail → ruff → focused pytest → commit):
6.  `R4-011: compaction keeps delegated result text` — `machina/core/compression.py`,
    `tests/test_compression.py` (check `tests/test_context_windows.py` diff belongs here or #13).
7.  `R4-012: Anthropic tool-result coalescing` — `machina/core/provider/anthropic.py`,
    `tests/test_anthropic_tool_results.py`.
8.  `R4-021: embedding fail-open closed` — `machina/rag/embedder.py`,
    `machina/memory/embeddings.py`, `tests/test_embedding_failopen_adversarial.py`,
    `tests/test_chaos_extended.py`.
9.  `R4-026: one workspace resolver for indexing` — `machina/tools/rag.py`,
    `machina/rag/loaders.py`, `tests/test_r4_026_rag_workspace_resolver.py`.
10. `R4-020: head-to-head grader integrity` — `machina/research/head_to_head.py`,
    `tests/test_head_to_head_grader_adversarial.py`.
11. `R4-013/14/15 + 040/041/049: credential-store fencing + bounded locks` —
    `machina/security/store_fence.py`, `chatgpt_auth.py`, `copilot_auth.py`,
    `tests/test_auth_store_adversarial.py`, `tests/test_chatgpt_auth_adversarial.py`.
12. `R4-043…048 + 045/047: env-file + secrets hardening` — `machina/security/env_file.py`,
    `oauth_keys.py`, `providers.py`, `tests/test_env_file_adversarial.py`,
    `tests/test_fm7_cli.py`, `tests/test_setup_secrets.py`,
    `tests/test_r4_045_047_049_providers.py`, `tests/test_provider_store.py`.
13. `Vendor compliance: remove Gemini+Claude subscription flows; add Kimi` —
    `machina/core/provider/{specs,factory,__init__}.py`, `machina/core/runtime.py`,
    DELETIONS: `provider/{gemini,claude}.py`, `security/{gemini_auth,claude_auth}.py`,
    `tests/test_{gemini_auth,gemini_provider,claude_auth,claude_provider}.py`;
    `machina/cli/auth.py`, `ui/src/features/settings.ts` (kept: `provider=copilot`),
    `README.md`, `.env.example`, `FEATURE_LEDGER.md`, `docs/verification/OAUTH_RISK_REGISTER.md`,
    `tests/test_kimi_provider.py`, `tests/test_subscription_removals.py`,
    `tests/test_architecture_layers.py`, `tests/test_oauth_cli_auth.py`, (+context_windows test
    if kimi-related).
14. `R4-016: redaction precision` — `machina/memory/redaction.py`,
    `tests/test_redaction_adversarial.py` (flip markers) — AFTER agent R4-REDACT.
15. `R4-066b: close the approval deadline window` — broker/store + tests + proof — AFTER R4-DW.
16. `docs: round-4 review, plan, ledgers, proofs` — all `docs/verification/CODEBASE_REVIEW_ROUND4*`,
    `FIX_PLAN_*`, proofs dir.

## Agents in flight (16 launched; verify on resume — notifications may have arrived)
Verify state directly (git status on their files; run their tests) — do not trust missing
reports. Check DB contention flakes with a retry-once-in-isolation before believing failures.

- `R4-DW` ses_ec32576d1fferWeUEWCITemMYg — approval deadline-window root cause (broker.py/store.py). Commit "R4-066b".
- `R4-REDACT` ses_ec33636c5ffeRzS8MTS0Dr6vck — redaction precision (redaction.py + corpus). Commit "R4-016".
- D1 `ses_ec31d325dffeUbAX2MWkVyHdeq` — R4-080+082+083 (config/gateway/UI protocol+settings). Commit "R4-080/082/083".
- D2 `ses_ec31d325cfferMUnYKITLMblTw` — R4-081+084 (mcp_server, api/streaming). Commit "R4-081/084".
- D3 `ses_ec31d325bffeNktHyDxSB3VqpM` — R4-028+029 (orchestrator/progress). Commit "R4-028/029".
- E1 `ses_ec31d325bffdUnoioVRBh5wAcG` — R4-085+086 (connectors oauth/policy). Commit "R4-085/086".
- E2 `ses_ec31d325afferV3Q5qDCEtcXR9` — R4-088 (rag pipeline/chunker/search, repomap). Commit "R4-088".
- F1 `ses_ec31b850cffeOYdNsXjNQwXuBj` — R4-090 tools.
- F2 `ses_ec31b850cffddU3Fa16zzSyZok` — R4-091 skills/soulspec.
- F3 `ses_ec31b850bffeqhm6HZSrCZsel2` — R4-092 research (owns test_fm6_locomo edits).
- F4 `ses_ec31b850affehm0bTdQtmhr90g` — R4-094 TUI (excludes protocol.ts/settings.ts).
- F5 `ses_ec31b850affd1JzdM6dOfVvqSk` — R4-095 infra (owns .github etc.).
- F6 `ses_ec31b8509ffe1H8QTrSp80fkUW` — R4-089 memory minus redaction (owns consolidator/approval/store/shared_bus).
- G1 `ses_ec31b8509ffdxR4NoTkoBD1Fcp` — test-truth A (owns test_repo_install, test_integration_permissions, test_gateway_providers, test_chaos_extended, test_memory, test_repomap, conftest leak fixes, test_gateway/test_api env leaks, test_mcp_manage_tool; NOT .github).
- G2 `ses_ec31b8508ffeTvEVsrg9kYN23j` — test-truth B (test_rpc_gateway, test_chaos, test_memory_bug_fixes, test_review_round2_fixes, test_scheduler_coverage, test_property_based, test_controller_triage, test_load_concurrency, test_learning_review, test_handoff_regressions, test_api_bugfixes, test_bug9_bug15_fixes; NOT gateway_providers/fm6_locomo).
- G3 `ses_ec31b8507ffe5RloJ1BAfkOVHs` — R4-105 coverage (new tests; minimal agent.py/context.py touches).

STILL HELD (launch as lanes free): R4-089's redaction bullets (REDACT), R4-093 + R4-094's settings.ts/protocol.ts bits (D1), G1's .github half (F5), R4-096 dead-code wave, R4-106 seam cleanup, R4-025 (owner).

Each agent's output: evidence under proofs dir; then I verify → commit in-series (pipefail recipe) → ledger. After the batch: sequential stage-3 gate → history strip → push-ready.

## After the commit series
1. Full gate SEQUENTIALLY: backend full suite, then `ui: npm run check` alone. Logs →
   proofs dir (`stage3_gate_backend.log`, `stage3_gate_ui.log`).
2. History strip: replace the two Gemini literals in every tree from `d173bb6^..HEAD`
   (filter-branch --tree-filter or filter-repo --replace-text with a file OUTSIDE the repo;
   NEVER echo the literals into the repo). Verify `git log -S "<client-id-prefix>"` empty and
   `git log -S "GOCSPX"` empty. Also confirm the worktree still contains the removal (post-strip).
3. Push branch `round4-fixes` (main is protected; PR per the repo's GitHub-flow skill). Use the
   `no-mistakes` skill before pushing. Then update ledgers with commit hashes + push state.

## Remaining waves after commits (plan file has details)
- D: R4-080 (boot/config cluster), R4-081 [rec: parent-resolves DSN], R4-082 (effort vocabulary +
  providers remove persist + envKey), R4-083, R4-084, R4-028 [rec: delete dead backtrack call],
  R4-029 (delegation row recovery).
- E: R4-085 (OAuth connectors), R4-086 [rec: relax screener], R4-088 (RAG), R4-089 (memory).
- F: R4-090 (tools), R4-091 (skills), R4-092 (research), R4-093 (CLI), R4-094 (TUI), R4-095
  (infra), R4-096 (dead-code wave).
- G: R4-100..106 (test-truth ~35 items).
- H: Round-6 docs truth (OPERATIONS self-migrate, RELEASE_READINESS DNS), R4-025
  (`max_iterations`: rec = REMOVE the surface per product intent; needs the owner's final nod or
  proceed), final gates R4-130/131 (no-mistakes + adversarial-reviewer over the round diff).

## Owner decisions
- Gemini/Claude removal + Kimi add: DIRECTED and in flight. Copilot: resolved KEEP (ACP note).
- R4-025 / R4-028 / R4-081 / R4-086: proceed per recommendations unless the owner objects.
- Push: the owner said "ill push once its all done" — prepare everything push-ready; do not
  force an allow; strip history first.
