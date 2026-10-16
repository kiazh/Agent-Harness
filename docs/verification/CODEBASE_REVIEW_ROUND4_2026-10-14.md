# Code review — round 4 — 2026-10-14 (full-codebase read)

Reviewed revision: `cdc5232` (working tree quiesced for these paths; the tree moved
`703ad81 → cdc5232` during the review and was edited concurrently between 15:52–16:15 EDT).
All line numbers are as of each read snapshot. Read-only application review; no source was
changed during the review itself.

Method: every first-party line was read end-to-end by partitioned read agents — backend
`machina/` A1–A15 (~70.4k lines), TUI `ui/src`+`ui/test` U1–U3 (~20.1k), tests T1–T16
(~82k), infra I1 (`install.sh`, Docker, CI, `tools/`, `pyproject`) — each with the
debug/review skill set (`diagnose`, `code-reviewer`, adversarial verification, coverage /
senior-qa, security-review, python-patterns, error-handling, agent-introspection-debugging)
and a mandatory per-finding false-positive check. A verification pass then re-read and
attempted to disprove every candidate P1 plus 12 representative test-quality claims
(4 adversarial verifiers, V1–V4), and audited prior verification documents against the
current code. Coverage statements with per-file line counts accompany every agent report;
the four subagent runs that stalled at the harness level were replaced and their slices
fully re-read.

Verification legend used below:
- **V1–V4** = independently re-verified in the verification pass; C = CONFIRMED,
  P = PARTIALLY CONFIRMED (scope/mechanism corrected here), R = REFUTED (do not fix),
  S = stale.
- No verification mark = single-pass review with author FP check; runtime-repro items are
  listed in the per-slice report sections.

Tally: 15 product/UI P1 findings (verified), ~35 P1-class false-confidence test findings,
~90 P2s, ~70 Q notes. 2 candidate P1s refuted by verification; 6 findings corrected in
scope/mechanism. 3 stale claims found in prior docs.

---

## 1. Verified P1 findings — the fix-first list

