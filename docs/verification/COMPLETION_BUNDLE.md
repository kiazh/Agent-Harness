# Completion Bundle — incident remediation + deterministic-first program

Date: 2026-10-13 · Repo: `/Users/shadow/machina` · Verified at HEAD `6a1131a` (plus user snapshots
`a7d53d4`, `1814eb2`, `e74ed2a`). Full-suite number recorded below from the final quiescence run.

## 1. Commit chain (this program)

| Commit | Contents |
|---|---|
| `c823097` | Wave 1: unbounded loop, full-mode authority, delegation correctness, controller/repomap/progress foundation |
| `1090103` | 98 research skills moved out of the repo to the operator skills dir |
| `76efb63` | Wave 2: artifact contracts, trace hierarchy, wiring, api/splits, hardening, probe |
| `3d52a1b` | W3: stagnation false-positive fix, `Container.reset()` NameError fix |
| `c9179aa` | Hotfix: env 0600 (+live machine fix), terminal-control injection, cancel-lock critical, `.GIT` case-alias bypass, card/diff redaction, auth crash |
| `a0cac28` | FM consolidation: suite-order hang root cause (container-reset singleton leak) + chunker adjacency/bound + delete_chunks guard |
| `a4f2197` | INFRA audit fixes: sandbox image rebuild (B1), npm ci, pgvector pin+SHA, placeholder guard, CI coverage, dependabot |
| `6a1131a` | A5 `turn_flow` extraction + V2 triage methodology hints + V1 formatting |

## 2. Final verification (exact commands)

```
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider --ignore=tests/test_e2e_gateway
  → 3428 passed / 0 failed / 1 skipped / 1 xfailed, 281s  (final quiescence run)
cd ui && npm run check
  → typecheck + eslint clean; 337 pass / 0 fail / 6 skipped
.venv/bin/python -m pytest tests/test_architecture_layers.py tests/test_architecture_pairs.py tests/test_leaf_independence.py
  → 76-83 passed (direction rules, deferred-import ratchet ≤677, god-file ceilings)
```

Wall-clock note: the final run took 281s vs ~142s on an idle machine — the suite is
now larger (3,430 checks incl. new streams) and hypothesis properties are
I/O-sensitive; one timing flake found under load was fixed with a recorded
rationale (`c37a0b6`, deadline=None on the audit-logging tool-execution property).

## 3. Incident issues-doc matrix — status

| Scenario | Status | Evidence |
|---|---|---|
| Greeting / self-contained workflow description | ✅ proportional routing | `test_turn_intent.py`, `test_controller_triage.py` (42-query matrix), conversational contract |
| Read-only architecture review → evidence, no edits, final report | ✅ | `test_delegation_contracts.py` (artifact verification), `test_compaction_checkpoint.py` (constraints/read-only survive) |
| Report request + compaction + interrupt/resume | ✅ | replay harness: 3 recalls then artifact (vs incident: 25 recalls, no file); `test_replay_harness.py` |
| Failed child AgentResponse → failed wrapper/DB, usage once | ✅ | `test_delegation_outcomes.py`, `test_failed_delegation_usage.py` |
| Delegate deadline / inherited shorter deadline | ✅ one authority (120s clipped) | `test_delegation_contracts.py` (115–125s window, clipped case, ContextVar reset) |
| Timeout/cancellation/descendant usage cleanup | ✅ | `test_failed_delegation_usage.py`, `test_delegation_boundaries.py`, `test_turn_completion_lifecycle.py` |
| Archive full of catalogs/echoes → instruction first | ✅ | `test_recall_filters.py` + FM5 authority demotion; retrieval eval **faithfulness 1.00** (was 0.00) |
| Nonexistent skill-like delegate name | ✅ zero-cost reject + suggestions | `test_delegate_validator.py` (profile scopes pass; DB-stored agents resolve) |
| High spend with no progress → stop + truthful reason | ✅ | `progress.py` K=3 + FM8 state-write honesty; traces reconcile to ledger (`test_trace_hierarchy.py`) |
| Export after compaction/resume → exactly-once coverage | ✅ unit-level | `test_transcript_export.py` (incl. deliberate "beyond 200" pin; resume bound 1000) |

## 4. Incident replay (offline harness)

`tests/test_replay_harness.py` — fixed loop metrics: `provider_calls=1, tokens_in=22, tokens_out=256,
recall_calls=3, delegate_launches=7, artifact_written=True, live_calls=0`.
Incident reality: 2,119,552 displayed tokens (user-confirmed via DeepSeek), ~228 tool calls, 25
post-ask recalls, zero `write_file` calls, no report. Delta: complete artifact with bounded recalls.

## 5. Token economies

- Prefix-cache pilot (E1): config-gated (`prompt_cache_enabled`, default OFF), breakpoints on
  system-tail + last tool for Anthropic / OpenRouter-anthropic; OFF-path wire bytes pinned.
  Measured basis: stable prefix ~3.4k tokens, 71% identical-prefix resend on fixture.
- Per-call accounting: traces reconcile exactly to `llm_usage` rows (5 shapes pinned); startup
  reservation reaping fixed (10-minute grace) (FM1); finish-lost surfaced (FM4).
- Faithfulness: memory + RAG 0.00 → 1.00 on fixtures with catalog-polluted data.

## 6. Security posture (this program)

