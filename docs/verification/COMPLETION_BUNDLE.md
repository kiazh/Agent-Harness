# Completion Bundle — incident remediation + deterministic-first program

Date: 2026-10-13 · Repo: `machina` checkout · Verified at HEAD `6a1131a` (plus user snapshots
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
- `provider.py` god-file split landed (W5): `machina/core/provider/` package (facade + 13 focused
  modules), `__all__` byte-identical, per-file ceilings + package-total guard in
  `tests/test_architecture_layers.py`; deferred-import census unchanged.
- Ports caller migration: the unadopted `tools/`/`memory/`/`rag/` `ports.py` modules were deleted
  (zero importers anywhere; they asserted an architecture that did not exist). `skills/ports.py`
  remains the one adopted seam, with its conformance test in `tests/test_architecture_pairs.py`.
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
- 2 user stashes + `session-5f625662.md` (tracked, scrubbed; relocated to docs/verification/fixtures/ 2026-10-14) left as-authored by operator.

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
- **Self-hosted SearXNG** (operator setup, not committed, 2026-10-14): private metasearch
  backend for `web_search` at a local `~/Projects/searxng` checkout in the lima docker VM
  (`searxng/searxng:2026.10.13-22c64b8d3`, host :8088, `restart: unless-stopped`, limiter off,
  `format: json` on, no telemetry/auxiliary outbound calls). `SEARXNG_URL=http://localhost:8088`
  in `.env`; the security probe now accepts an explicitly configured loopback SearXNG instance
  while arbitrary/private/metadata hosts stay rejected. Proof: JSON curl (26 results), tool-level
  probe (sole outbound call `http://localhost:8088/search`, DDG path untouched), j2 gate PASS.
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
  dependency. A follow-up adversarial round found the engine expands counted repetitions at
  *compile* time (no timeout covers compile): `a{100000000}` froze the loop with ~700 MB
  growth. Counted repetitions are now screened before any compile — counts bounded at 100 and
  nested counted repetitions refused, both failing closed — with MemoryError/OverflowError
  mapped to connection errors as defense in depth.
  Four adversarial re-verification rounds followed (each re-running its own attack probes at
  the new HEAD): the final round records **CLEAN for both the pattern-safety and
  consolidation-join families**, with the one LOW residual (settle window under a
  double-cancel) hardened afterwards; the real-schema false rejection it caught
  (`{1,128}` vs the first screen bound) is pinned by tests.
  (C2) the consolidation fix covered only the CLI: gateway turns, API streaming, scheduler jobs,
  and delegated children closed the session provider before extraction ran, so those sessions
  silently never consolidated. All four close paths now join the agent's consolidation/learning
  tasks (bounded) before closing the provider.
  Also fixed: (W1) the drain is cancellation-safe and catches mid-drain scheduling; (W2) the
  head-to-head runner's post-timeout pipe read is bounded (a detached grandchild cannot hang
  it); (W3) the schema-conformance SQLite skip is file-scoped so Postgres `?` operators remain
  covered; (N1) `web_crawl` now reports "Stopped early" when bounds cut a crawl short.
  Full suite after the wave: **3,469 passed, 0 failed**.

## 11. Code-review remediation (W0-W8), in progress

Source: `docs/verification/CODEBASE_REVIEW_2026-10-13.md` (external audit, untracked) plus
three internal review lanes (A: architecture coherence; B: tests/docs; C: quantitative
metrics). Execution order: W0 -> W4 -> W1 -> W3 -> W7/W8 -> structural W6/W2/W5, with the
test batches (B) interleaved. Evidence per step below; this section grows as the queue lands.

- **W4** (`711b49e`): one authority merge (`merge_authority`) + explicit AgentDef/authority
  convention translation; `tests/test_authority_merge.py` pins the convention pairs.
