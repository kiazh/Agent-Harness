# Round-4 work breakdown — task-level detail (2026-10-14)

Companion to `FIX_PLAN_ROUND4_2026-10-14.md` (strategy) and
`CODEBASE_REVIEW_ROUND4_2026-10-14.md` (findings ledger). This file splits every planned
fix into executable tasks with a uniform rigor protocol.

## How to use this file

- One task per row in the index; IDs `R4-0xx`. Each task is independently verifiable.
- Tasks marked **[decision-gated]** cannot start until the owner answers (see plan file,
  "Decisions needed"). Tasks marked **[red]** fix a currently failing check.
- Ordering: within a round, tasks are ordered to front-load cheap, verified fixes; the
  dependency column lists hard blockers only. File-collision groups (bottom of file)
  must be serialized even when logically independent.

## Verification policy (binding — E2E & adversarial first)

- **No fix is accepted on unit tests alone.** Every task's primary proof must be one of:
  - **E2E** — the real route/transport/store/CLI subprocess/DB is exercised end to end,
    with mocks permitted only at the network/provider/keyring boundary.
  - **Adversarial** — an attack or failure-injection harness that actively tries to break
    the fixed behavior (hostile payloads, races under barriers, crash/ENOSPC/symlink
    injection, replayed attacks).
  - **Eval** — a fixture-scored scenario for grading/benchmark/retrieval behavior
    (gold/prediction fixtures, metric thresholds), run before and after.
  Unit tests are allowed only as **supplements** to the above.
- **Mock boundary rule:** mocks may replace external providers/HTTP/DNS/keyring only —
  never the seam the fix touches and never an internal module on the fixed path. Where a
  boundary mock is used, the proof must also show the test reaches the real seam (run a
  probe against the unfixed code and watch it fail; this review's top finding was tests
  mocking seams the run path never calls).
- **RED-first applies to the strongest proof, not a weak one:** the E2E/adversarial check
  must fail against the unfixed code for exactly the named reason, then pass after. For
  timing-sensitive adversaries run ≥5 repeats and report the count.
- **Failure-injection is mandatory** for races, leaks, timeouts, atomicity, fail-closed
  policy, and grading tasks (see matrix).
- The **E2E & adversarial proof matrix** below is the per-task requirement; where a task's
  "RED test" line names a narrower check, the matrix wins.

Proof infrastructure to build before Round 1 (shared, lives under `tests/` with an `e2e`
marker plus `tools/` helpers; fixtures/evidence under
`docs/verification/proofs/codebase_review_round4_2026-10-14/`):

- **H-SSE** — real HTTP SSE harness (ASGI/uvicorn + fake provider at the network boundary)
  asserting the raw event frames, including cancel/close paths.
- **H-RACE** — auth-store race harness: real store files + real `flock`, barrier-mocked
  token endpoint, N≥5 repeats (refresh-vs-refresh, refresh-vs-logout, exchange-vs-refresh).
- **H-CORPUS** — redaction corpus + fuzzer: synthetic secrets (dotted, base64, URL-safe,
  quoted/unquoted) AND benign lookalikes, run through the real call sites
  (`MemoryStore.add`, turn-engine tool results, traces, streaming redactor);
  metrics: leak count must be 0, benign-mangle count 0.
- **H-ATTACK** — policy attack matrix: hostile commands/paths run through the real broker
  and `decide()` (git-grep flags, revspecs, backup names, flag calibration, symlinks),
  asserting both must-pend and must-stay-routine.
- **H-UI** — fake-terminal TUI harness (extends `ui/test` patterns) + payload corpus
  (ESC/OSC-52/newlines/bidi) for rendering and flow tests.
- **H-GRADE** — grader attack fixtures (addopts-deselect, root conftest, plugin, cache
  poisoning, test reordering) PLUS a genuine-fix fixture, scored end to end.
- **H-EVAL** — eval fixtures for LoCoMo/benchmark metrics with numeric golds/decimals.
- **H-FS** — filesystem adversary: symlinks, unwritable dirs, ENOSPC, crash injection.
- **H-DB** — real `machina_test` DB fixtures (existing `db_pool` pattern) for
  concurrency/atomicity proofs.

## The P.R.O.O.F. loop for EVERY task

1. **Probe** — re-read the cited code at current HEAD (line numbers drift); reproduce the
   effect first. Never fix from the report alone.
2. **Root cause** — one sentence naming the mechanism, not the symptom.
3. **Own the proof** — build the E2E/adversarial/eval check from the matrix; it must fail
   against the unfixed code for exactly the named reason (RED). Tightening/removing a
   false-confident test is a legitimate task, but the replacement should be E2E or
   adversarial wherever a real surface exists.
4. **Optimize the fix** — smallest change that makes the proof pass; no drive-by refactors.
5. **Finalize** — E2E/adversarial run recorded → affected suites → round gate
   (ruff/format/bandit; suites per plan file). P1 tasks drop before/after evidence
   (including the RED run against unfixed code) into the proofs dir.

Definition of Done (all tasks): matrix proof type satisfied and committed with the fix;
RED-on-unfixed demonstrated (screenshot/log/fixture output); focused run green; no new
warnings in touched modules; ledger/report status updated; no claims beyond evidence.

---

## Task index