| # | Finding | Files | Verification |
|---|---|---|---|
| 1 | **Every multi-event SSE turn emits a spurious error frame.** Each `__anext__` runs in a new asyncio Task while `turn_scope()`'s ContextVar token is set in the first task's context; the reset at stream end raises `ValueError` (cross-Context reset), surfacing as an error frame after the last delta. Corroborated by the repo's own E2E proof: **every recorded SSE turn shows `"error": 1`** before `done`. | `machina/core/agent.py:122-147`, `machina/core/session_mode.py:87-105`, `machina/api/routes/streaming.py:70-89,177,353`; `docs/verification/proofs/2026-10-14-e2e-gate.md:231-277` | V1-1 CONFIRMED |
| 2 | **Compaction summaries silently drop delegated-agent / job output.** Renderer reads `payload["result"]`; both writers store the text under `payload["content"]`; summary shows `[Result] ok: ` with an empty body. Intended fallback exists in `assembler.py:178-184`. | `machina/core/compression.py:823-828` vs `machina/core/orchestrator.py:417-423`, `machina/core/scheduler.py:973-979` | V1-2 CONFIRMED |
| 3 | **Anthropic/Claude multi-tool turns produce consecutive `user` roles** — one separate user message per tool result; a ≥2-tool batch violates the Messages API alternation contract. No coalescing exists anywhere on the path. | `machina/core/provider/anthropic.py:96-108`, `claude.py:60-64`; `prompt.py:161-191`, `agent.py:1193-1201` | V1-3 CONFIRMED |
| 4 | **Claude token store has no lock, no generation fence, plain-`unlink()` logout.** (a) Concurrent expired-token turns double-submit the single-use rotating refresh token (invalid_grant; possible family invalidation); (b) a refresh completing after logout recreates `claude.json`. Gemini shares the gap (plus a read-modify-write `update_project` clobber); Copilot shares the plain-unlink logout. ChatGPT already implements the intended flock+generation pattern. | `machina/security/claude_auth.py:360-452,570-652`; `machina/cli/auth.py:558-578`; `gemini_auth.py:215-222,356-446`; `copilot_auth.py:133-166` | V1-4 CONFIRMED (found independently by A7 and A12) |
| 5 | **Redaction under-match with tail leak.** `generic_secret` (alnum-only value class) runs before the quoted-key pattern; values containing `.`/`+`/`/`/`~` either never match (`token`/`secret` names) or lose only their leading run while the **tail persists** — the earlier substitution destroys the quote adjacency the rescue pattern needs. | `machina/memory/redaction.py:224-242,275-310` | V1-5 CONFIRMED |
| 6 | **Ctrl+C cannot stop a running `machina chat` turn.** The turn runs in a worker thread; `shutdown(wait=True)` re-blocks the main thread until the turn completes — the agent keeps calling tools and spending tokens after abort. (KI is delivered; the wait re-blocks.) | `machina/cli/__init__.py:69-74,397` | V2-6 CONFIRMED |
| 7 | **Headless `--mode full` is unresolvable.** CLI creates the session with `execution_mode='full'` but never activates the full-mode grant (gateway/REST do); policy demands a live grant → every governed tool call pauses; headless cannot approve → exit 3. | `machina/cli/__init__.py:196-244`; `machina/gateway/server.py:559-565`; `machina/permissions/policy.py:736-748` | V2-7 CONFIRMED |
| 8 | **Terminal timeout contract break.** Schema advertises up to 300 s; the loop hard-cancels at 60 s and the model receives the literal `Error: ` (argument-less `TimeoutError`). Terminal's own "Command timed out after Ns" is unreachable for exactly the advertised 60–300 s range (reachable ≤ ~59 s). | `machina/core/agent.py:214-225,1035-1092`; `machina/tools/terminal.py:265-268,439-440` | V1-7 PARTIAL (scope corrected) |
| 9 | **Head-to-head grader is bypassable.** "Tests unchanged" hashes only the chosen tests dir; a root-level `pytest.ini`/`pyproject.toml`/`conftest.py` written by the agent (`addopts = --deselect …`) keeps hashes unchanged and yields exit 0 + passed>0 → `success=true` without any fix. Also: a root-level `--test-path` guarantees `success=false` for a correct fix. | `machina/research/head_to_head.py:482-483,586-610` | V1-8 CONFIRMED |
| 10 | **Embedder zero-vector poisoning.** A 200 response missing `data` yields `[]` → `[0.0]*dimensions`, **cached**, and returned by memory as a successful embedding (no exception) — dense retrieval silently poisoned until LRU eviction. Single-text path only; the batch path raises. | `machina/rag/embedder.py:265-270,146-177`; `machina/memory/embeddings.py:39-44` | V1-10 CONFIRMED |
| 11 | **Denied tool actions render as success in the TUI.** Streamed denials carry `isError=false` and empty `outcome` (the `"denied"` outcome exists in the data model — `agent.py:257-258,1305` — but is not propagated on the stream), so the transcript paints the green checkmark on "The action was NOT executed." | `ui/src/transcript.ts:194`; `machina/core/agent.py:1155` vs `:257-258,1305`; `machina/core/turn_engine.py:364` | Direct check CONFIRMED |
| 12 | **Menu/Picker render untrusted labels unsanitized** — the only untrusted-text UI path skipping `sanitizeDisplay`; recall previews (raw tool output) and model IDs can carry ANSI/OSC/newlines into the terminal (frame corruption, terminal state changes). | `ui/src/components/Menu.ts:60,94-103` (0 sanitize calls), `ui/src/components.ts:317` | Direct check CONFIRMED |
| 13 | **`/provider remove` can leave the session pinned to the deleted provider** — the re-pin guard compares a stale session snapshot (never refreshed after `/provider`/`/models`); next turn fails "Unknown provider". | `ui/src/features/settings.ts:223-230`; `ui/src/app.ts:783`; `machina/gateway/features/providers_feature.py:276-314` | Mechanism confirmed; runtime repro pending |
| 14 | **`max_iterations` is dead configuration** — validated (1–50), persisted, exposed via soulspec/API — never read; both loops are `while True`. Inline-documented as "Deprecated… ignored", so it is intentional-but-user-invisible dead surface, not an undiscovered oversight. | `machina/core/agent.py:277-290,1606,1820`; `machina/core/config.py:90-92` | V2-10 CONFIRMED |
| 15 | **`index_document` path divergence.** The tool's loader resolves against `machina_home`/cwd while the permission broker approved against `workspace_root`; when those differ (explicitly configured case), approved absolute paths are rejected and relative paths can index a *different* file. | `machina/tools/rag.py:192`; `machina/rag/loaders.py:109-116,238-244`; `machina/permissions/policy.py:662-680` | V1-6 PARTIAL (scope corrected) |

