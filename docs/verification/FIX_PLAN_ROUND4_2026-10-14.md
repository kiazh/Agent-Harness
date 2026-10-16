# Fix plan — Round 4 (from the full-codebase review, 2026-10-14)

Companion to `docs/verification/CODEBASE_REVIEW_ROUND4_2026-10-14.md` (the findings ledger).
This plan turns every verified finding into a concrete action, ordered red-first.

## Context & ground rules

- Findings snapshot: HEAD `cdc5232`; the tree moved during the review. **Re-confirm line
  numbers before editing.**
- **Do NOT fix these — refuted by adversarial verification (no bug exists):**
  1. "absolute-path symlink bypass in `process.exec`" — `permissions/policy.py:563` already
     `.resolve()`s absolute operands.
  2. "LoCoMo mixed-mode crash" — `research/locomo.py:404` appends the sparse ranking in the
     hybrid branch too; only a component-reporting limitation exists.
- Coordination: another session commits to `main`. Serialize edits per file; rebase before
  each round; never run two writers on the same module concurrently.
- Tests: only `postgresql://postgres:postgres@localhost:5432/machina_test`; never the live
  DB. Real-provider calls are out of scope; use mocked transports.
- Skills to use per phase: `diagnose` (every bug), `tdd-guide`/ECC `tdd-workflow` (red-first
  test tightening), `coverage` + `senior-qa` (gap closure), `verification-loop` (round
  gates), `no-mistakes` (pre-push pipeline), `adversarial-reviewer` (whole-diff review),
  `security-review`/`security-pen-testing` (Rounds 2–3), `docker-patterns`/`deployment-
  patterns` (infra), ECC `python-testing`, `error-handling`, `agent-introspection-debugging`.

## Protocol for EVERY task

1. `diagnose` loop: reproduce → capture failing evidence → minimal fix → **E2E or
   adversarial/eval proof** (RED first: the strongest proof must fail against the unfixed
   code or the test is deleted/merged) → focused run. **No fix is accepted on unit tests
   alone**; mocks only at network/provider/keyring boundaries, never at the seam under fix.
   Per-task required proof type: see the matrix in `FIX_PLAN_ROUND4_TASKS.md`.
2. One logical change per commit, message `R4-N: <what>`.
3. P1 fixes get before/after evidence under
   `docs/verification/proofs/codebase_review_round4_2026-10-14/`.
4. Round gates: `ruff check` + `ruff format --check`; backend suite on `machina_test`
   (serial); `ui: npm run check`; `bandit`; `git diff --check`. **Run the backend and UI
   gates SEQUENTIALLY, never in parallel**: the UI clipboard test bounds an external
   process at the product's 3 s timeout, and concurrent suite load can starve a spawn past
   it (observed once; isolated runs are ~0.6 s). Parallel gates are self-inflicted flakes.
5. Final gate adds: `uv lock --check`, wheel+sdist build + `tools/check_release_artifacts.py`,
   `no-mistakes`, `adversarial-reviewer` over the round diff, hosted CI green.
6. Design decisions (bottom of this file) get a one-line note in `FEATURE_LEDGER.md`.

---

## Round 0 — Write the report + restore a green baseline (blocks everything)

- [ ] Report + proofs dir written (this commit).
- [ ] Fix the red ratchet: `machina/core/provider/reasoning.py` is 68 lines vs a 61 ceiling
      (package 5508 vs 5501) after `cdc5232`. Trim/relocate ~7 lines (ceilings only fall);
      run `tests/test_architecture_layers.py` green.
- [ ] Run the full backend + UI suites; record EVERY currently-failing test (known: the two
      ratchet guards; suspects seen during review: `test_provider_store.py`,
      `test_provider_context_audit.py`, `test_fm1_regressions.py`). Triage each.
- [ ] Add `test_architecture_layers` (+ an SSE no-error-frame assertion) to the standard
      gate so the bypass that produced the red tree cannot recur.

## Round 1 — Verified P1 product fixes

1. **SSE spurious error frame** (`core/agent.py:122-147`, `core/session_mode.py:87-105`,
   `api/routes/streaming.py:70-89,177,353`): drive the generator within one task, or move
   the turn-scope token lifecycle into the engine so set/reset share a Context. Flip the
   E2E proof's `"error": 1` to 0 (`docs/verification/proofs/2026-10-14-e2e-gate.md:231-277`).
2. **Compaction drops result text** (`core/compression.py:823-828`): read
   `payload.get("result", payload.get("content",""))` (align with `core/assembler.py:178-184`).
   Test: delegated-output text present in the summary.
3. **Anthropic consecutive `user` roles** (`core/provider/anthropic.py:96-108`,
   `claude.py:60-64`): coalesce a batch's `tool_result` blocks into ONE user message.
   Test: 2-tool turn emits one user message; roles alternate.