- **W1a** (`4a06870`): the three unadopted ports modules deleted (zero production importers).
- **W1b** (`c3da32b`): dead constants excised; digest version single-sourced.
- **W1c** (`34f4447`): dead config/stores/params removed; `terminal_image` wired to its reader.
- **W3** (`cc309e8`): one claim-renewal primitive (`turns.renewal_loop`) used by every
  transport; `TurnOwnership.start_renewal` delegates to it (accepts `on_lost`, injectable
  loss event, returns the task); Gateway and SSE inline loops deleted; never-adopted
  `SessionTurnCoordinator` deleted with its false delegation docstring. Two bugs caught while
  landing: (a) a replacement typo emitted the unregistered protocol event
  `turn.ownership.lost` (registered name: `turn.ownership_lost`) — the emit raised inside the
  loss callback and would have silently skipped stopping owned work; cancel now precedes emit
  and the primitive guards the callback; (b) the primitive initially dropped the former
  loops' defensive renew `try/except` — restored (transport test fakes pin the two-argument
  call shape). New regression:
  `tests/test_audit_remediation.py::test_renewal_loop_reports_loss_through_the_shared_primitive`.
  Full suite: **3,486 passed, 0 failed**; ruff check + format green.

- **W1-finish** (this commit): the remaining production-dead surface removed with
  zero-reference verification per item: `compression.RollingCompaction` and its four
  methods (module docstring corrected to name the real path, `services.maybe_auto_compact`;
  its test class removed; `_split_preserve_recent`'s always-true nested branches collapsed,
  behavior identical); `models.ToolCall`/`ToolResult` (plus `__all__`/re-export cleanup);
  `context.replace_chunks` + `mark_accessed` (the fenced `replace_chunks_by_ids` is the real
  path); `orchestrator._build_agent` (superseded by `_default_agent`); `scheduler.job_runner`;
  `core/container.py` (production-dead — the live composition root is `runtime_services`;
  its tests removed); the `tools/registry.py` shim (3 tests repointed to
  `machina.tools.base.registry`). Test-observed diagnostics `agent._last_rag_mode` and
  `wiring._last_complexity` KEPT with comments — their tests assert real classification
  behavior. L4: `runtime._detect_providers` capability string now derived honestly from
  `cohere_api_key`; redundant local imports removed (`provider.py`, hoisted `time` in
  `gateway/server.py`); `_default_agent_factory` kept as sentinel with an accurate comment;
  confirmed `context_windows.turn_token_budget` / `provider._cache_clear` are documented
  compat seams and `config._session_overrides` already carries its honest "no production
  writer" note. Suite: **3,467 passed, 0 failed** (delta = removed tests); ruff green.

- **W7 batch 1** (`51cc4fc`): 40 surveyed error-handling items — every silent
  fallback on consent/ownership/persistence boundaries now records
  `record_failure` (sanitized) or logs its traceback; 27 new/extended
  assertions verified red->green. Four logging-only tails with no natural
  hook are recorded as unpinned in the commit.
- **W7 batch 2** (`d7db105`): the 13 items on the previously-held files,
  including the nuanced renewal-join cancellation fix (propagates shutdown
  cancellation; still absorbs the expected self-cancelled join so terminal
  completion and growth-listener cleanup survive). 19 pins.
- **B Batch 1** (`fe127f3`): honest recount — the earlier "33 directly
  deletable tests" was a similarity-threshold artifact over near-dup PAIRS;
  re-reading every pair validated exactly 11 true deletions (5 from the 33
  plus 6 re-derived), each with a strictly-superset twin; 21 pairs move to
  Batch 2 parametrize merges. Suites 205 + 187 green; collect count -11
  exactly.

## 12. External audit remediation (2026-10-13)

Every security/correctness claim in the untracked external review was
re-verified against the worktree with a real reproduction BEFORE any fix:
**18 CONFIRMED (all fixed), 1 REFUTED at HEAD, 1 UNPROVABLE** (an agent
example with no runnable artifact). The repro battery, its acceptance run,
and the claim->script->fix-commit index live in
`docs/verification/proofs/audit_2026-10-13/` (README + `battery_after_fix.log`;
three harnesses cannot flip — they hardcode pre-fix flows or predicates — and
each has an explicit green-side proof documented in the index).

Fix commits: `b5e46ef` (chunker preamble/aliases, document-replace lock,
IPv6 authority), `af9d938` (workspace-scoped cache + effects eligibility +
bounded search matcher), `660a063` (approval-id resolution, /init refusal,
/agents save parsing, C1 controls, env export), `a8de932` (PEM streaming
redaction, SSE terminal validation both loops, CLI overrides, trace
double-yield; provider.py ratchet 2554->2576 reviewed), `0c9eeee` (find
write-flags, approval redaction + capability grants + durable-claim
fail-closed, export ownership), `54a8151` (recursive-read review, cohere key
identity, rerank dedupe, failed outcome records, estimator full-schema,
eviction floor covers conversations, `{}` extraction fails retryable).