Additional product bugs surfaced via test inspection (verified where marked):

- **`machina uninstall` shim matcher is substring containment** — a shim pointing at a
  sibling path (`/Users/x/machina-old`) matches app_root `/Users/x/machina` and is deleted
  (`machina/cli/__init__.py:1928-1930,2183-2197`). V2-11 CONFIRMED.
- **Artifact-contract "backtrack" is a no-op** — `should_write_first(0, True)` return is
  discarded; the advertised forced synthesize/write attempt never happens
  (`machina/core/orchestrator.py:402-414`; `progress.py:115-124`). V2-12 CONFIRMED.
- **Delegation rows can stay `running` forever** when the child-ledger read fails
  (`orchestrator.py:348-357`; no reconciliation anywhere). T12, single-pass.
- **Tool-call undercount after trace ring overflow** — `turn_report` counts only retained
  spans (`observability/traces.py:725-729`). T9, single-pass.
- **Tree currently RED:** god-file ratchet violated — `machina/core/provider/reasoning.py`
  68 lines vs 61 ceiling, provider package 5508 vs 5501; `test_architecture_layers.py` has
  2 failing guards. `cdc5232` bypassed the gate. Verified by direct `wc` count. T3-2.

---

## 2. Cross-cutting themes

1. **False confidence in tests is the largest single theme.** ~35 P1-class tests cannot fail
   for the regression they name: `... or True` tautologies (3 files), tests that never call
   the code under test (`connect()`, `acquire()`, probe tools, `claim_due` copied as SQL),
   tests patching a seam the run path doesn't use (`add_chunk` vs `add_chunks_batch`), mocks
   that enforce the behavior themselves (OAuth caps by stub), assertions pinning pre-fix
   behavior (RPC-gateway TTL), and mock-echo suites. 12 adversarially spot-verified: 10
   confirmed, 2 partially confirmed, 0 refuted.
2. **Credential-lifecycle fencing was fixed for ChatGPT only** (Rounds 3/6). Claude/Gemini/
   Copilot lack lock+fence; the ChatGPT fence has two residual windows (pre-logout refresh
   tokens; benign peer rotation false-cancels a valid login); the redaction registry covers
   ChatGPT tokens only.
3. **Workspace-root vs machina-home/cwd divergence** appears independently in at least two
   remaining places (index_document loader, FileLoader base) — one canonical resolver is
   missing.
4. **Error surfaces degrade to ambiguity:** bare `Error: ` on timeouts; dead "oauth is not
   configured" guards; `config-set`/`mode` report success while env-pinned values win; MCP
   child silently on a different DSN; memory approval gate documented but unwired.
5. **Policy-layer gaps:** `git grep --untracked` bypasses the recursive-read review
   (confirmed); `HEAD:credentials` revspec misses sensitive_path; `.env~`-style backups
   escape the dotenv matcher; `-c/-C/-o` over-reject benign flags (fail-closed noise).
6. **TUI honesty & robustness:** denied→success checkmark; approval **expiry** never reaches
   the UI; re-resuming the visible session wipes live output and drops running-tool
   completions; unsanitized menu labels; Kitty-protocol Ctrl+S.
7. **Dead/duplicated machinery:** tool-result TTL cache unreachable for every shipped tool;
   `_RpcGateway` lock/TTL dead complexity; `ContextManager.cache_size` dead;
   `overlap_offset` write-only; test-double detection inside production `delete_chunks`.

---

## 3. P2 findings by subsystem (compressed; full detail in the per-slice sections below)

### Security (`machina/security/`) — A7
- ChatGPT refresh started with pre-logout tokens still resurrects the store (residual of
  R3-2 in a distinct window) — `chatgpt_auth.py:541-568,627-640`.
- ChatGPT login exchange false-cancels on a benign peer rotation (over-match of the fence;
  the one-time code is already consumed) — `chatgpt_auth.py:748,804-815`.
- Redaction registry: Claude/Gemini/Copilot token values never registered —
  `claude_auth.py:154-187` etc.
- `.env` escape round-trip corrupts values containing `\` or `"` — `env_file.py:157-174`
  (strict xfail pins it; flip the xfail with the fix).