4. **Claude/Gemini/Copilot auth fencing** (`security/claude_auth.py:360-452,570-652`,
   `gemini_auth.py:215-222,356-446`, `copilot_auth.py:133-166`, `cli/auth.py:558-578,701-721`):
   port the ChatGPT pattern (`security/chatgpt_auth.py:98-206,428-434`): per-store flock over
   read→POST→publish, peer adoption under lock, generation bump on logout, fenced logout
   helper; Gemini `update_project` merge-under-lock; Copilot cancels pending device flows.
   Tests: barrier-mocked concurrent refresh (one winner, no invalid_grant); refresh after
   logout cannot recreate the store; project-update cannot clobber a fresh refresh.
5. **Redaction under-match + tail leak** (`memory/redaction.py:224-242,275-310`): run
   quoted-key patterns before `generic_secret`; whole-value capture with permissive chars;
   post-replace re-scan. Add over-match guards (bearer min-length/word bound; "bearer of"
   untouched; SSN/CC calibration; `sk-` boundary). Tests both directions.
6. **Ctrl+C cannot stop `machina chat`** (`cli/__init__.py:69-74,397`): run the turn on the
   main loop with `loop.add_signal_handler(SIGINT, cancel)` (or hand the signal to the
   worker as cancellation); cleanup runs; exit 130. Test: SIGINT during a slow tool cancels
   within ~1 s and releases the claim.
7. **Headless `full` trap** (`cli/__init__.py:196-244`): call
   `activate_durable_mode`/`grant_full_default` after session create (mirror
   `gateway/server.py:559-565`, `api/routes/sessions.py:102-108`) — or refuse `--mode full`
   headless with a clear error. Test: headless full turn runs a routine tool with no pause.
8. **Terminal timeout contract** (`core/agent.py:214-225,1035-1092`;
   `tools/terminal.py:265-268`): pass the per-call timeout into the wait_for budget (bounded
   by the hard cap); format `TimeoutError` with tool name + seconds. Test: `timeout=90`
   cancels at ~90 s (compressed), message names the timeout; no bare `Error: `.
9. **Head-to-head grader integrity** (`research/head_to_head.py:482-483,586-610`): hash the
   whole fixture root except an allowlist; run pytest with `-p no:cacheprovider` and an
   explicit `-c` config override; require `failed == 0` and an unchanged workspace. Tests:
   the addopts-deselect attack scores `success=false`; a genuine fix still scores true;
   root-level `--test-path` is no longer a guaranteed false negative.
10. **Embedder zero-vector poisoning** (`rag/embedder.py:265-270,146-177`): missing/empty
    `data` → typed error, never cache. Test: malformed 200 → error; cache unchanged.
11. **Denied tool renders as success** (`core/agent.py:1155` use `_tool_outcome(item)`;
    `ui/src/transcript.ts:194` map `"denied"` to a distinct state). UI test: denied card
    shows denied, not success.
12. **Menu/Picker sanitization** (`ui/src/components/Menu.ts:60,94-103`,
    `components.ts:317`) + newline-splitting in single-row renderers (`ToolTurn.ts:58-65`,
    `ApprovalCard.ts:249-252`). Tests: ESC/OSC/newline payloads neutralized/split.
13. **Provider-remove stale pin** (`ui/src/features/settings.ts:223-230`, `app.ts:783`):
    refresh App session state on `session.setModel`, or re-fetch the session in the remove
    guard. Test: add → switch → remove → prompt routes to the fallback.
14. **`max_iterations` decision + fix** (`core/agent.py:277-290,1606,1820`;
    `core/config.py:90-92`): DECISION — enforce (recommended) or remove the surface
    everywhere; update the test that pins inertness either way.
15. **`index_document` resolver** (`tools/rag.py:192`, `rag/loaders.py:109-116,238-244`):
    one canonical workspace resolver (`execution_context.workspace_root`) for the FileLoader
    base; pass the broker-resolved absolute path. Tests: `workspace_root` ≠ home (abs + rel).
16. Small verified P1-family: uninstall shim segment-aware matching
    (`cli/__init__.py:1928-1930`) + boundary test; artifact "backtrack" implement-or-delete
    (`core/orchestrator.py:402-414`); delegation rows record `error` instead of staying
    `running` when the ledger read fails.

## Round 2 — Security hardening (verified P2s)

- ChatGPT residual fences: refresh-with-pre-logout-tokens window
  (`security/chatgpt_auth.py:541-568,627-640`); login-exchange false-cancel on benign peer
  rotation (`:748,804-815` — only generation change should cancel).
- Register Claude/Gemini/Copilot token values with the redaction registry
  (`claude_auth.py:154-187` etc.; cf. `chatgpt_auth.py:227-234`).
- `.env` escape round-trip (`security/env_file.py:157-174`) → unescape on read; flip the
  strict xfail (`tests/test_fm7_cli.py:325-337`).