Consolidated tree: **3,531 passed, 0 failed, 1 skipped, 1 xfailed**;
`ruff check` + `format --check` green across `machina/ tools/ tests/`.

- **W6** (`56ec5d0`): the compression twins collapsed — `compress_session` is now
  a thin mapping over `compress_session_with_status` (completed->result,
  no_op/busy/stale->None, failed->re-raise the ORIGINAL exception via a private
  `cause` field on CompressOutcome, repr-hidden). Ownership, bounded provider
  close, end_mutation, and record_failure keys unchanged; AH-audit comments moved
  to the survivor. 8 pins (busy/stale both kinds, original-exception identity for
  persistence and claim failures, cause not leaked into repr). Acceptance: j6
  gate PASS. Full backend 3,530 passed at its landing.
- **W5** (`1fb85cf`): `machina/core/provider.py` (2,576 lines) split into a
  13-module package + re-export facade. 41/41 class/function bodies AST-verified
  byte-identical; `__all__` diff-verified byte-for-byte; the four transport
  classes stay reachable (pre-split import surface). SSE terminal validation and
  stream retry live once in transport.py; logger pinned to the original name so
  caplog filters keep matching. Ratchet: the single god-file entry replaced by 14
  measured per-file ceilings (shared `GOD_FILE_CEILINGS`, also fixing a stale pin
  in `research/benchmark.py` that would have crashed on a directory), plus two new
  guards (no submodule imports the facade; package total only falls). 47 test
  mutation/read repoints moved to owner modules; deferred-import census unchanged
  (672 <= 680). Verification: focused battery 334 passed; full backend 3,565
  passed / 0 failed; j8 gate PASS; hatchling build + artifact check OK.

- **W2** (`5a35ded` + `d166302`): the two turn loops now run on one engine.
  Step 1 landed `machina/core/turn_engine.py` (relay: redaction feed +
  exactly-once withheld tail, stable-id tool pairing with FIFO fallback,
  4000-char post-redaction truncation, token accounting, needs_approval
  extraction; close: stop renewal -> shielded bounded end_turn -> join ->
  provider close with the audited RF4-C1/W7 cancellation semantics; 20 unit
  pins). Steps 2-5 adopted it in gateway (relay+terminal, close) and SSE
  (relay, close), keeping per-transport supervisors, approval policy, frame
  serialization, growth listeners, trace spans and boundary keys in the
  adapters. Explicit SSE wire deltas accepted: tool frames always carry an
  id and pair by stable id; tool results truncate at 4000 chars after
  redaction; terminal tokens fall back to the accumulated counter; unknown
  event types are logged/skipped. Net -67 lines across the two transports;
  j7 PASSed after every step; full backend 3,576 passed / 0 failed under
  the orchestrator's own run.
- **Structural verification**: the full j1-j8 e2e gate was re-run on the
  finished tree (proof committed alongside this entry) — see
  `docs/verification/proofs/2026-10-14-e2e-gate.*`.

- **B Batch 2** (`ad255ac`): the near-duplicate pairs that could not be
  deleted without losing coverage are now 15 count-preserving parametrize
  merges across 12 files (34 scanner cases, 28 permission-risk commands,
  window-bound cases, tag parsing, chaos tool-edge handling, soulspec
  sentinels, installer prompt failure, ...). Collect count EXACT before/after
  (3578); battery 398 passed; full backend 3,576 passed / 0 failed. Honest
  scope: the earlier "137 mergeable" was never reproducible; only
  re-verified pairs were executed.
- **Format hygiene** (`fa1fb2f`): repo-wide `ruff format` normalized 29
  drifted files (mixed provenance: pre-existing drift + lanes whose format
  check covered a subset or ran before final edits). Format-only; suite
  unchanged at 3,576 passed.

## 13. Close-out state (2026-10-13)

- Remediation queue W0-W8 + B batches: COMPLETE. External audit: 18
  confirmed defects fixed with an independently re-runnable battery
  (`docs/verification/proofs/audit_2026-10-13/`), 1 refuted, 1 unprovable.
