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

## 10. Post-program fixes (second pass, 2026-10-13)

Found by running the system live (all numbers observed, not estimated):

- **Web extraction was unusable on real pages** (`a5c4a07`): `web_extract` passed a 20 KB raw-body
  cap to the shared fetcher while docs pages are 100 KB–1 MB of markup; the live probe on
  docs.python.org failed with "response exceeds 20000 byte cap". Raw cap now 1 MB (streamed,
  deadline-enforced), tool text cap 5 KB → 20 KB, and `web_research` indexes 60 KB/page.
  Live proof: `web_research` fetched docs.python.org + realpython.com, session RAG grew
  236 → 313 chunks in 19.0 s / 6 tool calls / 50,085 tokens; a follow-up turn quoted the exact
  saved sentence with its source URL (verified byte-for-byte in the stored chunk).
- **`search_documents` demanded a raw session UUID** (`253d215`): the live run's research → recall
  loop broke with `Invalid session_id 'current'`. The tool now defaults to the active session
  (`current`/`active` aliases); explicit cross-session IDs are still rejected.
- **Memory consolidation never worked — two stacked bugs** (`8c0e013`):
  1. a shutdown race (fire-and-forget consolidation task vs `db.close()`, `InterfaceError` on
     every CLI run), fixed with a bounded drain (`core/background_tasks.py`, called by CLI chat
     before provider+pool close, gateway close, and runtime shutdown);
  2. masked by the race — `agent_factory` built `MemoryConsolidator()` with no provider, so
     extraction always raised "no LLM provider available for memory extraction". The factory now
     passes the session provider.
  Live proof (DeepSeek): the first memories ever stored in this checkout's dev DB —
  "User's favorite color is teal." (preference, 0.5) and "User adopted a beagle named Pixel."
  (fact, 0.6) — with the consolidation cursor advanced and zero stderr errors.
- **`web_crawl` (Firecrawl-lite) added** (`2011f3f`): bounded multi-page crawl (≤10 pages,
  depth ≤3, 110 s wall) over policy-checked links, every fetch through the SSRF-safe pinned
  transport, per-page indexing into session RAG, redacted-URL dedup. Live proof: the agent
  crawled docs.python.org/3/library/json.html in `ask` mode and the session indexed 16 chunks
  from that source. 13 new tests; deferred-import census (680) and file ceilings intact.
- **MCP connector compatibility fixes** (`4680eb7`, `0f1277a`): dogfooding a real MCP server
  through `mcp_call` exposed the schema validator rejecting standard JSON-Schema keywords that
  FastMCP/pydantic emit. A schema-aware census of the actual tool schemas identified the exact
  gaps: `$schema`/`$id`/`$comment`/`examples` accepted as inert; `propertyNames` and `pattern`
  enforced (bounded; invalid regex fails closed); `format` explicitly treated as a spec-level
  annotation. The fail-closed rule still covers genuinely unknown keywords (the "constraints
  never silently disappear" regression now pins `multipleOf` as its example). Live proof:
  Machina's agent ran `mcp_list → mcp_describe → mcp_call(firecrawl__firecrawl_crawl, limit=2)`
  and received `ok: true, status: completed, completed: 2/2` (4 iterations, 4 tool calls).
- **Self-hosted Firecrawl** (operator setup, not committed): `firecrawl/firecrawl` cloned,
  prebuilt `ghcr` images (no source builds), stack on :3002 = api + workers + playwright +
  redis + rabbitmq + nuq-postgres, keyless (`USE_DB_AUTHENTICATION=false`). Live scrape of
  example.com returned markdown in 1.7 s. Machina side: revisioned `~/.machina/mcp.json`
  entry (`firecrawl-mcp` over stdio), `FIRECRAWL_API_URL` via env_allowlist (never in JSON),
  `trusted_read` allowlist for scrape/map/crawl/check_crawl_status.
- **Host runtime field note** (environment, not repo): colima 0.10.3's images never ran
  cloud-init on this macOS 26 host — the machine's username is `shadow`, which is a reserved
  group in Ubuntu (`shadow:x:42`), so cloud-init silently fails to create the guest user and
  SSH keys are never installed. A lima instance with `--set '.user.name="machina"'`
  bootstrapped Docker 29.9.0 in ~30 s; compose `cpus: 4.0 → 2.5` for the 3-CPU VM.
- **Head-to-head harness comparison** (`machina/research/head_to_head.py`; full report in
  `docs/verification/HEAD_TO_HEAD_2026-10-13.md`): same-model (DeepSeek flash layer) 3-task
  matrix — **both harnesses 3/3 PASS**, test files hash-intact. OpenCode: median 15.9 s,
  25.8k tokens (excl. 516k cache reads), 33 tool calls, $0.0075 total. Machina: median 25.5 s,
  474.8k tokens, 56 tool calls (one heavy cell: task2 at 167 s / 327.8k tokens including
  terminal-interpreter resolution friction on this host). Six explicit caveats in the report
  (model mapping best-effort, cache-read asymmetry, ledger-vs-audit deltas, small N, single
  machine, synthetic scope).
- **Adversarial review + hardening (BLOCK → fixes)** — an independent three-persona hostile
  review of this wave returned **BLOCK** with two demonstrated criticals, both fixed:
  (C1) the 0f1277a `pattern` check was bounded in pattern *length* but not in *matching time* —
  a 6-char `(a+)+$` froze the event loop on a 31-char value. Matching now runs through the
  `regex` engine with a 50 ms per-match timeout and a 250 ms per-call budget (ContextVar,
  covers propertyNames fan-out); exhaustion fails the call closed. `regex` is a declared
  dependency.
  (C2) the consolidation fix covered only the CLI: gateway turns, API streaming, scheduler jobs,
  and delegated children closed the session provider before extraction ran, so those sessions
  silently never consolidated. All four close paths now join the agent's consolidation/learning
  tasks (bounded) before closing the provider.
  Also fixed: (W1) the drain is cancellation-safe and catches mid-drain scheduling; (W2) the
  head-to-head runner's post-timeout pipe read is bounded (a detached grandchild cannot hang
  it); (W3) the schema-conformance SQLite skip is file-scoped so Postgres `?` operators remain
  covered; (N1) `web_crawl` now reports "Stopped early" when bounds cut a crawl short.
  Full suite after the wave: **3,469 passed, 0 failed**.