- `.env` writer: predictable temp name, no `O_EXCL`/`O_NOFOLLOW`, no fsync —
  `env_file.py:236-275`.
- `MACHINA_OAUTH_ALLOW_PLAINTEXT_KEYRING=1` behaves inverted and widens the backend
  allowlist — `oauth_keys.py:58-75`.
- Lowercase/alias env keys invisible to updater/remover — `env_file.py:177-183`.
- Non-loopback plaintext `http://` base URLs receive bearer keys in cleartext —
  `providers.py:168-177`.
- `.env` fallback resolves inside `site-packages` on wheel installs —
  `env_file.py:51,130`.
- Blocking `flock` without timeout in both store locks — `chatgpt_auth.py:196`,
  `providers.py:92`; fd leak if `chmod` fails before `fdopen` (6 sites).
- `save_tokens` can persist a null store and clobber a valid one (public-API misuse).

### Permissions / observability — A8
- `git grep --untracked/--no-exclude-standard` reads ignored credential files without
  approval (confirmed — V2-4).
- `git show HEAD:credentials` revspec misses `credential_access` — `policy.py:187-218`.
- `.env~`, `.env-backup`, `credentials~`, `.netrc.bak` name-family gaps — `policy.py:209-215`.
- `-c/-C/-o` escape-arg check over-rejects benign flags (`wc -c`, `ls -C`, `grep -C 2`) —
  `commands.py:58-86`.
- `_tree_contains_component` fails open on unreadable directories — `policy.py:426-441`.
- Trace rings evict in-flight parent turns/spans with no marker —
  `observability/traces.py:255-258,324-325`.
- `safe_logging` redacts only top-level strings; redactor call unguarded —
  `observability/safe_logging.py:12-29`.
- Approval-timeout race reports "expired" while durable truth is "approved" —
  `broker.py:250-256`, `store.py:575-611`; audit rows for allowed actions lack
  target/argv — `broker.py:344-369`.

### Gateway / API / process — A5, A6
- Malformed `config.yaml` root crashes every CLI command (`.items()` outside try) —
  `config.py:576` (V2-1 CONFIRMED).
- MCP `machina_chat` child loses `DATABASE_URL`/`MACHINA_ENV_FILE`; same server's tools can
  use different databases (V2-2 CONFIRMED).
- `run_bounded` leaks `ValueError` on NUL bytes despite "spawn never raises" —
  `child_process.py:207-230`.
- `Database.connect()` check-then-act race leaks a connection pool —
  `db/connection.py:35-52`.
- CLI recovery message says `machina db init`, which does not exist —
  `cli/__init__.py:220`.
- `config.set reasoning_effort` two disjoint validator vocabularies; `models.setEffort`
  `minimal` → INTERNAL_ERROR — `server.py:693-703`, `providers_feature.py:177-197`.
- `apiKeyEnv` vs `envKey` cross-boundary mismatch — `providers_feature.py:299-306`,
  `protocol.ts:183`, `settings.ts:257`.
- Gateway `providers.remove` fallback not persisted (restart restores removed provider) —
  `providers_feature.py:292-298`.
- `python -m machina.gateway` mints an undisclosed token → every method returns
  unauthorized — `gateway/__main__.py:62-67`, `server.py:224-230`.
- REST SSE exposes the turn's ownership claim token as `turnId` — `streaming.py:133-135`.

### Connectors / OAuth — A10
- OAuth "not configured" guard is unreachable (`x or {}`) — `oauth.py:154-156` (+4 sites).
- Invalid-port endpoints pass validation; `preview` raises uncaught `ValueError` —
  `models.py:178-190`, `oauth_network.py:58-72`.
- Pattern prescreener over-rejects bounded nested counts (IPv4-style schemas) —
  `policy.py:118-148` (validator false positive).
- In-flight refresh preflight hard-fails concurrent callers (confirmed — V2-5).
- Non-ASCII `state` → `TypeError` in callback handler — `oauth_callback.py:214-222`.
- Synchronous index reads on the event loop — `oauth.py:761,984`.

### Memory — A9
- `bearer token` plain-English over-redaction — `redaction.py:113-117`.
- SSN over-match (`\b\d{9}\b`), 17–19-digit cards miss, `sk-` matches inside identifiers —
  `redaction.py:73,202,212,280`.