- Structural queue: W6 -> W2 -> W5 all landed after the j1-j8 gate baseline;
  final acceptance run 8 PASS / 0 FAIL / 0 SKIP (proof committed).
- Final tree: `3576 passed, 1 skipped, 1 xfailed, 0 failed`;
  `ruff check` + `ruff format --check` fully green (488 files).
- Untouched by design: the two user git stashes; the untracked external
  review file left as authored.
- **Full benchmark run (2026-10-14)**: proof `docs/verification/proofs/2026-10-14-benchmark.json`.
  Replay: 1 provider call / 22+256 tokens / 3 recalls / 7 delegate launches / artifact
  written (vs incident 2,119,552 tokens, 228 calls, 25 recalls, no report). Retrieval:
  memory/rag/recall relevancy+faithfulness 1.0 (baseline-before-program 0.0). LoCoMo
  (10 conversations, 1,986 questions): TF-IDF baseline k=5 recall 0.440 / hit 0.477 /
  MRR 0.354; k=10 0.510 / 0.555 / 0.365; production archive search 0.425 / 0.461 /
  0.345; production session recall identical at ~5.5-11.5 ms. Prefix: 3,756-token
  stable prefix, 72% identical-prefix resend share. Architecture: 211 files, 56,775 LOC,
  deferred imports 652 <= 680, every god-file ceiling green (incl. the 14 provider
  modules from W5). Suite: 3,576 passed / 0 failed; UI 341 pass / 6 skip / 0 fail.
  Note: 4 transient `batch_update_access` failures (absorbed by design, best-effort
  access bumps) occurred among ~2k retrievals; a section re-run showed zero recurrences
  and identical recall numbers — pool noise, no result impact.

- **Quality wave: local reranker + ONNX embeddings + weighted RRF (2026-10-14)**.
  fastembed 0.9 + onnxruntime 1.31 install on Python 3.14; no torch, no paid
  API anywhere. Full LoCoMo matrix (proofs + summary.md in
  docs/verification/proofs/rerank_wave_2026-10-14/): measured best fully-local
  config = bge-small ONNX embeddings (weighted RRF keyword 1.0 / dense 0.25) +
  local MiniLM-L12 cross-encoder -> recall@5 0.448 / hit 0.481 / MRR 0.373,
  beating the TF-IDF baseline (0.440/0.477/0.354) and the old production
  keyword path (0.425/0.461/0.345). Rerank p50 191 ms at production candidate
  depth; cold start 7.4 s once. **Defaults stay conservative by measurement**:
  embeddings `off` and reranker `none` for fresh installs (recall and latency
  bars not cleared for default-on), both available as opt-in; this machine
  opts in via .env (onnx + local reranker). Suite 3,697 passed / 0 failed;
  ruff green. Caveats recorded: huggingface-hub pinned <2.0 by fastembed,
  single-dataset point estimates, L6/thread/batching latency levers untested.

- **harness-evals pilot (2026-10-14)**: evaluated `harness/harness-evals`
  (Apache-2.0) against our bespoke stack. Verdict: keep the bespoke layer
  (e2e gate, unified benchmark, head-to-head, repro batteries); adopt it
  selectively for judged answer quality. Pilot ran clean end-to-end: 50
  LoCoMo questions, production retrieval -> DeepSeek answers (the one paid
  surface; a ~40-line json_object shim covers DeepSeek's rejection of
  json_schema judges) -> judge metrics: faithfulness mean 0.950 (94% pass;
  21/50 vacuous "no claims"), context_recall 0.453 (42% pass; non-vacuous
  0.299), repo token_f1 anchor 0.208, evidence recall@5 0.367 (matches the
  known retrieval number). Cost measured: 46,825 in / 3,466 out tokens for
  50 questions -> **$0.72 peak / $0.36 off-peak extrapolated to all 1,986**
  (cache-miss upper bound). Recommendations: (a) judged LoCoMo QA as an
  opt-in nightly benchmark section (~1.5 d) - ADOPT; (b) conversation
  simulation vs the SSE approval flow (~2 d, needs a thin custom target for
  the two-step resolve->resume) - LATER; (c) baseline regression gating
  (~0.5 d; deterministic per-PR, judged nightly) - ADOPT LITE. Artifacts:
  tools/harness_evals_pilot.py + docs/verification/HARNESS_EVALS_PILOT.md.