| ID | Task | Sev | Dep | Files (primary) |
|---|---|---|---|---|
| R4-001 | Trim `reasoning.py` to ratchet ceiling [red] | P1-red | — | core/provider/reasoning.py |
| R4-002 | Baseline suite triage (backend + UI) | P1-red | — | tests, docs |
| R4-003 | Gate architecture + SSE assertions into CI | P1 | R4-001,010 | .github, tests |
| R4-010 | SSE spurious error frame (turn-scope ContextVar) | P1 | — | core/agent.py, session_mode.py, api/routes/streaming.py |
| R4-011 | Compaction drops result text | P1 | — | core/compression.py |
| R4-012 | Anthropic/Claude tool-result coalescing | P1 | — | core/provider/anthropic.py, claude.py |
| R4-013 | Claude auth fencing (lock+generation+logout) | P1 | — | security/claude_auth.py, cli/auth.py |
| R4-014 | Gemini auth fencing + update_project merge | P1 | R4-013 pattern | security/gemini_auth.py, cli/auth.py |
| R4-015 | Copilot logout fence + device-flow cancel | P1 | R4-013 pattern | security/copilot_auth.py, cli/auth.py |
| R4-016 | Redaction under-match/tail-leak + over-match guards | P1 | — | memory/redaction.py |
| R4-017 | Ctrl+C cancels `machina chat` | P1 | — | cli/__init__.py |
| R4-018 | Headless `full` mode grant (or refusal) [decision-gated] | P1 | dec-1 | cli/__init__.py |
| R4-019 | Terminal timeout plumbing + typed timeout message | P1 | — | core/agent.py, tools/terminal.py |
| R4-020 | Grader integrity: hash scope + pytest-config isolation | P1 | — | research/head_to_head.py |
| R4-021 | Embedder zero-vector → typed error, never cache | P1 | — | rag/embedder.py, memory/embeddings.py |
| R4-022 | Denied→denied end-to-end (stream outcome + UI) | P1 | — | core/agent.py, ui/src/transcript.ts, components |
| R4-023 | Menu/Picker sanitization + newline splitting | P1 | — | ui/src/components/Menu.ts, components.ts, ToolTurn.ts, ApprovalCard.ts |
| R4-024 | Provider-remove stale session pin | P1 | — | ui/src/features/settings.ts, app.ts |
| R4-025 | `max_iterations` enforce-or-remove [decision-gated] | P1 | dec-1 | core/agent.py, config.py, agent_def.py |
| R4-026 | `index_document` canonical workspace resolver | P1 | — | tools/rag.py, rag/loaders.py |
| R4-027 | Uninstall shim segment-aware matching | P1 | — | cli/__init__.py |
| R4-028 | Artifact backtrack: implement or delete [decision-gated] | P2 | dec-3 | core/orchestrator.py, progress.py |
| R4-029 | Delegation row recovery on ledger failure | P2 | — | core/orchestrator.py |
| R4-040 | ChatGPT pre-logout refresh window | P2 | — | security/chatgpt_auth.py |
| R4-041 | ChatGPT exchange false-cancel on peer rotation | P2 | — | security/chatgpt_auth.py |
| R4-042 | Register Claude/Gemini/Copilot tokens for redaction | P2 | R4-013..015 | security/*_auth.py, security/secrets.py |
| R4-043 | `.env` escape round-trip + xfail flip | P2 | — | security/env_file.py, tests/test_fm7_cli.py |
| R4-044 | `.env` writer hardening (O_EXCL/NOFOLLOW/fsync/mkstemp) | P2 | — | security/env_file.py |
| R4-045 | `MACHINA_OAUTH_ALLOW_PLAINTEXT_KEYRING` semantics | P2 | — | security/oauth_keys.py |
| R4-046 | Case-insensitive env key update/remove | P2 | R4-043 | security/env_file.py |
| R4-047 | Non-loopback plaintext `http://` bearer guard | P2 | — | security/providers.py |
| R4-048 | `.env` fallback out of site-packages | P2 | — | security/env_file.py |
| R4-049 | Lock timeouts + chmod fd-close helper | P2 | — | security/chatgpt_auth.py, providers.py + siblings |
| R4-060 | `git grep` traversal routing + revspec + backup names | P2 | — | permissions/policy.py, commands.py |
| R4-061 | `_ESCAPE_ARGS` false-positive calibration | P2 | R4-060 | permissions/commands.py |
| R4-062 | `_tree_contains_component` fail-closed | P2 | — | permissions/policy.py |
| R4-063 | `safe_logging` structured + guarded redaction | P2 | — | observability/safe_logging.py |
| R4-064 | Trace-ring eviction markers | P2 | — | observability/traces.py |
| R4-065 | Approval expiry frame + UI retire | P2 | — | permissions/broker.py, ui/src/app.ts |
| R4-066 | Expired-vs-approved durable race | P2 | R4-065 | permissions/broker.py, store.py |
| R4-067 | Audit target/argv for allowed actions | P2 | — | permissions/broker.py |
| R4-068 | Store ordering F6 (`complete_execution`, `create_approval`) | Q | — | permissions/store.py |
| R4-080 | Boot/config cluster (yaml root, db-init msg, connect sentinel, run_bounded) | P2 | — | core/config.py, db/connection.py, child_process.py, cli |
| R4-081 | MCP chat DSN passthrough [decision-gated] | P2 | dec-2 | mcp_server/__init__.py, core/child_process.py |
| R4-082 | Gateway effort vocab + providers remove persist + envKey mismatch | P2 | R4-024 | gateway/features/providers_feature.py, ui |
| R4-083 | Gateway standalone token disclosure | P2 | — | gateway/__main__.py, server.py |
| R4-084 | REST SSE turnId = claim token | P2 | R4-010 | api/routes/streaming.py |
| R4-085 | OAuth connector cluster (dead guard, ports, preflight, state, to_thread) | P2 | — | connectors/* |
| R4-086 | Pattern-screener bounded nested counts [decision-gated] | P2 | dec-4 | connectors/policy.py |
| R4-088 | RAG cluster (empty reindex, split_code, fences, failure surface, cache digest) | P2 | R4-026 | rag/*, repomap/cache.py |
| R4-089 | Memory cluster (dedup embeddings, streaming hold, calibration, gate) | P2 | R4-016 | memory/* |
| R4-090 | Tools cluster (read_file paging, marker, atomic write, persona, schemas) | P2 | — | tools/* |
| R4-091 | Skills/soulspec cluster (hub JSON, delete tree, size guards, Windows, classifier) | P2 | — | skills/*, soulspec/* |
| R4-092 | Research cluster (test-path, runs-dir, benchmark timeouts, cleanup, -O) | P2 | R4-020 | research/* |
| R4-093 | CLI cluster (config-set sync, Rich escaping, tracebacks, refresh, setup) | P2 | — | cli/* |
| R4-094 | TUI cluster (re-resume, tail notices, images, composer, kbd, contrast, budget) | P2 | R4-065 | ui/src/* |
| R4-095 | Infra cluster (uv lock CI, docker reuse, pins, install.sh, telemetry) | P2 | — | .github, Dockerfiles, install.sh, pyproject |
| R4-096 | Dead-code wave (Q items: caches, RpcGateway TTL, overlap_offset, seams) | Q | — | multiple |
| R4-100 | Test truth A: unfalsifiable assertions | P1 | — | tests/* |
| R4-101 | Test truth B: never-reaches-code (core flows) | P1 | — | tests/* |
| R4-102 | Test truth C: wrong-seam mocks + mock-enforced behavior | P1 | R4-101 | tests/* |
| R4-103 | Test truth D: pre-fix pins (RPC TTL, luna, bug15, locomo) | P1 | R4-011 | tests/* |
| R4-104 | Test truth E: env coupling, skips, Windows, leaked env | P2 | — | tests/*, .github |
| R4-105 | Missing coverage: scope binding, load race, flock, approval view | P1 | — | tests/* |
| R4-106 | Production "test-only" seam cleanup (soulspec conformance etc.) | Q | — | machina/* |
| R4-120 | Docs truth (OPERATIONS, RELEASE_READINESS, checkpoint, ledger, README) | P2 | R4-018 | docs/* |
| R4-130 | Round-7 final gates (full suites, artifacts, no-mistakes, adversarial) | gate | all | — |
| R4-131 | Update review ledger with fix statuses | gate | R4-130 | docs/verification |

---

## Round 0 — baseline

### R4-001 · [P1-red] Trim `reasoning.py` to the ratchet ceiling
- **Probe:** `wc -l machina/core/provider/reasoning.py` → 68; `cat .../provider/*.py | wc -l`
  → 5508; ceilings 61 / 5501 at `tests/test_architecture_layers.py:376,414`. Run the two
  guard tests, confirm failure.
- **Root cause:** `cdc5232` added 7 lines; the ratchet (ceilings only fall) was not updated
  and the commit's gate did not run the architecture suite.
- **Fix steps:** (1) read the 7 new lines; (2) relocate/compress so the file fits 61 lines
  without behavior change (move a helper to a sibling module only if that module has
  headroom); (3) if removal is impossible without behavior change, take an explicit
  ratified ceiling bump in a separate commit with justification in the message (the
  ratchet policy prefers trimming).
- **RED test:** existing guard is the test; run it failing first, then green.
- **Proof:** before/after `wc -l` in the round proofs dir.
- **Watch-outs:** do not reformat unrelated lines; confirm no other file grew in the same
  package (sum test).

### R4-002 · [P1-red] Baseline suite triage
- **Probe:** run backend suite (serial, `machina_test`) + `ui: npm run check`; capture full
  failure list.
- **Fix steps:** classify every failure: (a) already known from this review, (b) new
  coincident breakage from the concurrent session (suspects: `test_provider_store.py`,
  `test_provider_context_audit.py`, `test_fm1_regressions.py`), (c) flake. File (b) as
  blockers with owners; record (c) with a rerun result.
- **Proof:** `baseline_backend.log`, `baseline_ui.log` in proofs dir + a triage table.
- **Watch-outs:** do not "fix" tests before classifying; DB only `machina_test`.

### R4-003 · [P1] Gate the architecture + SSE invariants
- **Fix steps:** add `tests/test_architecture_layers.py` (and later R4-010's SSE
  no-error-frame assertion) to the CI pytest selection and to the local pre-commit/gate
  script; document in `OPERATIONS.md` gate list.
- **RED test:** n/a (process fix); verify by deliberately growing a guarded file locally
  and seeing CI fail.

## Round 1 — P1 product fixes

### R4-010 · [P1] SSE spurious error frame
- **Probe:** run the E2E gate (`tools/e2e_gate.py`) or the SSE integration tests; observe an
  `error` frame after the last delta on every multi-event stream. Evidence already exists:
  `docs/verification/proofs/2026-10-14-e2e-gate.md:231-277` (`"error": 1` per turn).
- **Root cause:** `turn_scope()` ContextVar token created in the task driving the first
  `__anext__`, reset in a different task's context → `ValueError`; the scope was also
  functionally invisible to subsequent per-step tasks (contexts are copied from the
  creator, not chained).
- **Fix steps:** move turn-scope ownership to the transport driver: enter the scope once in
  `event_stream`/`_stream_session` (or an explicit engine API `begin_turn_scope/end_...`)
  so every step task inherits it from its creator; remove the decorator's ContextVar
  lifecycle (or reimplement it to tolerate cross-context reset). Do not merely swallow the
  `ValueError` — that leaves the scope inactive after step 1.
- **RED test:** integration test over the real streaming route with a fake agent emitting
  ≥2 events: assert (a) no `error` frame, (b) the turn scope is active in every step.
- **Proof:** E2E gate rerun showing `"error": 0`.
- **Watch-outs:** `retire_execution`'s `aclose()` path (streaming.py:353) and cancellation
  must still exit the scope exactly once; CLI/gateway single-task drivers must keep working.

### R4-011 · [P1] Compaction drops result text
- **Probe:** unit-level: feed `compress()` a `result` chunk written like the orchestrator
  does (`payload={"content": ...}`); render via `_chunk_to_text`; observe empty body.
  Cross-check `assembler.py:178-184` (the intended `result`→`content` fallback).
- **Fix steps:** in `compression.py:825`, read
  `payload.get("result", payload.get("content", ""))`; grep for any other renderer of
  `result` chunks to align (`prompt.py` native-block replay is separate — verify).
- **RED test:** compaction-summary text contains the delegated output for a fixture chunk
  pair; plus a no-regression test for a chunk that DOES provide `result`.
- **Watch-outs:** keep summarizer prompt budgets unchanged; check both `compress` and
  `acompress` paths (they are duplicated — see R4-096).

### R4-012 · [P1] Anthropic/Claude tool-result coalescing
- **Probe:** build two `role:"tool"` messages (as `append_tool_messages` emits for a 2-tool
  batch) and run `_to_anthropic_messages`; observe consecutive `{"role":"user"}` entries.
- **Fix steps:** collect all adjacent `tool` messages into ONE user message whose
  `content` is the ordered list of `tool_result` blocks; keep one `tool_result` per call id;
  apply to `anthropic.py` only (ClaudeProvider inherits).
- **RED test:** 2-tool turn → single user message, roles alternate, both `tool_use_id`s
  present; 1-tool turn unchanged; interleaved assistant text unaffected.
- **Watch-outs:** error results (`is_error:true`) ordering; empty-content guards.

### R4-013 · [P1] Claude auth fencing
- **Probe:** read `chatgpt_auth.py:98-206,428-434` (reference pattern) vs
  `claude_auth.py:360-452`; reproduce race with a barrier-mocked transport and two
  concurrent `refresh()` calls; reproduce logout resurrection with a delayed-save refresh.
- **Fix steps:** (1) reuse `fcntl` per-store lock with `_store_target` resolution
  (audit-pattern parity: same resolved spelling for lock+save); (2) held across
  read→POST→publish with peer adoption under the lock; (3) generation file +
  `invalidate_credentials`; (4) `refresh` rechecks generation post-POST, saves only if
  unchanged; (5) post-error re-adoption of the winner's pair; (6) `claude_logout` becomes a
  fenced store helper (bump generation under lock → unlink); CLI calls it.
- **RED tests:** concurrent refresh (one request to the network, second adopts, no
  invalid_grant); refresh completing after logout does not recreate the store; import path
  keeps fencing.
- **Proof:** before/after logs in proofs dir.
- **Watch-outs:** Windows path (no fcntl) — mirror whatever chatgpt does; keep the store
  file mode/atomicity; do not change token normalization.

### R4-014 · [P1] Gemini fencing + update_project merge
- **Fix steps:** same lock/generation pattern; `update_project` (gemini_auth.py:215-222)
  becomes read-merge-write under the same lock (re-read immediately before save; never
  clobber a newer access token).
- **RED tests:** project-update interleaved with refresh cannot resurrect an expired token;
  logout resurrection fence; refresh remains last-writer-safe.

### R4-015 · [P1] Copilot logout fence + device-flow cancel
- **Fix steps:** fenced unlink helper; cancel/ignore pending device-flow completion after
  logout (generation or explicit cancel token), keeping the documented `gh auth token`
  re-import behavior.
- **RED test:** device flow completing after logout does not write the store.

### R4-016 · [P1] Redaction under-match/tail-leak + over-match guards
- **Probe:** synthetic corpus through `redact_secrets`: dotted keys, URL-safe base64,
  values with `+/=~`, short leading runs, plus benign strings ("bearing bad news", "task-…",
  9-digit math, 19-digit numbers).
- **Root cause:** ordered substitution with a narrow value class that destroys quote
  adjacency; checksum/anchoring gaps in over-match rules.
- **Fix steps:** (1) move quoted-key rules BEFORE `generic_secret`; (2) quoted rule:
  capture the full quoted value regardless of charset (min length guard); (3) unquoted
  rule: widen class to `[A-Za-z0-9_\-+/=.~]{16,}`; (4) post-pass: re-scan the output once to
  catch remnants; (5) over-match: `Bearer` requires ≥16-char token-shaped value; `sk-` gets
  a `(?<![A-Za-z0-9])` prefix bound; SSN rule gated by context label OR document the
  residual false positive and keep fail-closed; CC quantifier extended to 13–19 digits with
  Luhn retained.
- **RED tests:** every corpus secret fully removed (assert no tail); every benign string
  untouched; streaming variant aligned.
- **Proof:** before/after redaction table in proofs dir.
- **Watch-outs:** redaction is on hot paths — keep regex count sane (re-run micro-bench);
  `redact_dict`/`redact_value` paths share patterns.

### R4-017 · [P1] Ctrl+C cancels `machina chat`
- **Probe:** run a chat turn with a 90s tool; send SIGINT; observe nothing stops, then
  "Aborted!" only after completion.
- **Fix steps:** run the turn coroutine on the main loop and install
  `loop.add_signal_handler(SIGINT, ...)` that cancels the running turn task and completes
  the existing cleanup (claim release, provider close); fallback for Windows via
  `signal.signal`. Exit 130.
- **RED test:** subprocess test: start a chat with a slow tool, send SIGINT, assert exit
  within ~2s, claim released, no orphan process group.
- **Watch-outs:** keep `_run`'s import-time safety for non-main-thread invocations; the CLI
  ownership-loss path must interact cleanly (tests exist at `tests/test_cli_turn_ownership.py`).

### R4-018 · [P1, decision-gated] Headless `full` mode
- **Fix steps (recommended):** after `session_manager.create` in `chat`, mirror gateway:
  `activate_durable_mode`/`grant_full_default` when effective mode is `full`; print a
  one-line notice. Alternative: refuse `--mode full`/saved-full with a typed error.
- **RED test:** headless run with `mode=full` executes a routine tool with zero pauses
  (or the refusal path returns the documented exit code).

### R4-019 · [P1] Terminal timeout plumbing
- **Fix steps:** thread the requested timeout into the per-call wait_for budget
  (`min(requested, hard cap)`); on `TimeoutError`, format `Error: <tool> timed out after
  <N>s (limit ≤60s per call)`; keep the 60s hard cap only if policy demands — else raise the
  cap to match the schema (decision note: schema says 300, so honor it with a documented
  cap).
- **RED tests:** compressed-timeout test asserting the typed message and cancellation at the
  requested bound; regression for default 60.

### R4-020 · [P1] Grader integrity
- **Fix steps:** (1) hash the whole fixture root except an explicit workspace allowlist
  (source dirs the agent must edit); (2) run pytest with `-p no:cacheprovider`,
  `--override-ini=addopts=` and `-c` pointing at a pristine config inside the fixture;
  (3) require `failed == 0` parsed from output (not just exit code) and the workspace hash
  check; (4) fix the root-`--test-path` false negative by hashing per-path semantics.
- **RED tests:** (a) addopts-deselect attack → `success=false`; (b) genuine fix →
  `success=true`; (c) root-level test file case.
- **Watch-outs:** keep evidence fields (`git_capture`) recorded; don't break existing
  fixture formats.

### R4-021 · [P1] Embedder zero-vector
- **Fix steps:** `_embed_uncached`: missing/empty `data` on non-empty input → raise typed
  `EmbeddingError`; ensure the cache is not written on failure; add a defensive all-zero
  check in `memory/embeddings.py` (reject with log).
- **RED tests:** malformed 200 → raises; cache empty after; batch path unchanged.

### R4-022 · [P1] Denied→denied end-to-end
- **Fix steps:** stream `_tool_outcome(item)` in the `tool_result` event (agent.py:1155
  area); add `"denied"` (and keep `"failed"`/`"paused"`) to the UI event contract
  (`ui/src/events.ts`), map it in `transcript.ts:194` to a distinct status, and render a
  distinct marker in `ToolTurn`/`ToolCard` (e.g. ⊘ + muted color, not the success ✔).
- **RED tests:** backend: streamed outcome equals "denied" for a broker-denied call; UI:
  denied card does not render the success state; legacy `isError` path unchanged.

### R4-023 · [P1] Menu sanitization + newline splitting
- **Fix steps:** wrap title/label/description with `sanitizeDisplay` at render (and split
  on `\n` into separate rows); apply the same newline-splitting to `ToolTurn` header and
  `ApprovalCard` title; keep truncate math on sanitized text.
- **RED tests:** ESC/OSC/newline payloads in labels; assert output contains no raw control
  bytes and rows count matches split.

### R4-024 · [P1] Provider-remove stale pin
- **Fix steps:** refresh App session state whenever session route changes
  (`session.setModel`/`setProvider` path in `app.ts onConfig`), or have `removeProvider`
  fetch the session before the guard; keep the fallback message.
- **RED test:** scripted: add → switch → remove → submit; assert the turn targets the
  fallback provider.

### R4-025 · [P1, decision-gated] `max_iterations`
- **Fix (enforce, recommended):** read the effective cap (agent def → config) in both loops;
  count iterations; on exceed, stop with the existing bounded-stop machinery
  (stagnation-style stop with `stop_reason="max_iterations"`); keep `while True` semantics
  when cap is None. **Fix (remove):** delete field + config + soulspec mapping + DB column
  (migrate) + tests.
- **RED test:** cap=1 → exactly one provider call and the typed stop reason; cap=None
  unchanged.

### R4-026 · [P1] index_document canonical resolver
- **Fix steps:** construct the FileLoader with `execution_context.workspace_root()` (single
  resolver used by the broker); pass the broker-resolved absolute path from `tools/rag.py`;
  keep relative resolution for legacy callers through the same resolver.
- **RED tests:** config with `workspace_root != machina_home != cwd`: absolute workspace
  path indexes the right file; relative path resolves to workspace, never `machina_home`.
- **Watch-outs:** keep `loaders.py` private-part filters; don't break HTTP document route
  (pre-built Document bypasses the loader).

### R4-027 · [P1] Uninstall shim matching
- **Fix steps:** match on path segments (split on separators, compare the app_root chain as
  a prefix of full segments), case-insensitive; keep the existing test green.
- **RED test:** sibling `/Users/x/machina-old` shim → not matched; exact + containing-dir
  cases unchanged.

### R4-028 · [P2, decision-gated] Artifact backtrack
- **Fix (implement):** on missing expected artifact, perform one forced synthesize/write
  attempt using the existing progress advice (call the write path, not just the predicate)
  then re-check; **Fix (delete):** remove the dead call + comment + the test's
  `should_write_first` assertion, and record why in the ledger.
- **RED test (implement):** missing artifact → one write attempt + second check; (delete):
  n/a.

### R4-029 · [P2] Delegation row recovery
- **Fix steps:** when the post-run ledger read fails, record `status="error"` with
  `tokens=null` (explicitly unknown) or retry the read once; never leave rows `running`
  silently; update the test that pins the zero-record behavior.
- **RED test:** ledger read raises → row ends `error`, `_record_end` called once.

## Wave B additions (owner-directed + lane gaps)

### R4-GEMINI-REMOVE · Remove the Gemini Code Assist OAuth (subscription) flow — **owner-directed**
Reason (independently verified, primary sources): Google's Gemini CLI FAQ declares third-party
OAuth reuse a direct ToS violation subject to account suspension; abuse detection is live
(discussion #22970, 2026-10-11) and account/project suspensions for this pattern are
documented (#18999, #25648). Also removes the GitHub push-protection blocker.
Scope:
- Delete `machina/security/gemini_auth.py`; remove its store path from
  `permissions/policy.py` credential-store gating and the `store_fence` docstring mention.
- `core/provider/gemini.py`: remove the license/auth-file mode ONLY; keep the API-key
  (AI Studio) provider path fully working.
- `core/provider/factory.py:127-128`, `specs.py:124` comment, `runtime.py` references,
  `cli/auth.py` gemini sub-app + import + help text.
- Tests: delete `test_gemini_auth.py`; prune license parts of `test_gemini_provider.py`,
  `test_oauth_cli_auth.py`; drop the R4-014 Gemini fence tests from
  `test_auth_store_adversarial.py` (code removed).
- Docs: README subscription list, FEATURE_LEDGER, OAUTH_RISK_REGISTER (record removal +
  reason); leave historical proofs as-is (annotate, don't rewrite evidence).
- `provider=gemini` without an API key → clear typed error pointing at AI Studio keys
  (no auth-file fallback).
Proof: no code reads `~/.machina/auth/gemini.json` or `~/.gemini/oauth_creds.json`;
API-key gemini E2E green; auth surfaces (CLI/gateway/UI) no longer offer gemini; full suite
green.

### R4-GEMINI-HISTORY · Strip the Gemini client id/secret from the 5 unpushed commits — **owner-directed**
- After all writers stop: `git filter-repo --replace-text` (fallback: filter-branch) with a
  replacement file **outside the repo**; verify `git log -S` finds nothing; then push —
  **no GitHub "allow" click needed**. Rewrites hashes of ac832cd→cdc5232; notify the other
  session to re-fetch.

### R4-049b · chmod-failure fd-close helper (remaining sites)
- `chatgpt_auth.py:154-165,334-347`, `claude_auth.py:173-186`, `copilot_auth.py:152-165`,
  `providers.py:216-229` (gemini site disappears with R4-GEMINI-REMOVE). Small shared helper
  (store_fence) + tests; reconcile with R4-CGPT's lock work.

### R4-CLAUDE-REMOVE · Remove the Claude subscription OAuth flow — **owner-directed (confirmed banned)**
Evidence: Anthropic (2026-02-19, code.claude.com/docs/en/legal-and-compliance): OAuth tokens from
Free/Pro/Max plans are for Claude Code and native Anthropic apps only — "not permitted" in any
other product, tool, or service; "Anthropic does not permit third-party developers to offer
Claude.ai login into their own applications, or to route requests through Free, Pro, or Max plan
credentials on behalf of their users"; developers may not collect/store/intermediate Claude.ai
tokens. Enforcement/bans documented (2026-02).
Scope mirrors R4-GEMINI-REMOVE: delete `security/claude_auth.py`; remove its store path from
policy credential gating + the store_fence docstring; keep `provider=claude`/anthropic API-key
paths (ANTHROPIC_API_KEY) fully working; drop the license mode from `provider/claude.py` +
factory/specs references; CLI `auth claude` sub-app + help text; tests (`test_claude_auth.py`
deleted; adversarial claude tests dropped); docs (README subscription list, FEATURE_LEDGER,
OAUTH_RISK_REGISTER).
Proof: no code reads `~/.machina/auth/claude.json` or the Claude Code keychain; API-key anthropic
provider E2E green; full suite green.

### R4-COPILOT-DECISION · Copilot subscription flow — **RESOLVED: KEEP (with note)**
- Verified: current GitHub terms (2026-03-05, superseded by the GitHub Generative AI Services
  Terms) include CLI tools as a Copilot surface; no prohibition of third-party consumption was
  found (unlike Anthropic/Google). GitHub actively enables third-party use: Copilot CLI ships an
  **ACP server** ("use Copilot CLI as an agent in any third-party tools, IDEs, or automation
  systems").
- Nuance for the roadmap (not blocking): the sanctioned bridge is the official Copilot CLI
  (ACP / `-p` mode); raw token reuse is grey. Optional future task: migrate `copilot_auth`
  consumers to drive the official CLI via the existing interop pattern.
- Action: keep; record the note in OAUTH_RISK_REGISTER.

### R4-KIMI-ADD · Add Kimi Code (K3 / KFC) as a supported provider — **owner-directed (officially supported)**
- Kimi Code docs: "Subscribers can also obtain an API Key to integrate Kimi Code's model
  capabilities into third-party development tools and platforms" — with first-party guides for
  Claude Code, OpenCode, Codex. Officially sanctioned; API-key based (no OAuth piggyback).
- Endpoints (overseas): OpenAI-compatible `https://api.kimi.ai/coding/v1` (China
  `https://api.kimi.com/coding/v1`); Anthropic-compatible `https://api.kimi.ai/coding/`.
- Models: `k3`, `k3-256k`, `kimi-for-coding`, `kimi-for-coding-highspeed` (k3 1M context for
  Plus/Moderato+; high-speed Pro/Allegretto+).
- Scope: add a `kimi` provider spec (OpenAI-compatible; env key `KIMI_API_KEY`; default model
  `kimi-for-coding`), catalog/context-window entries, README/setup docs; tests (spec + E2E
  payload shape via MockTransport). Reuse the existing direct/OpenAI-compatible transport —
  NO new auth machinery.
- Proof: provider E2E with mocked transport asserting endpoint/model/headers; config validation
  accepts the key; docs updated.

## Round 2 — security

### R4-040 · ChatGPT pre-logout refresh window
- **Fix steps:** capture the generation BEFORE the caller's tokens were read (or re-check
  at publish against logout-time generation); if the store was removed after the tokens
  were issued, refuse the save. Update the docstring claim accordingly.
- **RED test:** tokens loaded → logout → 401 refresh path → store NOT recreated.

### R4-041 · ChatGPT exchange false-cancel
- **Fix steps:** treat only generation changes as cancellation; content fingerprint
  mismatch from a benign peer rotation adopts the rotated store and proceeds (or retries the
  save) instead of raising `chatgpt_auth_cancelled`.
- **RED test:** exchange completes while a peer refresh rotated the store → login succeeds,
  store contains the exchanged tokens, no cancel error.

### R4-042 · Redaction registration for all stores
- **Fix steps:** call `remember_secret_value` at every token save/load boundary for
  claude/gemini/copilot (mirror `chatgpt_auth.py:227-234`), including access+refresh.
- **RED test:** synthetic token echoed into a tool result/error is redacted for each
  provider.

### R4-043 · `.env` escape round-trip
- **Fix steps:** implement unescaping in `read_env_values` (`\\`→`\`, `\"`→`"` after quote
  strip); flip the strict xfail in `tests/test_fm7_cli.py:325-337` into a passing test; add
  round-trip cases (backslash, quote, trailing spaces).
- **Watch-outs:** python-dotenv parity for the live loader; don't break membership checks.

### R4-044 · `.env` writer hardening
- **Fix steps:** `mkstemp` in the target dir + `O_EXCL|O_NOFOLLOW` semantics, 0600 via
  fchmod, write, fsync file, `os.replace`, fsync dir; unpredictable name removes the
  `<name>.tmp.<pid>` symlink attack.
- **RED tests:** pre-created symlink at the old predictable path is untouched; crash-during
  write never leaves an empty `.env` (simulated failure injection).

### R4-045 · Keyring plaintext flag
- **Fix steps:** flag set → allow plaintext backend (the documented escape hatch) while
  still rejecting unknown backends unless explicitly allowed; flag unset → current strict
  allowlist.
- **RED tests:** both directions; unknown backend rejected in both.

### R4-046 · Case-insensitive env keys
- **Fix steps:** normalize on match in `_env_line_key`/`read_env_values`; update/remove
  must find `openrouter_api_key=…`; appending duplicates never occurs.
- **RED test:** lowercase pre-existing line is updated in place; removal removes it.

### R4-047 · Plaintext http bearer guard
- **Fix steps:** allow `http://` only for loopback by default; non-loopback requires an
  explicit opt-in (`MACHINA_ALLOW_PLAINTEXT_HTTP=1`) or is refused with a clear message.
- **RED tests:** non-loopback http refused by default; opted-in warning path; loopback
  unchanged.

### R4-048 · `.env` fallback site-packages
- **Fix steps:** detect wheel layout (no repo markers) and skip the `parents[2]` fallback;
  target the explicit override, the walk-up result, or refuse with guidance. Same for the
  allowed-roots computation (:51).
- **RED test:** simulate installed layout in tmp; no site-packages path is used/created.

### R4-049 · Lock timeouts + fd close
- **Fix steps:** bounded `flock` acquisition with typed timeout error (both stores); helper
  that closes the mkstemp fd when chmod fails before fdopen (6 sites).
- **RED tests:** contended lock → typed error within bound; chmod-failure injection → fd
  closed, temp removed.

## Round 3 — permissions / approvals / observability

### R4-060 · `git grep` traversal + revspec + backup names
- **Fix steps:** route `git grep` (and any git subcommand matching recursive-search
  semantics) into `_recursive_traversal`-style review; handle `REV:path` operands in
  `sensitive_path` (split on `:`), add `.env~`/`.env-backup`/`credentials~`/`.netrc.bak`
  families (keep `.env.example` allowed).
- **RED tests:** `git grep --untracked --no-exclude-standard TOKEN` → pending card;
  `git show HEAD:credentials` → credential_access; `.env~` variants flagged; `.env.example`
  still allowed; benign git reads unchanged.

### R4-061 · Escape-args calibration
- **Fix steps:** split the escape list into "dangerous anywhere" vs "dangerous as bare
  token"; keep `git -c`/`python -c`/`find -o` blocked; allow `wc -c`, `ls -C/-o`,
  `grep -C N`, `head -c N`, `diff -c`.
- **RED tests:** allow-list cases produce routine decision; blocked spellings still blocked
  (including attached forms per `commands.py:128-132`).

### R4-062 · Tree-contains fail-closed
- **Fix steps:** OSError on `iterdir`/`is_dir` → treat as "contains" (return True) matching
  the truncation doctrine.
- **RED test:** unreadable subdir inside a recursive-copy source → requires review.

### R4-063 · safe_logging
- **Fix steps:** redact dict/list/tuple args recursively; wrap `redact_secrets` in
  try/except with `[REDACTED]` fail-closed; keep non-machina loggers untouched.
- **RED tests:** structured secret arg redacted; redactor raising → masked, no exception
  escape.

### R4-064 · Trace-ring markers
- **Fix steps:** record evictions (`dropped_turns`/`dropped_spans`, tool-call delta) and
  surface in `turn_report`/diagnostics; never evict a turn with open spans without marking.
- **Follow-up (found during R4-010):** `instrument_stream`'s OTel `span(...)` still exits
  across contexts under per-step drivers — opentelemetry logs and swallows it, no frame;
  move the span to the driver-owned lifetime or document the limitation.
- **RED test:** >128 child turns → parent report carries `dropped` counts, tool totals still
  correct or explicitly marked.

### R4-065 · Approval expiry frame + UI
- **Fix steps:** broker emits an `approval.resolved`/`permission.resolved` frame with status
  `expired` on timeout (same channel as decisions); UI retires the card + footer on it.
- **RED tests:** expiry emits the frame; UI test retires card/status without a decision.

### R4-066 · Expired-vs-approved race
- **Fix steps:** on resolve returning `None` because the row is already `approved`, honour
  the durable record (continue as approved or explicitly re-emit its state) instead of
  reporting "expired".
- **RED test:** resolve lands microseconds before timeout → caller sees approved semantics;
  no stale-approved replay on retry.

### R4-067 · Audit target/argv
- **Fix steps:** include targets (paths/argv digest) on `permission_allowed` audit rows for
  allowed-without-approval actions (bounded preview + redaction).
- **RED test:** routine write/exec rows carry target info; no raw secrets.

### R4-068 · Store ordering F6
- **Fix steps:** `complete_execution` memory-after-durable (or explicit divergence marker);
  `create_approval` cleans the in-memory record when the INSERT fails.
- **RED tests:** durable failure leaves no authoritative memory state; insert failure leaves
  no orphan pending.

## Round 4 — P2 clusters (each bullet = one subtask)

### R4-080 · Boot/config
- [ ] non-mapping YAML root → warning + defaults (`config.py:576`); RED test with a list root.
- [ ] recovery message `machina init` (`cli/__init__.py:220`); test asserts the message.
- [ ] `Database.connect` connecting-sentinel (`db/connection.py:35-52`); concurrency test.
- [ ] `run_bounded` catches `ValueError` → typed result (`child_process.py:221`); NUL-byte test.

### R4-081 · MCP chat DSN [decision-gated]
- [ ] parent resolves DSN and passes it explicitly to the child env (recommended);
  integration test: same DB across `machina_session_create` and `machina_chat`.

### R4-082 · Gateway/providers
- [ ] unify effort vocabulary across both validators or make `config.set` route through the
  catalog normalizer (`server.py:693-703`); tests for `xhigh|max|off` on both surfaces.
- [ ] `apiKeyEnv` vs `envKey` — pick one name, update server + protocol + UI; contract test.
- [ ] gateway `providers.remove` persists its fallback like the CLI (`providers_feature.py:292-298`);
  restart test shows the fallback survives.

### R4-083 · Gateway token disclosure
- [ ] `python -m machina.gateway`: print the minted token (or read it from a file) and
  correct the comment/SECURITY.md; test the standalone handshake path.

### R4-084 · REST SSE turnId
- [ ] emit the 12-hex canonical id (not the claim owner) in REST frames; keep ownership
  internal; cross-transport correlation test. (Depends on R4-010's scope rework.)

### R4-085 · OAuth connectors
- [ ] remove the `or {}` dead guards (5 sites) and exercise the refusal path; test
  `oauth_not_configured`.
- [ ] validate `parts.port` at config time (`models.py:178-190`); test `:99999`/`:abc`
  rejected at save, preview never raises ValueError.
- [ ] refresh preflight: wait on the in-flight refresh instead of hard-fail
  (`oauth.py:1182-1188,1260-1274`); concurrent-caller test.
- [ ] ASCII-check callback `state` (`oauth_callback.py:214-222`); non-ASCII test returns the
  error page, no TypeError.
- [ ] `_read_index` calls to `to_thread` (`oauth.py:761,984`); loop-lag test.

### R4-086 · Pattern screener [decision-gated]
- [ ] allow bounded nested counts with a small product budget (e.g. ≤1000 reps), keep
  explosive patterns rejected; IPv4-style schema accepted; existing rejection tests
  re-baselined.

### R4-088 · RAG/repomap
- [ ] empty re-index deletes the prior generation (`pipeline.py:318`); truncate→re-index test.
- [ ] decorator-aware `split_code` (`chunker.py:188-233`); decorated def test.
- [ ] fence-aware markdown headings (`chunker.py:142-159`); fenced `#`-in-code test.
- [ ] dual-branch search failure surfaces loudly (`search.py:100-137`); both-fail test.
- [ ] repomap cache: verify digest or rename to stat-cache (`repomap/cache.py:33-43`);
  mtime-preserving-write test.
- [ ] `_hard_split` guards `chunk_size<=0` (`chunker.py:405-424`); property test.

### R4-089 · Memory
- [ ] dedup candidates embed before comparison (`consolidator.py:296`); paraphrased-memory
  test.
- [ ] streaming redactor digit-run fix (`redaction.py:491-618`); long-digit stream test.
- [ ] calibration follow-ups from R4-016 corpus (SSN/CC/sk-) with policy note.
- [ ] wire the approval gate or delete `approval.py` + docs claim; test per decision.
- [ ] `update_access` quarantined filter; `shared_bus` retryable receipts.

### R4-090 · Tools
- [ ] `read_file`: apply offset/limit before the size gate (`file.py:287-292`); big-file
  paging test.
- [ ] truncation marker for line-limited reads (`file.py:315-317`); marker test.
- [ ] atomic `write_file` (temp+replace) (`file.py:357-405`); ENOSPC simulation test.
- [ ] `harness_status`: distinguishable persona failure (`harness.py:165-203`); test.
- [ ] honor `additionalProperties` or set `false` everywhere (`base.py:234-261`); schema
  contract test.
- [ ] parameter schemas for the 5 tools (`builtins.py:232,274,465`, `skills.py:11-14,48-51`).

### R4-091 · Skills/soulspec
- [ ] hub JSON type-hardening (`registry.py:1063-1109`); corrupt-hub test.
- [ ] `delete_skill` removes manifest-owned tree (`registry.py:901-916`,
  `repo_install.py:915-936`); reinstall-after-delete test.
- [ ] pre-stat size guards (`registry.py:337-350`, `soulspec/schema.py:76`); symlink-to-huge
  test.
- [ ] Windows activation fsync best-effort (`repo_install.py:993-1008`); platform-conditional
  test.
- [ ] repo URL port validation (`repo_install.py:91-97`); `:99999` test.
- [ ] workflow classifier gate (`workflow.py:126-149`); "go ahead" test asserts no coding
  stages.

### R4-092 · Research
- [ ] root `--test-path` hashing fix (`head_to_head.py`); success=true for a genuine fix.
- [ ] `--runs-dir` guard (`head_to_head.py:663`); fixture-contamination test.
- [ ] benchmark subprocess timeouts + non-zero exit on section error (`benchmark.py:388-501`);
  test.
- [ ] cleanup isolation per step (`workflow_benchmark.py:83-94`, `locomo.py:532-534`,
  `identity_eval.py:92-100`); one-failure-keeps-cleaning test.
- [ ] `-O`-safe measurement (`workflow_benchmark.py:55-66`); no side-effects-in-assert test.

### R4-093 · CLI
- [ ] `config-set`/`mode` sync `.env`+env like gateway (`cli/__init__.py:1498-1519,492-515`);
  pinned-key test.
- [ ] Rich escaping for all interpolated output (`cli/output.py:68-129`, `cli/__init__.py`
  prints); `[/b]`-title test.
- [ ] traceback-free fork/growth (`cli/__init__.py:646-666`, `cli/growth.py:97-169`); typed
  error tests.
- [ ] `providers models --refresh` keeps the cache on empty success (`cli/providers.py:375-404`);
  test.
- [ ] `setup` env-file guard (`cli/__init__.py:1298`); bad-override test.
- [ ] `repl --mode` validation (`cli/__init__.py:400-408`); invalid-mode test.

### R4-094 · TUI
- [ ] same-id re-resume guard (`app.ts:369-401`); mid-turn re-resume test.
- [ ] hidden-failure notices survive completion (`tail.ts:113-124`); test.
- [ ] image-marker reset (`app.ts:822-827,993-998`); Ctrl+C→EscEsc test.
- [ ] composer window indicator + mouse offset (`components.ts:184-220`); tests.
- [ ] Kitty Ctrl+S via `matchesKey` (`Form.ts:93,241`); unit test.
- [ ] contrast fix for cobalt2/solarized selection rows (`design/theme.ts:637-641`);
  token-level test.
- [ ] `/mode revoke` copy → `/approvals revoke` (`mode.ts:57`); copy test.
- [ ] delegate budget > server budget (`agents.ts:182`, `app.ts:327`); test.
- [ ] `/keys` allowlist alignment (`keys.ts:22-39`); empty-key redaction guard
  (`settings.ts:163-167`).
- [ ] job prefix pagination honest (no dead cursor loop) (`jobs.ts:15-38`); >500-job test.
- [ ] MCP OAuth cancel on transient failures (`mcp-oauth.ts:174-204`); test.

### R4-095 · Infra
- [ ] CI `uv lock --check` step; lockfile enforcement test in workflow.
- [ ] docker-db reuse credential reconciliation (`tools/install-helpers.sh:216-245`);
  reuse-path test with fake docker.
- [ ] pin actions by SHA + base images by digest; dependabot notes.
- [ ] compose `./machina` mount documented as inert or wired via PYTHONPATH; test/note.
- [ ] install.sh env-file parent guard (`install.sh:305-309`); override test.
- [ ] wheel telemetry exclusion (`pyproject.toml:65-71`).

### R4-096 · Dead-code wave (Q)
- [ ] tool-cache policy decision (`tools/base.py:76-90`) — remove cache or declare effects;
- [ ] `_RpcGateway` TTL/lock simplification (`api/app.py:150-166`);
- [ ] `ContextManager` dead params (`core/context.py:162-167`, incl. the phantom cache
  documented in tests);
- [ ] `overlap_offset` writer (`rag/chunker.py:371`);
- [ ] `delete_chunks` production test-double branch removal (`core/context.py:915-965`) +
  fix the tests to drive the transaction;
- [ ] `cache_size`/`_last_search_mode`/`_pending` cleanups; split compress/acompress
  duplication (`compression.py:152-318`) once R4-011 lands.

## Round 5 — test truth (red-first)

**Mandate: replacements are E2E or adversarial, not unit tests.** Use the real surfaces —
subprocess imports for purity checks, real routes/DB for TTL/load/ownership, fake-terminal
UI for rendering, attack fixtures for guards. Unit-level tightening is allowed only where
no runtime surface exists, with a written reason in the task notes.

### R4-100 · Unfalsifiable assertions
- Fix the three `... or True` sites, `evicted >= 0`, `result > 0` evictions, and the five
  `isinstance(list)`-only suites by asserting the deterministic expected values. Each
  tightened test must be run against the current code first (it may expose real bugs).

### R4-101 · Never-reaches-code (core flows)
- `connect()` test calls connect; `acquire()` property tests call `acquire`; rerank timeout
  test uses ≥2 candidates and asserts the timeout path; AH-018 test instantiates the agent
  and calls the probe; paused-job test calls `claim_due`; startup test exercises the real
  method; chaos DB-failure tests patch `add_chunks_batch`; `test_rpc_gateway` drives the
  route; controller purity becomes a subprocess import test.

### R4-102 · Wrong-seam / mock-enforced
- OAuth caps: assert the REAL transport's `_check_response` (inject at network level);
  delegation transport: use the real orchestrator seam; persona DB filters: assert SQL
  params; batch dedup: wire a real chunk + override so the dedup branch executes.

### R4-103 · Pre-fix pins
- RPC TTL tests: drive `gw.call()` and expire entries via clock; assert cleanup happens.
  luna-ultra: drop `or True` (may fail → fix catalog). bug-15 lazy config: subprocess import
  assertion. `_normalize_answer`: update expectation only with the locomo change (R4-092
  family) and add the decimal-preserving case.

### R4-104 · Environment coupling
- `test_repomap`: derive root from `__file__`; `live_db`: skip without DSN; live MCP test:
  fail (not skip) on timeouts beyond tool budgets; Windows tests: add a Windows CI job or
  mark expected-skip with a tracked issue; stop leaking `MACHINA_API_KEY` /
  `MACHINA_GATEWAY_NO_SCHEDULER` at import (monkeypatch/env-restore).

### R4-105 · Missing coverage
- assert caller-scope binding end-to-end; same-session read/write load race; cross-process
  flock contention for the auth stores; "no secrets in approval view" with the secret in
  argv; advisory-lock pool path with a real DSN; env-file round-trip after R4-043.

### R4-106 · Production seams
- Move `TestSoulSpecConformance` assertions into `tests/` and leave conformance.py as a
  fetchable module or delete it; sweep the ~20 "Tests only; kept as …" sites and document
  or remove each.

## Round 6 — docs

### R4-120 · Docs truth
- `OPERATIONS.md` self-migration claim; `RELEASE_READINESS.md` DNS claim; annotate the
  round-2 lockfile claim; update `CURRENT_ISSUES_CHECKPOINT.md` + `FEATURE_LEDGER.md`; README
  updates for changed behavior (effort vocabulary, `/provider remove`, keys allowlist,
  `max_iterations` decision).

## Round 7 — final gates

### R4-130 · Final verification
- Full backend (serial, `machina_test`) + UI check + ruff/format + bandit + `uv lock --check`
  + wheel/sdist + artifact check + `no-mistakes` + `adversarial-reviewer` over the round
  diff + hosted CI green; E2E gate rerun showing SSE `"error": 0`.

### R4-131 · Ledger update
- Update `CODEBASE_REVIEW_ROUND4_2026-10-14.md` with per-finding fix status (fixed /
  deferred-with-reason / refuted); mark deferred items explicitly; no claim beyond evidence.

---

## E2E & adversarial proof matrix (binding per task)

| Task | Required proof (primary) | Harness | Key assertions |
|---|---|---|---|
| R4-001 | guard run + count evidence | — | guard RED on current tree, GREEN after; wc before/after |
| R4-002 | E2E suites | existing suites | logs + triage table |
| R4-003 | adversarial | gate script | deliberately grown guarded file FAILS the gate in a scratch worktree, then reverted |
| R4-010 | E2E | H-SSE | ≥2-event stream: no error frame; scope active each step; cancel/close exits once |
| R4-011 | E2E | H-DB | compaction over real DB with a delegated `result` chunk: summary contains child text |
| R4-012 | adversarial | network-boundary capture | one user message, N `tool_result` blocks, alternation; 1/2/3-tool + is_error variants |
| R4-013 | adversarial | H-RACE | 1 POST per rotation, peer adoption, no resurrection post-logout; N≥5 repeats |
| R4-014 | adversarial | H-RACE | gemini variants + `update_project` interleave never resurrects an expired token |
| R4-015 | adversarial | H-RACE (device flow) | delayed flow completion after logout writes nothing |
| R4-016 | adversarial corpus | H-CORPUS | 0 leaks / 0 benign mangles across corpus+fuzz, batch and streaming |
| R4-017 | E2E | H-CLI | SIGINT mid-slow-tool: cancel ≤2 s, exit 130, claim released, no orphan PIDs |
| R4-018 | E2E | H-CLI + H-DB | headless full: routine tool executes with zero pauses (or documented refusal code) |
| R4-019 | E2E | H-SSE/H-GW | typed timeout message; cancel at requested bound; boundary values 59/60/90/300 |
| R4-020 | adversarial eval | H-GRADE | 4 attack fixtures → success=false; genuine fix → true; root `--test-path` case |
| R4-021 | E2E | network-boundary mock | malformed 200 → typed error; cache untouched; next retrieval works |
| R4-022 | E2E + UI | H-GW + H-UI | streamed outcome "denied"; UI renders denied (not success); legacy `isError` path intact |
| R4-023 | adversarial UI | H-UI corpus | no control bytes in output; multiline rows split; width math intact |
| R4-024 | E2E | H-UI scripted + gateway assert | after remove: session re-pinned; next turn routes to the fallback |
| R4-025 | E2E | agent loop + fake provider | cap=1 → exactly 1 provider call; typed stop payload; streaming/delegation bypass attempts fail |
| R4-026 | E2E | H-DB + H-FS | `workspace_root≠home`: index→search returns the right file; symlink/`../`/planted-file attacks fail |
| R4-027 | adversarial | temp-HOME fixtures | sibling/case/suffix variants not matched; exact + ancestor matched |
| R4-028 | E2E | delegation harness | missing artifact → forced write attempt observed (or claim deleted) |
| R4-029 | E2E | delegation + injected DB failure | row ends `error` (never `running`); one `_record_end` |
| R4-040 | adversarial | H-RACE (chatgpt) | pre-logout tokens cannot recreate the store; N≥5 |
| R4-041 | E2E | login+refresh interleave | login succeeds during benign rotation; code not wasted |
| R4-042 | adversarial echo | real tool-result/log paths | claude/gemini/copilot tokens redacted everywhere |
| R4-043 | E2E | secrets flows (setup/CLI/gateway) | backslash/quote values round-trip; strict xfail converted to passing |
| R4-044 | adversarial FS | H-FS | pre-created symlink untouched; crash injection never leaves an empty `.env` |
| R4-045 | config matrix E2E | keyring boundary stub | flag on → plaintext allowed, unknown still rejected; flag off → strict |
| R4-046 | E2E | gateway + CLI secrets | lowercase line updated/removed in place |
| R4-047 | E2E | network-boundary mock | non-loopback `http` refused (or explicit opt-in + warning) |
| R4-048 | E2E | wheel-layout simulation | no site-packages path created or used |
| R4-049 | adversarial | H-RACE + chmod injection | contended lock → typed timeout within bound; fd closed on chmod failure |
| R4-060 | adversarial | H-ATTACK | git grep/revspec/backup names must-pend; benign git reads stay routine |
| R4-061 | adversarial | H-ATTACK | flag calibration proven both directions |
| R4-062 | adversarial FS | H-FS | unreadable dir → review required |
| R4-063 | E2E logging | real logger + sink capture | structured secret redacted; redactor failure masks, nothing escapes |
| R4-064 | E2E | H-GW long turn | eviction markers present; tool totals correct or explicitly marked |
| R4-065 | E2E | compressed approval timeout | resolution frame emitted; H-UI retires card + footer |
| R4-066 | adversarial timing | resolve-at-deadline race N≥5 | durable approved honored; no replay from stale state |
| R4-067 | E2E | real actions + audit rows | target info present; no raw secrets |
| R4-068 | E2E | durable failure injection | no memory-only authoritative state; no orphan pending |
| R4-080 | E2E per bullet | H-CLI/H-DB/H-FS | per-bullet RED cases (yaml root, msg, sentinel race, NUL) |
| R4-081 | E2E | H-CLI + H-DB | same DB across MCP tools |
| R4-082 | E2E contract | gateway + UI harness | effort vocab on both surfaces; single envKey name; removal survives restart |
| R4-083 | E2E | standalone handshake | token disclosed; methods work |
| R4-084 | E2E | H-SSE | canonical turnId; cross-transport correlation |
| R4-085 | adversarial | OAuth fixtures | per-bullet must-fail/must-pass (dead guard, ports, preflight wait, state, to_thread) |
| R4-086 | adversarial | pattern corpus | IPv4-style accepted; explosive rejected; rep budget honored |
| R4-088 | E2E/adversarial | H-DB + H-FS | per-bullet (empty reindex, decorators, fences, dual-failure, digest, zero-size) |
| R4-089 | adversarial | H-CORPUS + H-DB | per-bullet |
| R4-090 | adversarial | H-FS + tool E2E | per-bullet (paging, marker, ENOSPC atomicity, persona, schemas) |
| R4-091 | adversarial | install fixtures | per-bullet (corrupt hub, delete/reinstall, huge symlink, Windows branch, URLs, "go ahead") |
| R4-092 | eval | H-GRADE/H-EVAL | per-bullet (attacks, contamination, timeouts, cleanup, `-O`) |
| R4-093 | E2E | H-CLI | per-bullet |
| R4-094 | E2E UI | H-UI | per-bullet |
| R4-095 | adversarial | CI/docker fixtures | per-bullet |
| R4-096 | E2E | affected suites | behavior unchanged; E2E proofs still drive the seams |
| R4-100 | adversarial | per-item | tightened assertions fail pre-fix wherever a real bug exists |
| R4-101 | E2E | H-* (subprocess imports, real routes, real DB) | per-item |
| R4-102 | E2E | real seams | per-item |
| R4-103 | E2E/eval | real call paths + H-EVAL | per-item |
| R4-104 | adversarial/CI | CI matrix | per-item |
| R4-105 | E2E | H-RACE/H-DB/H-UI | per-item |
| R4-106 | E2E | subprocess/collection checks | per-item |
| R4-120 | docs | — | every claim matches current code |
| R4-130 | gate | all harnesses | full gate list green |

## File-collision groups (serialize edits within a group)

- `core/agent.py`: R4-010, R4-019, R4-022, R4-025 (order: 010 → 019 → 022 → 025).
- `security/chatgpt_auth.py`: R4-040, R4-041, R4-049.
- `security/claude_auth.py`: R4-013, R4-042, R4-049.
- `security/env_file.py`: R4-043, R4-044, R4-046, R4-048 (order: 044 → 043 → 046 → 048).
- `memory/redaction.py`: R4-016, R4-089 (streaming part).
- `permissions/policy.py`: R4-060, R4-061, R4-062.
- `permissions/broker.py`: R4-065, R4-066, R4-067.
- `tools/rag.py` + `rag/loaders.py`: R4-026, R4-088 (R4-026 first).
- `ui/src/app.ts`: R4-024, R4-065(UI), R4-094.
- `tests/test_api_bugfixes.py`: R4-010, R4-103 (order: 010 → 103).

## Suggested parallel waves (respecting collisions)

- **W0:** R4-001, R4-002, R4-003.
- **W1:** R4-011, R4-012, R4-016, R4-020, R4-021, R4-023, R4-024, R4-027, R4-029
  (independent files).
- **W2:** agent.py chain (R4-010 → 019 → 022 → 025), auth chain (R4-013 → 014 → 015),
  R4-017, R4-018, R4-026.
- **W3:** Round 2 + Round 3 in parallel per collision groups.
- **W4:** Round 4 clusters; Round 5 test tasks interleaved per-file; Round 6 last (after
  decisions land), Round 7.