- Streaming redactor holds >40-char digit runs indefinitely, quadratic re-scan —
  `redaction.py:491-618`.
- Cosine dedup inert on the real path (candidates built without embeddings) —
  `consolidator.py:296`.
- Memory approval gate documented but wired to nothing — `approval.py` (no production
  callers).
- `user_profile` write/read asymmetry; `update_access` vs `batch_update_access`
  inconsistency; `shared_bus` retry gap.

### Tools / RAG / skills / research — A11, A13, A14, A15
- `read_file` rejects files over `max_size` before offset/limit paging —
  `tools/file.py:287-292`; silent truncation at `limit` — `:315-317`.
- `write_file` truncates in place; mid-write ENOSPC destroys the original —
  `tools/file.py:357-405`.
- `harness_status` conflates persona-resolution failure with "no persona" —
  `tools/harness.py:165-203`.
- Re-indexing a now-empty document leaves old chunks searchable —
  `rag/pipeline.py:318`.
- `split_code` detaches decorators from their functions — `rag/chunker.py:188-233`.
- `FileLoader` ignores `workspace_root` (`loaders.py:109`) — same family as P1 #15.
- Dual-branch search failure masked as "No results found" — `rag/search.py:100-137`.
- RepoMap cache is stat-only; sha256 write-only — `repomap/cache.py:33-43`.
- `delete_skill` removes only SKILL.md; half-deleted installs block reinstall —
  `skills/registry.py:901-916`, `repo_install.py:915-936`.
- Unbounded pre-cap reads (`registry.py:337-350`, `soulspec/schema.py:76`); Windows
  repo-activate impossible (non-best-effort dir fsync) — `repo_install.py:993-1008`.
- "go ahead" activates the coding workflow (bare tokens in `_TECHNOLOGIES`) —
  `skills/workflow.py:126-149` (V2-8 CONFIRMED; classifier false positive).
- LoCoMo `_normalize_answer` collapses decimals: `35` scores perfect vs gold `3.5` —
  `research/locomo.py:596-602` (V2-9 CONFIRMED).
- Head-to-head `--runs-dir` at fixture parent contaminates the fixture;
  benchmark can hang forever and always exits 0; cleanup aborts on first failure;
  `-O` silently skips measured calls — `head_to_head.py`, `benchmark.py`,
  `workflow_benchmark.py`, `locomo.py` (per A15 report).

### CLI — A12
- `config-set`/`mode` are no-ops when the key is pinned in `.env`/env (setup-created
  installs pin model/provider) — `cli/__init__.py:1498-1519,492-515` vs gateway's env sync
  `server.py:706-720`.
- User/DB/LLM strings rendered as Rich markup → `MarkupError` crash or swallowed text —
  `cli/output.py:68-129`, `cli/__init__.py` interpolating prints.
- `fork`/`growth import*`/`export*` raw tracebacks — `cli/__init__.py:646-666`,
  `cli/growth.py:97-169`.
- `providers models --refresh` overwrites a good cache with `[]` on empty success —
  `cli/providers.py:375-404`.
- `setup` crashes on invalid `MACHINA_ENV_FILE` — `cli/__init__.py:1298`.
- `repl --mode` not validated — `cli/__init__.py:400-408`.
- `doctor` never exits non-zero; negative `-n` limits → raw tracebacks; `provider add`
  exits 1 when only the model fetch failed (per A12 report).

### TUI — U1, U2, U3
- Re-resuming the visible session mid-turn erases live output and drops running-tool
  completions (`app.ts:369-401`); hidden-session failure notices deleted at completion
  (`tail.ts:113-124`); approval expiry never retires the card / footer
  (`app.ts:1279-1291`, `broker.py:250-256`); stale `[image N]` literals after Ctrl+C + Esc
  Esc (`app.ts:822-827,993-998`).
- Embedded newlines corrupt single-row renderers (`ToolTurn.ts:58-65`,
  `ApprovalCard.ts:249-252`); composer hides draft rows without the documented marker
  (`components.ts:211-220`); mouse mapping ignores window start (`components.ts:184-200`);
  Ctrl+S broken under Kitty protocol (`Form.ts:93,241`); cobalt2/solarized selection rows
  below 4.5:1 (`design/theme.ts:637-641`).