- **Firecrawl VM boot fix (2026-10-14, infra outside the repo)**: the api
  container had exited OOM/timeout three hours before the first final-sweep
  gate run — the stock harness boots 9+ node processes (api + 5 nuq workers
  + queue/extract/prefetch/reconciler) and the api never bound :3002 inside
  the 60s watchdog on the 5GiB lima VM (`ExitCode 1`; OOMKilled true on the
  first failure). Diagnosed by running the api binary alone (binds in ~3s)
  and api+one-worker (binds fine): the herd is the problem, not the binary.
  Fix: right-sized command in ~/firecrawl/docker-compose.yaml (2 nuq workers
  + queue worker + api) — ready in 16s, live scrape verified, j4 green
  again. Also noted: the stock `NUM_WORKERS_PER_QUEUE=8` default is far too
  high for this VM.
- **Final acceptance gate (post-review + post-quality-wave, 2026-10-14)**:
  **8 PASS / 0 FAIL / 0 SKIP** with the onnx-embedding + local-reranker
  stack active (proof committed at
  docs/verification/proofs/2026-10-14-e2e-gate.*). This run certifies the
  full stack: all 20 review findings fixed, local quality wave landed,
  restored firecrawl.

- **Adversarial re-verification round (2026-10-14, evening)**: a second
  hostile pass over the day's fixes found SIX real gaps, all now fixed with
  revert-verified pins: (1) rg value-taking options (`-g '*'`, `--glob`)
  hid the default directory walk and leaked credentials - full option-table
  parse audited against rg 15.1.0 (`4c76e18`); (2) the recursive-copy scan
  failed OPEN at the 4,096-entry cap and under `cp -L` symlinks - now fails
  closed with full dereference-flag semantics (`4c76e18`); (3) CLI ownership
  loss was checked only on the next yield - delayed effects ran after loss,
  generator closed after claim release/DB close - now cancels immediately
  with `['generator.close','release','db.close']` ordering (`9de5556`);
  (4) skill publication survived COMMIT-time transaction failures
  (`8186adc`); (5) provider-qualified embedding ids
  (`openai/text-embedding-3-small`) still misrouted to ONNX (`8186adc`);
  (6) the pilot stringified numeric golds before scoring (`8186adc`).
  Also fixed from live use the same day: TUI selection copy flashed
  "Copied!" while only writing OSC 52 - the real platform clipboard is now
  injected and the flash reports the true spawn result (`64d9125`).
  Suite after the round: **3,734 passed / 0 failed**; ruff green (493
  files); final gate re-run recorded below when it completes.
- **Certification after the re-verification round**: final j1-j8 gate on the
  tree carrying all six adversarial-round fixes + the TUI clipboard fix:
  **8 PASS / 0 FAIL / 0 SKIP** (proof: docs/verification/proofs/2026-10-14-e2e-gate.*,
  re-committed). Suite on the same tree: **3,734 passed / 0 failed**; ruff
  green (493 files).

## 14. Standing directives (owner, 2026-10-14)

- **Continuous holistic review**: the principal-engineer/charter review
  (10-section format, read-only, evidence-based) runs as a STANDING role in
  rounds keyed to every wave of landings and every refactoring stage: audit
  the freshly frozen tree + the newest diffs for cross-lane coherence,
  end-to-end integrity, dead/duplicated/speculative code, layer drift, and
  regression of the staged plan; report findings ranked with smallest safe
  corrections; fixes are dispatched from those findings and re-audited.
  Round 1 (findings H1/H2/M1-M7/L1-L5 + staged plan) is in the session
  record; rounds resume in the same reviewer session to keep context.
- **Skills always active**: learned skills publish enabled (no blocking
  human approval); `/skills proposals` is a post-hoc review/rollback queue;
  safety = versioned/revertible + audited + caps + verifier + eval-net;
  permission/authority/security-classified material never auto-applies.
- **Local-first**: only the agent LLM API may be paid; everything else has
  a local or self-hosted path (see sections 11-13 and the interop proofs).

## 15. Standing holistic review — Stage 2 (round-2 findings, 2026-10-14)

MCP-server-shim lane of the round-2 fix dispatch (fixes landed on the live
working tree; no commit from this lane per the dispatch rule). All changes
confined to the shim, its tests, and docs; the auto-apply lane's files were
read-only references.