- `.env` writer (`env_file.py:236-275`): `O_EXCL|O_NOFOLLOW`, unpredictable temp
  (`mkstemp` in target dir), fsync file + dir.
- Keyring flag (`security/oauth_keys.py:58-75`): honor the flag's name (allow plaintext);
  keep the allowlist otherwise.
- Lowercase/alias env keys (`env_file.py:177-183`): case-insensitive update/remove.
- Non-loopback plaintext `http://` (`security/providers.py:168-177`): warn or require an
  explicit opt-in before attaching bearer keys.
- `.env` fallback into site-packages (`env_file.py:51,130`): detect installed-package
  layout; never target site-packages.
- `flock` timeouts in both store locks (`chatgpt_auth.py:196`, `providers.py:92`); fd-close
  helper for chmod-failure paths (`chatgpt_auth.py:334-347` + 5 siblings).

## Round 3 — Permissions, policy, approvals, observability

- `git grep --untracked/--no-exclude-standard` → route to review
  (`permissions/policy.py:323-391`, `commands.py:52,58-86`); revspec handling
  (`HEAD:credentials`); `.env~`/`credentials~`/`.netrc.bak` families (keep `.env.example`
  allowed).
- Calibrate `_ESCAPE_ARGS` false positives (`wc -c`, `ls -C`, `grep -C 2`, `head -c`) while
  keeping dangerous attached forms blocked (`commands.py:58-86,126-132`).
- `_tree_contains_component` fail-closed on OSError (`policy.py:426-441`).
- `safe_logging` structured-value redaction + guarded redactor call
  (`observability/safe_logging.py:12-29`); trace-ring eviction markers
  (`observability/traces.py:255-258,324-325`).
- Approval expiry emits a resolution frame (`permissions/broker.py:250-256`) + UI retire
  (`app.ts:1279-1291`); route the expired-vs-approved race through the durable record
  (`store.py:575-611`); audit rows carry target/argv (`broker.py:344-369`).
- Store ordering F6 (`permissions/store.py:691-718,498-529`).

## Round 4 — Correctness/robustness P2 clusters

- **Config/boot:** guard non-mapping YAML root (`core/config.py:576`); fix the recovery
  message to `machina init` (`cli/__init__.py:220`); connecting-sentinel for
  `Database.connect` (`db/connection.py:35-52`); `run_bounded` ValueError → typed result
  (`core/child_process.py:207-230`); MCP `machina_chat` DSN passthrough (decision)
  (`mcp_server/__init__.py:114-121,192-199`).
- **Connectors:** drop the `or {}` that kills the "oauth not configured" guard
  (`connectors/oauth.py:154-156` + 4 sites); validate ports (`models.py:178-190`) and
  harden `preview` (`oauth.py:170-178`); pattern-screener calibration
  (`policy.py:118-148`); refresh preflight waits instead of hard-fail
  (`oauth.py:1182-1188,1260-1274`, `execution.py:293-298`); ASCII-check OAuth `state`
  (`oauth_callback.py:214-222`); move sync index reads to `to_thread`
  (`oauth.py:761,984`).
- **RAG:** empty re-index deletes the old generation (`pipeline.py:318`); decorator-aware
  `split_code` (`chunker.py:188-233`); fence-aware markdown headings (`chunker.py:142-159`);
  surface dual-branch failure (`search.py:100-137`); repomap digest check or rename
  (`repomap/cache.py:33-43`); `_hard_split` zero guard (`chunker.py:405-424`).
- **Memory:** build dedup candidates with embeddings (`consolidator.py:296`) or delete the
  claim; streaming digit-run hold (`redaction.py:491-618`); calibration set
  (`redaction.py:73,202,212,280`); wire or remove the approval gate (`approval.py`);
  `update_access`/`batch_update_access` consistency; `shared_bus` retry.
- **Tools:** `read_file` size-check after offset/limit (`file.py:287-292`); truncation
  marker (`file.py:315-317`); atomic `write_file` temp+replace (`file.py:357-405`);
  `harness_status` failure ≠ absent (`harness.py:165-203`); tool-cache decision
  (`base.py:76-90`); honor `additionalProperties` (`base.py:234-261`); parameters for the 5
  schema-less tools (`builtins.py:232,274,465`, `skills.py:11-14,48-51`).
- **Skills/soulspec:** hub-JSON type hardening (`registry.py:1063-1109`); `delete_skill`
  removes the manifest-owned tree (`registry.py:901-916`, `repo_install.py:915-936`);
  pre-stat size guards (`registry.py:337-350`, `soulspec/schema.py:76`); Windows
  `_fsync_dir` best-effort at activation (`repo_install.py:993-1008`); repo URL port
  validation (`repo_install.py:91-97`); workflow classifier gate
  (`skills/workflow.py:126-149`).