- `/mode revoke` advertised but nonexistent (`mode.ts:57`); `/delegate` client timeout
  equals the gateway budget (`agents.ts:182`, `app.ts:327`); `/keys` suggests keys the
  gateway refuses (`keys.ts:22-39`, `secrets.py:35-55`); job prefix pagination dead
  (`jobs.ts:15-38`); MCP OAuth not cancelled on transient failures
  (`mcp-oauth.ts:174-204`); protocol type drift (`protocol.ts:80-84,487-490`).

### Infra — I1
- Lockfile "authoritative" but never verified in CI; Docker-DB reuse credential mismatch;
  mutable action/base-image tags; compose `./machina` mount inert (stale-code hazard);
  install.sh raw error on bad `MACHINA_ENV_FILE`; telemetry JSON ships in wheels;
  `e2e_gate.py` uses ambient DB config with no `--db-url` guard.

---

## 4. False-positive ledger (verification output)

**Candidate P1s refuted by adversarial verification — do NOT fix (there is no bug):**

1. **"Absolute-path symlink bypass in `process.exec`"** — REFUTED. `.resolve()` wraps the
   entire conditional (`permissions/policy.py:563`), so absolute operands are resolved and
   the sensitive component (`.env`, `credentials`, …) is caught exactly as for the relative
   spelling. No asymmetry exists at any revision. (Original A8-1 claim removed.)
2. **"LoCoMo mixed-mode crash"** — REFUTED. The hybrid branch appends the sparse ranking to
   `keyword_rankings` too (`research/locomo.py:404`); every evidence question contributes
   exactly one entry, so `evaluate_rankings`'s length check cannot raise in a mixed run.
   Only a component-reporting limitation remains (dense/hybrid component scores skipped in
   mixed mode). (Original A15-2 claim removed.)

**Claims corrected in scope/mechanism (kept, re-scoped):**