- **N1 — dash-leading prompts were parsed as CLI options**
  (`machina/mcp_server/__init__.py`): `_chat_command` now emits
  `python -m machina chat -q [--session …] [--mode …] -- <prompt>`, so click
  can no longer consume `-i` (interactive flag) or `--help` (usage, exit 0).
  Pins: `test_chat_dash_prompt_follows_double_dash_terminator` (`-i`,
  `--help` argv shape) plus updated success/resume argv assertions;
  pre-fix RED verified against `git show HEAD:…`. Live proof: a real
  `opencode run` passed the prompt `-i` through the shim — status
  completed, session `63ead83a-92ba-4cfa-bb05-0ee5f2c876b1`, the reply
  treats `-i` as text, and the durable `user_message` chunk is
  `{"content": "-i"}` (logs + DB check in
  `docs/verification/proofs/interop_2026-10-14/dash_prompt_proof.md`).
- **N2 — child cwd was REPO_ROOT** (wrong in wheel installs, where it is
  site-packages): `_run_process` defaults `cwd=None` and resolves the host
  launch directory per call (`_default_child_cwd` → `os.getcwd()`); an
  explicit `workspace_root`/`machina_home` config still wins inside the
  child, so the shim does not import config. Pins:
  `test_run_process_default_cwd_is_call_time_launch_directory`,
  `test_run_process_honors_explicit_cwd` (real fast children).
- **N4 — pause-trailer leak**: `_strip_pause_lines` now also drops the CLI
  suggestion line (`Resolve the request, then resume the session.`) and the
  canonical paused-response echo (`Waiting for approval.`), on top of the
  existing `✗` / `request <id>` filters. Pins: the paused-reply exact-equality
  assertions and `test_strip_pause_lines_removes_every_pause_trailer`;
  pre-fix leak reproduced from the old function.
- **N5 — session-create schema upgrade kept (documented)**: `machina init`
  creates the deployment schema, but the additive feature migrations
  (growth/delegation/image_context/soulspec) are re-applied by every
  transport entry point; the comment now says so and
  `test_session_create_applies_runtime_schema_best_effort` pins
  attempt-before-create plus never-fatal.
- **Extension-point hygiene**: the unused `MACHINA_MCP_EXTRA_ENV`
  passthrough was deleted (charter preference; no host case existed) and
  `test_child_env_carries_only_the_allowlist` pins allowlist-only children.
- **Docs**: j9 row + skip note in `docs/verification/E2E_PROTOCOL.md`;
  README's suite references moved to j1–j9 (the dated 8-journey record is
  kept as recorded, with j9's separate interop validation named);
  new **Machina as an MCP server** sections in README and `docs/MCP.md`
  (registration command, gitignored `opencode.json`, text-only +
  exit-3-paused contract, j9 certification requirement); learning keys
  (`MACHINA_LEARNING_REVIEW_AUTO_APPLY`,
  `MACHINA_LEARNING_REVIEW_AUTO_APPLY_PER_DAY`,
  `MACHINA_LEARNED_SKILLS_DIR`) documented in README + `.env.example` from
  the current config.py/schema (read-only).
- **Gates**: `pytest tests/test_mcp_server.py` — **26 passed / 0 failed**
  (19 pre-existing + 7 new pins, stdio handshake included); `ruff check` +
  `ruff format --check` green on all touched Python files.
- **j1-j9 certification (2026-10-14, final)**: full acceptance gate on the
  tree carrying every wave of the day (adversarial re-verification fixes,
  agent tabs, MCP shim + Stage 2 durability, auto-applied learning +
  census consolidation, Stage 3 UI dedup, Stage 4+5 status/process
  consolidation, hygiene): **9 PASS / 0 FAIL / 0 SKIP** — j4 firecrawl and
  j9 opencode-MCP both green on real components (proof committed at
  docs/verification/proofs/2026-10-14-e2e-gate.*). Backend suite on the
  same tree: 3,776 passed / 0 failed.

## 16. Program close-out (2026-10-14, final)

- **Standing holistic review: EXHAUSTED.** Three rounds (round 1 → staged
  plan; round 2 → committed-red proofs + N-series findings; round 3 → final
  10-section ledger, "tree releasable on my evidence"). Every H/M/N/L
  finding is FIXED, RESOLVED-DIFFERENTLY-acceptable, or ACCEPTED-BY-DECISION;
  the single open code nit (the oauth_callback._invalid() no-op seam) was
  removed in `3b238dd`.