Fixed and regression-locked: `.env` 0600 on every write path (live machine file repaired),
terminal-control injection on Notice/assistant paths, `.GIT`/`.MACHINA` case-alias bypass
(fail-closed on review), secrets in approval cards/diffs, cancel-during-release permanent
session lock, non-ASCII auth crash, `secrets.set` control-plane key allowlist, OAuth preview
reaping + generation skips, Dependabot coverage, placeholder API key startup refusal,
pgvector build pinned+SHA-verified, sandbox image rebuilt (was unbuildable) + CI-built.
Enforcement audit: all 7 execution paths guarded (P6/P7 re-tested against the real broker).

## 7. Deferrals / uncertainties (honest ledger)

- Live-provider validation of the prefix cache (real variance sizing) — pilot stays OFF until then.
- Windows/macOS CI matrix absent (installer paths inspected, not exercised there).
- Export fidelity: unit-level coverage; full persisted-DB vs export comparison not exercised.
- `provider.py` god-file split (template-method refactor) deferred; ceilings tracked with delta tables.
- Ports caller migration (tools/memory/rag `ports.py` consumers) — seams exist, callers partial.
- Memory/RAG FTS index — proposed SQL, not applied (operator DDL):
  ```sql
  CREATE INDEX IF NOT EXISTS idx_memories_fts
      ON memories USING GIN (to_tsvector('english', content));
  ```
- Fresh-test-DB priming: `test_schema_conformance` expects `workflow_trial`'s lazily-created
  tables (`_SETUP_SQL` in `machina/research/workflow_trial.py`); a brand-new DB needs that module's
  setup run once (pre-existing mechanism, not a regression).
- ContentHashCache uses a fixed `<path>.tmp` name; concurrent savers of the same root can race
  (pre-existing `cache.py`; worst case degrades to a re-scan).
- Hatchling `force-include` bypasses `[tool.hatch.build] exclude` (verified empirically), so runtime
  telemetry counters can ship in wheels built from a used checkout; `.dockerignore` prunes them from
  image contexts and pyproject documents the limitation. Counters only, no secrets.
- 2 user stashes + `session-5f625662.md` (tracked, scrubbed) left as-authored by operator.

## 8. Final adversarial pass (post-program)

Independent hostile review of the integrated state (invariants, cross-feature interactions, probe
suite, bundle-claims audit): **verdict CONCERNS (low) — zero blocker/major**. Verified independently:
full suite 3428/0 (144.7s idle), UI 337/0, replay metrics exact, prefix probe 3566 tok / 71.5%,
ratchet 675≤677, E1 OFF byte-identity + ON/OFF cache separation, V1 confinement, sandbox context
builds, placeholder guard precedence. Findings, all addressed at this HEAD (`see final-pass commit`):
M1 loop failure-path parity (run_stream now schedules consolidation), M2 bundle ledger self-fix,
N1 A5 delta-table entry, N2 stale-triage clearing, N3 dead `_turn_locks` removed, N4 provider cache
comment corrected to the true scoping, N5 telemetry hygiene (dockerignore + docs), N6
`learn_directory` 120s tool timeout. Regressions: `tests/test_final_pass_fixes.py` (6, RED-verified).

## 9. Unified benchmark (post-program tooling, commit `80eda54`)

`machina/research/benchmark.py` — one offline command that reuses the harnesses above as single
sources of truth: `python -m machina.research.benchmark [--json out.json]`. Recorded at `80eda54`:

- Incident replay: `provider_calls=1, tokens_in=22, tokens_out=256, recall_calls=3,
  delegate_launches=7, artifact_written=True, live_calls=0` (incident: 2,119,552 tokens,
  228 calls, 25 post-ask recalls, no artifact).
- Retrieval: memory/RAG/recall relevancy = faithfulness = 1.00 (program baseline: faithfulness 0.00).
- Prompt prefix: 28 tools, stable prefix 3,566 tokens, 71.5% identical-prefix resend on the fixture.
- Architecture: 199 files / 54,797 LOC; deferred-import census 680 ≤ cap 680 (cap moved 677 → 680
  by delta-table entry for the tool's five lazy test-harness/pytest imports).
- Full gates re-run by the same command: backend **3,439 passed, 1 skipped, 1 xfailed** (143.9s; the
  3,434 program baseline + 5 new tool tests), UI **337 passed, 0 failed, 6 skipped**.
- LoCoMo memory evaluation (external `locomo10.json`; 10 conversations, 1,986 questions; k=5):
  deterministic TF-IDF baseline evidence_recall 0.440 / hit 0.477 / MRR 0.354; production
  PostgreSQL archive search 0.425 / 0.461 / 0.345; production cross-session recall
  0.425 / 0.461 / 0.345 at 10.6 ms/query. Honest read: the lexical layer of both production paths
  sits at baseline parity — the deeper pipeline (embeddings/RAG synthesis) is scored separately by
  the retrieval section above. Answer-level token-F1 was not run (needs authorized reader-LLM
  calls; the module supports it via `evaluate_answers`).
- Cross-tool note: no shared benchmark exists with other agent harnesses (e.g. OpenCode); the tool
  emits an explicit protocol block with the two fair axes (same-task delivery metrics; LoCoMo
  memory quality) instead of unmeasurable comparisons. The dataset is never auto-downloaded;
  without it the LoCoMo section reports SKIPPED rather than fabricated numbers.