- index_document divergence (P1 #15): requires `workspace_root` explicitly configured to a
  value differing from `machina_home`/cwd; both failure modes hold under that configuration.
- Terminal timeout (P1 #8): inner "Command timed out after Ns" is reachable for requested
  timeouts ≤ ~59 s; the contract break is the 60–300 s advertised range.
- Ctrl+C: `KeyboardInterrupt` can be delivered while blocked in `.result()`; it is the
  subsequent `shutdown(wait=True)` that re-blocks until completion — same user-visible
  conclusion.
- `max_iterations`: inline-documented as deprecated/ignored, so "silently" is inaccurate;
  the user-invisible dead configuration surface is the surviving issue.
- RPC-gateway TTL tests: the cleanup *helper* is genuinely tested; the `gw.call()` call-site
  (the actual leak-prevention fix) is not driven by any test.
- `connect()`-without-DSN test: the DSN-construction assertion is real (not tautological);
  the `connect()` path itself is unguarded by the test.

**Refuted during the read itself (per-slice REFUTED sections; ~60 items):** representative
examples — DirectProvider base_url path merge, per-family cache identity, RRF weight math,
reranker fallback/validation paths, FileLoader traversal defenses, `delete_chunks`
double-delete re-run, asyncpg `Record.get`/mock semantics, `_approval_view` leak,
provider-store reserved-name policy, workflow `MACHINA_API_KEY` interpolation, DNS-pinning
and redirect defenses, `importorskip("mcp")` no-op, property-test health-check suppression,
in-tree checkpoints' "verified" rows re-checked present, and every R3/R6 item marked fixed
at HEAD (R3 #4–#12, R6-1/6-2/6-3) — none re-reported.

---

## 5. Prior-docs audit (claims vs current code)

Verified as still true: 13/13 sampled `BUGFIX_AUDIT.md` "Fixed behavior" bullets; 3/3
sampled `CURRENT_ISSUES_CHECKPOINT.md` rows (rotated-token redactor registration, MCP
discovery credential refusal, grant-rotation retry); 4/4 `CODEBASE_REVIEW_ROUND3` "Confirmed
earlier fixes" (failed-publication compensation, CLI ownership-loss cancellation, qualified
embedding IDs, numeric pilot golds). `RELEASE_READINESS`/`OPERATIONS` headline claims mostly
supported (CI gating, coverage threshold 60, backup/restore unverified by design, installer
consumes `main`).

**Claims that do NOT hold today (false/stale):**

1. `OPERATIONS.md:54-57` — "The gateway does not self-migrate on startup": false at HEAD;
   the gateway/CLI/runtime apply additive runtime migrations at startup and fail closed
   (`gateway/server.py:501-511`, `db/connection.py:196-209`, `cli/__init__.py:218`,
   `core/runtime.py:112`).
2. `RELEASE_READINESS.md:9` — "DNS-rebinding protections remain outside the release scope":
   stale; the pinned transport is implemented and in use
   (`connectors/http_transport.py:75-144`).
3. An untracked round-2 review doc's "`uv.lock` lacks embeddings/rerank extras": false
   against the current lock (`uv.lock:1289-1339`).

Execution-result claims (pass counts, hosted CI outcomes) remain unverified historical
evidence — not falsified, not re-runnable read-only.

---

## 6. Test-quality P1-class detail (the false-confidence set)

12 claims adversarially re-verified by V3 (10 CONFIRMED, 2 PARTIAL, 0 REFUTED). The
remaining P1-class items are listed per-file in the T1–T16 slice reports; summary of the
strongest:

- **Unfalsifiable assertions:** `or True` in `test_repo_install.py:234`,
  `test_integration_permissions.py:91`, `test_gateway_providers.py:77`; `evicted >= 0`
  (`test_chaos_extended.py:789`); `result > 0` eviction asserts; `isinstance(results, list)`
  across five retriever/consolidator tests.
- **Never reaches the code:** `connect()` (`test_review_round2_fixes.py:53`), `acquire()`
  (`test_property_based.py:469-505`), rerank timeout with one candidate
  (`test_memory_bug_fixes.py:269`), AH-018 probe never invoked
  (`test_handoff_regressions.py:471-519`), `claim_due` re-implemented as SQL
  (`test_audit_remediation.py:1016-1071`), `startup()` replaced by a hand copy
  (`test_production_readiness_p2.py:210-243`), wrong seam `add_chunk` vs
  `add_chunks_batch` (`test_chaos.py:619-688`), `test_rpc_gateway.py` never calling the
  route, controller purity vacuous under suite ordering
  (`test_controller_triage.py:269`), learning-cap test disabled by default config
  (`test_learning_review.py:857-909`), mixed read/write load test with disjoint session
  sets and a phantom cache (`test_load_concurrency.py:316-379`).
- **Mock-enforced behavior:** OAuth size/compression caps enforced by the stub
  (`test_connectors_oauth_network.py:31-53`); delegation "early return" fully mocked;
  persona-store filters never asserted; batch-dedup test returns "empty" before extraction.
- **Pins pre-fix behavior:** RPC TTL tests assert persistence
  (`test_api_bugfixes.py:347-448`); `_normalize_answer(2024.5) == "20245"` pins the decimal
  collapse (`test_fm6_locomo.py:23-24`).
- **Environment coupling:** `REPO_ROOT` was hardcoded to an absolute maintainer-home path in `test_repomap.py` (since fixed);
  `live_db` errors instead of skipping without a DSN; live MCP round trip converts hangs to
  skips; no Windows CI despite Windows-only path tests; env vars set at import
  (`MACHINA_API_KEY`, `MACHINA_GATEWAY_NO_SCHEDULER`) leak for the session.
- **Production test-code pattern:** `soulspec/conformance.py` ships
  `TestSoulSpecConformance` with the real assertions (exported in the public API, never
  collected as tests); ~20 production locations carry "Tests only; kept as …" seams.

---

## 7. Coverage, limits, and runtime-repro backlog

- Coverage: every first-party line read end-to-end; per-file line-count tables and coverage
  statements in each slice report; four harness-level stalls were replaced and fully
  re-read. No unreadable files.
- Limits: source inspection + targeted static verification; no app/tests/DB/network were
  executed during the read or the verification pass. Items marked "Needs runtime repro" in
  the slice reports remain unconfirmed at runtime; the highest-value ones are: the SSE error
  frame against a live server; Claude concurrent refresh with a mocked token endpoint; the
  chaos/load tests with `MACHINA_TEST_DATABASE_URL` set; TUI visual symptoms; the two red
  architecture guards (already arithmetically verified).
- Line-number drift: the tree was edited concurrently during the review
  (`703ad81 → cdc5232`). Re-confirm every line reference against the current HEAD before
  editing; the API/SSE and auth areas are the most likely to have moved.

---

## 8. Fix status (R4-131, 2026-10-15)

Every Round-4 task below is committed on `round4-fixes` with its proof bundle under
`docs/verification/proofs/codebase_review_round4_2026-10-14/` (RED-on-unfixed
evidence included). Commit hashes are post-rewrite (Gemini-secret strip); run
`git log --oneline` for the live series.

| Task | Status | Commit / note |
|---|---|---|
| R4-001 ratchet trim | FIXED | f9f9a8a |
| R4-002 baseline triage | DONE | stage ledgers |
| R4-003 gate architecture + SSE in CI | DONE | CI `pytest tests/` sweep includes both; verified |
| R4-010 SSE error frame | FIXED | 13d5f5c |
| R4-011 compaction result text | FIXED | 197e71c |
| R4-012 anthropic coalescing | FIXED | 1e37bc1 |
| R4-013 Claude fencing | SUPERSEDED | flow removed (vendor terms) 959947e |
| R4-014 Gemini fencing | SUPERSEDED | flow removed 959947e |
| R4-015 Copilot logout fence | FIXED | bdb3a8b |
| R4-016 redaction precision | FIXED | 970bc57; fixture de-literal 1b01d98 |
| R4-017 Ctrl+C | FIXED | 13d5f5c |
| R4-018 headless full | FIXED | 13d5f5c |
| R4-019 terminal timeout | FIXED | 13d5f5c |
| R4-020 grader integrity | FIXED | acaca11 |
| R4-021 embedder zero-vector | FIXED | 39e9f88 |
| R4-022 denied→denied | FIXED | 33c763d |
| R4-023 menu sanitization | FIXED | 33c763d |
| R4-024 provider-remove pin | FIXED | 6797646 |
| R4-025 max_iterations | FIXED (REMOVE, owner) | bceadb4 |
| R4-026 workspace resolver | FIXED | 9fe1956 |
| R4-027 uninstall matcher | FIXED | 13d5f5c |
| R4-028 artifact backtrack | FIXED (delete branch) | 838679e |
| R4-029 delegation recovery | FIXED | 838679e |
| R4-040/041/049 chatgpt fences | FIXED | bdb3a8b |
| R4-042 token registration | SUPERSEDED | claude/gemini flows removed |
| R4-043…049 env/secrets | FIXED | 2d1a6ec |
| R4-060…068 policy/approvals | FIXED | 03f5fa5; R4-066b durable-read 8e4f988 |
| R4-080/082/083 config/gateway | FIXED | 3130a7a |
| R4-081/084 MCP DSN + SSE turnId | FIXED | 91fb8be |
| R4-085/086 connectors | FIXED | 88b51cd |
| R4-088 RAG cluster | FIXED | 7e0ef86 |
| R4-089 memory cluster | FIXED | 948f61e + 22f4bc2 |
| R4-090 tools | FIXED | bf9ac9b |
| R4-091 skills/soulspec | FIXED | 0547aea |
| R4-092 research | FIXED | b378663 |
| R4-093 CLI | FIXED | b1794be |
| R4-094 TUI | FIXED | eedcde1; parity test 011f1ba |
| R4-095 infra | FIXED | 5b2d2f1 |
| R4-096 dead-code wave | FIXED | 4294499 |
| R4-100/101/102/103/104/105 test truth | FIXED | 7c9321e, 9474333, da7e9e9 |
| R4-106 production seams | FIXED | 1b74a13 + 150c3fc |
| R4-120 docs truth | FIXED | 71d5fa3 |
| R4-049b fd-close helper | FIXED | b27a420 |
| R4-130 final gates | PENDING | runs after R5/HELD lanes settle |
| R4-131 this ledger | DONE | this commit |

Refuted items remain out of scope, as documented in the plan: the absolute-path
symlink claim and the LoCoMo mixed-mode crash.

Behaviour changes (all called out in their commit bodies): nullable
`agent_messages.tokens_used` (DROP NOT NULL, idempotent); `minimal` effort tier
accepted at config; `/provider remove` persists its fallback; full-grant
disclosure semantics unchanged in policy, corrected in copy; `max_iterations`
surface removed (config key, souldef field, DB column via idempotent DROP).