- **Decisions recorded**: the two untracked external review documents
  (CODEBASE_REVIEW_2026-10-13.md / -14.md) remain as-authored/untracked per
  the owner convention (they are the source records the ledger cites); the
  723KB session fixture was relocated to docs/verification/fixtures/
  (`e2a08cc`); the `approve_rollback` diagnostic-key rename and the
  shim `--json` chat mode remain optional/deferred (round-3 §10 risks).
- **Accepted risks (round-3 §10, not to fix)**: j9 is per-machine (needs
  opencode + registration); auto-apply crash window recovers via post-hoc
  review/revert (no startup reconciler); precision auto-disable is
  opportunistic; learned telemetry co-located with the curated tree;
  MACHINA_LEARNED_SKILLS_DIR is the relocation override; family listings
  bounded to 2,000 sessions; tail-dedupe prefix heuristic pinned for the
  exercised cases; the MCP shim parses CLI text until a --json mode lands.
- **Final certification (frozen tree)**: e2e gate **j1-j9: 9 PASS / 0 FAIL /
  0 SKIP** (`57eee68`); backend suite **3,776 passed / 0 failed**; UI
  **370 tests / 364 pass / 0 fail / 6 env-gated skips**; ruff check+format
  green (497 files); architecture ratchets green (deferred imports 682/682,
  mcp_server 0/0, tools 21/21, skills 4/4).
- **Delivered today (context)**: adversarial re-verification of the
  2026-10-14 review (6 findings fixed); always-active skill learning with
  safety envelope; agent tabs for delegated sessions; Machina MCP server
  (OpenCode/Codex interop keystone) with a real cross-harness round-trip;
  TUI clipboard truth; README truth refresh; slop cleanup; Stages 0-5 of
  the consolidation plan. Standing directives (§14) govern what runs
  continuously from here.

## 17. Final certification + close-out (2026-10-14, evening)

- **Standing review: rounds 1-5 complete, plan EXHAUSTED.** Round 5 (full
  tree, all skills incl. code-reviewer/adversarial/senior-qa/diagnose/
  senior-secops) returned "definition-of-done MET" with a small fix list,
  all now closed: F5 oversized auth-callback headers (`c25e451`,
  discrimination-verified pin), F6/F8/N3' README truth + provider UI
  discoverability + comment drift (`2d7400f`), F2 runner extraction +
  F3 wheel-anchor refusal + N1' state flock + N2' output redaction + F7
  hint (`c1550f9`; census 685/685).
- **Final certification (frozen tree, commit `c1550f9`)**: e2e gate
  **j1-j9: 9 PASS / 0 FAIL / 0 SKIP**; backend suite **3,989 passed /
  0 failed**; standalone UI **398 tests / 392 pass / 0 fail**; ruff check +
  format green (512 files); architecture ratchets green (deferred 685/685,
  mcp_server 0/0, tools 21/21, skills 4/4).
- **Delivered across this era**: always-on skill learning with a safety
  envelope (auto-publish/audit/revert/precision); agent subagent UI
  (footer count + Down menu) with buffered live tails; Machina MCP server +
  conversational mcp_connect/disconnect (approval-gated) + installer/
  setup/doctor interop (OpenCode/Codex/Hermes, zero writes unless
  explicit); ChatGPT-account provider (own subscription usage, live-proven,
  sign-in via CLI `machina auth` and TUI `/auth`); free-model routing
  (generic keyless `custom` provider + OpenCode Zen preset; live proof via
  Hermes's local endpoint); TUI clipboard truth; full 2026-10-14 review
  remediation + 6 re-verification fixes; Stages 0-5 consolidation.
- **Accepted risks recorded (round-5 §10)**: ChatGPT transport follows the
  open-source Codex wire (typed drift errors); token store plaintext 0600
  like the Codex CLI's own; full-mode authority bypasses per-action
  approval by design (ask/workspace/sandbox always pause); loopback-callback
  local-race model; family listing bound; npm transient (unproduced, watch);
  prior entries unchanged.
- **Standing directives (§14) remain in force** for everything after this
  close-out.