- **Research/CLI/TUI/Infra:** root test-path hashing, runs-dir guard, benchmark
  timeouts + exit codes, cleanup isolation, `-O` asserts (`research/`); `config-set` env
  sync, Rich escaping, tracebacks, `--refresh` empty guard, setup env guard, repl
  validation (`cli/`); re-resume guard, hidden-failure notices, approval-expiry UI,
  image-marker reset, composer indicators, mouse offset, Kitty Ctrl+S, contrast,
  `/mode revoke` copy, delegate budget, keys allowlist (`ui/`); CI `uv lock --check`,
  docker-reuse credentials, digest/SHA pins, install.sh env guard, wheel telemetry
  exclusion (`infra`).

## Round 5 — Test-suite truth (RED-first; ~35 P1-class items)

Per the false-confidence list in the review report, grouped. Replacements must be **E2E or
adversarial** wherever a real surface exists (subprocess imports, real routes/DB,
fake-terminal UI, attack fixtures); unit-level tightening only with a written reason.

- **Unfalsifiable:** `or True` ×3 (`test_repo_install.py:234`,
  `test_integration_permissions.py:91`, `test_gateway_providers.py:77`); `evicted >= 0`
  (`test_chaos_extended.py:789`); `result > 0` eviction asserts; `isinstance(results, list)`
  suites.
- **Never reaches the code:** `connect()` (`test_review_round2_fixes.py:53`), `acquire()`
  (`test_property_based.py:469-505`), rerank early-return (`test_memory_bug_fixes.py:269`),
  AH-018 probe (`test_handoff_regressions.py:471-519`), `claim_due` SQL copy
  (`test_audit_remediation.py:1016-1071`), `startup()` hand copy
  (`test_production_readiness_p2.py:210-243`), wrong seam `add_chunk`
  (`test_chaos.py:619-688`), RPC route (`test_rpc_gateway.py`), controller purity
  (`test_controller_triage.py:269`), learning cap (`test_learning_review.py:857-909`),
  load race (`test_load_concurrency.py:316-379`).
- **Mock-enforced behavior:** OAuth caps by stub (`test_connectors_oauth_network.py:31-53`),
  delegation transport, persona DB filters, batch dedup.
- **Pins pre-fix behavior:** RPC TTL (`test_api_bugfixes.py:347-448`), luna ultra,
  bug-15 lazy config, `_normalize_answer(2024.5)` (update with the R1/`locomo` change).
- **Environment coupling:** maintainer-home absolute path hardcode (`test_repomap.py`, since fixed),
  `live_db` error-instead-of-skip, MCP hang→skip, Windows-only tests never on CI, env vars
  leaking from import (`MACHINA_API_KEY`, `MACHINA_GATEWAY_NO_SCHEDULER`).
- **Missing coverage to add:** true caller-scope binding; same-session read/write load race;
  cross-process flock contention; "no secrets in approval view" with the secret in argv;
  advisory-lock pool path with a DSN; env-file round-trip after the escape fix.

## Round 6 — Docs truth

- `OPERATIONS.md:54-57` — correct the self-migration claim (additive runtime migrations at
  startup, fail closed; full schema via `machina init`).
- `RELEASE_READINESS.md:9` — DNS-rebinding protections are implemented and in use.
- Annotate the round-2 review doc's fixed lockfile claim.
- Update `CURRENT_ISSUES_CHECKPOINT.md` + `FEATURE_LEDGER.md`; README where behavior changed
  (effort vocabulary; `/provider remove`; keys allowlist).

## Round 7 — Final verification & release gate

- Full backend suite (serial, `machina_test`), `ui: npm run check`, ruff/format, bandit,
  `uv lock --check` (enforced in CI), wheel+sdist + artifact check, `no-mistakes`,
  `adversarial-reviewer` over the whole round diff, hosted CI green.
- Re-run the E2E gate proof; confirm SSE `"error": 0` and both architecture guards green.
- Update the Round-4 review doc with fix status per finding; keep "verified" claims scoped
  to the evidence.

## Decisions needed from the owner (before Round 1)

1. `max_iterations`: enforce the cap (recommended) or remove the surface.
2. MCP `machina_chat`: pass `DATABASE_URL`/`MACHINA_ENV_FILE` through the sanitizer, or
   resolve the DSN in the parent and hand it to the child (recommended: parent passes its
   resolved DSN; child never guesses).
3. Artifact-contract "backtrack": implement the retry, or delete the dead call + claim.
4. Pattern screener: relax bounded nested counts (`(a{1,3}){3}`) with a size budget
   (recommended), or keep rejecting and document.
5. `/provider remove` per session: persist the gateway fallback (recommended) or refuse
   removal while any session is pinned.
6. Scope/order: everything in this order OK, or P1-only first and stop for review?
