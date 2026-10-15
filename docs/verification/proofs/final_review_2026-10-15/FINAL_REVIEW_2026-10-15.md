# FINAL REVIEW — consolidated findings ledger (2026-10-15)

Sources (all evidence under this directory):
1. `CODEBASE_REVIEW_FOLLOWUP_2026-10-15.md` (external follow-up review, F01–F14).
2. This session's 12-agent final-review pass:
   `adversarial_review.md`, `code_review.md`, `security_review.md`,
   `architecture_review.md`, `line_core_runtime.md`, `line_providers.md`,
   `line_security_permissions.md`, `line_memory_connectors.md`,
   `line_rag_tools_skills.md`, `line_gateway_obs_cli.md`, `line_ui.md`,
   `line_infra_docs.md`.

Status legend: `FIXED <commit>` · `LANE` (fix lane in flight) · `DEFERRED`
(documented, with reason) · `REFUTED` · `DOC` (wording/documentation-only).

## A. Follow-up review (F01–F14)

| ID | Sev | Finding | Status |
|---|---|---|---|
| F01 | P1c | Dir-fd write chain: ancestor swap between containment check and lstat/open | FIXED 56f8ad3 |
| F02 | P1 | Cancelling a tool batch loses records of already-completed effects | LANE (core) |
| F03 | P2 | Failed connector rollback clobbers a newer concurrent config | LANE (connectors) |
| F04 | P2 | Agent mcp_connect can launch a stdio connector in sandbox mode | LANE (connectors) |
| F05 | P2 | Connector store flock acquired synchronously in async paths | LANE (connectors) |
| F06 | P2 | OAuth refresh lock-order inversion | LANE (connectors) |
| F07 | P2 | HTTP streaming drops growth/RSI context between generator steps | FIXED 2edea07 |
| F08 | P2 | Shared RAG pipelines keep stale embedding credentials / failed selection | FIXED bdea3a3 |
| F09 | P2 | CLI config-set --persist writes the old value back via env shadow | FIXED 8864af1 |
| F10 | P2 | setup --force merges instead of replacing .env | FIXED 8864af1 |
| F11 | P2 | LoCoMo title-only baseline structurally scores zero | FIXED fa13f27 |
| F12 | P2 | Delegation bookkeeping failure leaks child/provider | FIXED 4714e5e |
| F13 | P2 | FIFO in resolve_path hangs the event loop | FIXED 56f8ad3 |
| F14 | P3L | Bounded subprocess stdin can block before supervision | LANE (misc) |

## B. Security

| ID | Sev | Finding | Status |
|---|---|---|---|
| S1 | P2 | `_check_ip_public` missed CGNAT (100.100.100.200 metadata) + ranges | FIXED 56c0222 |
| S2 | P2 | MCP mode ceiling bypassed when `mode=None` (inherits operator mode) | LANE (misc) |
| S3 | P2 | Embedded-`@` DB URL leak + password-only userinfo (`:pw@`) | FIXED 0e933a8 + 4c00c00* |
| S4 | P2 | REV `:N:path` git revspec bypasses the credential gate | FIXED e0590e0 |
| S5 | P2 | `grep -id recurse` compact clusters bypass recursive-read review | FIXED e0590e0 |
| S6 | P3 | `_sanitize_value` kwargs can override event/timestamp (spoofing) | FIXED 0e933a8 |
| S7 | P3 | ChatGPT logout can fail to revoke behind a wedged refresh lock | DEFERRED (design: bounded wait + typed error; copilot uses fenced_logout; revisit) |
| S8 | P3 | `parsed.port` raised bare ValueError | FIXED 56c0222 |
| S9 | P3 | Skills temp write predictable name + symlink-following | LANE (misc) |
| S10 | P3 | `device_flow_poll` unbounded slow_down sleep | FIXED bd886c1 |
| S11 | P3 | `find_pending`/`find_approved_unclaimed` skip public_view | FIXED e0590e0 |
| S12 | P3u | Credential params named bare `key`/`*_key` not redacted | DEFERRED (would over-redact benign `key=`; needs naming policy) |
| S14 | P2 | Presigned-URL signatures (`sig=`, `X-Amz-Signature`) persisted in audit | FIXED (SEC-2 commit) |
| S13 | Q | `is_loopback_base_url` treats 0.0.0.0 as loopback | DEFERRED (pre-existing; document) |

## C. Core runtime

| ID | Sev | Finding | Status |
|---|---|---|---|
| C1 | P2 | Event-ordinal ordering vs `(created_at,id)` cursor skips tail rows across page boundaries | LANE (core) |
| C2 | P1 | Cancelled batch loses completed-effect records/approvals | LANE (core) |
| C3 | P3 | `run()` flush lacks the cancellation-safe wrapper `run_stream()` has | LANE (core) |
| C4 | P3 | `needs_approvals` (plural) dropped by derivation | LANE (core) |
| C5 | P3 | Flush docstring/behavior mismatch: failed batch silently dropped | LANE (core) |

## D. Providers / config / observability

| ID | Sev | Finding | Status |
|---|---|---|---|
| P1 | P2 | Runtime readiness reports anthropic as "unknown provider" | FIXED 0e933a8 |
| P2 | P3 | SSE parser doesn't strip a leading BOM | FIXED 0e933a8 |
| P3 | P3 | Rate-limit "0 disables" unreachable via config bound | FIXED 0e933a8 |
| P4 | P3 | `audit.metadata()` AttributeError on non-str keys | FIXED 0e933a8 |
| P5 | P3 | `_is_free_model` applies OpenRouter policy to direct providers | DEFERRED (low reach; needs provider context) |
| P6 | P3 | `render_diagnostics` omits `ledger_dropped` marker | BATCH-3 |
| P7 | P3 | Runtime shutdown stops audit before draining background work | FIXED 0e933a8 |
| P8 | P2 | Gateway global mode writes shadowed by MACHINA_EXECUTION_MODE | FIXED d1438e8 |
| P9 | P3 | Gateway global `mode.revoke` not persisted | FIXED d1438e8 |
| P10 | P3 | HTTP SSE v2 `tool.complete.isError` always false | BATCH-3 (post-streaming lane) |
| P11 | P3 | Source-text assertion test (r5_011_013) | BATCH-3 |

## E. Connectors / memory

| ID | Sev | Finding | Status |
|---|---|---|---|
| N1 | P2 | Screener charges nested counts, not atom/body size (`(<193ch>){10000}` compile bomb) | LANE (connectors) |
| N2 | P3 | `OAuthCoordinator.forget()` ignores its owner argument | DEFERRED (route enforces; defense-in-depth) |
| N3 | P3 | Completed-logout tombstone window can resurrect a grant | DEFERRED (cancel-first guard makes it narrow) |
| N4 | P3 | Streaming redactor flush drops withheld PEM prefix | BATCH-3 |
| N5 | Q | `MemoryStore.add` drops explicit connection via gate | DEFERRED (latent; all live callers bypass) |
| N6 | Q | `execute_call` semaphore timeout may leak a permit | DEFERRED (unconfirmed; needs runtime repro) |

## F. Tools / files

| ID | Sev | Finding | Status |
|---|---|---|---|
| T1 | P1c | (see F01) | FIXED 56f8ad3 |
| T2 | P2 | (see F13) | FIXED 56f8ad3 |
| T3 | P2 | `read_file` reads to EOF for small pages (regression) | FIXED 56f8ad3 |
| T4 | P2 | `_atomic_write_file` duplicates `_atomic_write_file_at` | FIXED 56f8ad3 |
| T5 | P3 | Skill-install writes still detach via bare to_thread | BATCH-3 |
| T6 | P3 | `_fsync_dir_best_effort` duplicates env_file's helper | DEFERRED (hoist with atomic-write consolidation) |

## G. RAG / skills / research / db

| ID | Sev | Finding | Status |
|---|---|---|---|
| R1 | P2 | (see F08) | LANE (rag) |
| R2 | P3 | Chunker recursion can drop a leading separator (coverage) | FIXED bdea3a3 |
| R3 | P2 | head_to_head `success` ignores harness exit/error | FIXED fa13f27 |
| R4 | P3 | identity_eval reports 0.0 with no injected transitions | FIXED fa13f27 |
| R5 | P2 | (see F11) | FIXED fa13f27 |
| R6 | P3 | Skill enable/update bypass the install manifest (un-reinstallable) | DEFERRED (needs manifest-update design) |
| R7 | P3 | Casefold collision desync for uppercase local skill names | LANE (misc addendum) |
| R8 | Qx | `_infer_schema` collapses compound types (latent) | DEFERRED (no live caller) |

## H. UI

| ID | Sev | Finding | Status |
|---|---|---|---|
| U1 | P3 | OutputViewer renders name/toolId unsanitized | FIXED 39376d0 |
| U2 | P3 | `dim` token below WCAG AA for essential text | FIXED 39376d0 |
| U3 | Q | Dead exports (windowAroundCursor/setOutcome/getState) | FIXED 39376d0 |
| U4 | Q | OAuthProgress missing return; agents.ts shadow | FIXED 39376d0 |
| U5 | Q | White-box test pins (state/mode tests) | DEFERRED (not behavior bugs) |

## I. Infra / docs

| ID | Sev | Finding | Status |
|---|---|---|---|
| I1 | P2 | e2e_gate blank `--db-url=` defeats the non-test-DB guard | LANE (misc) |
| I2 | P3 | FEATURE_LEDGER: `minimal` is a distinct tier, not alias-normalized | LANE (misc) |
| I3 | P3 | Pinned-digest automation overclaimed (3 of 6 sites unmanaged) | FIXED 2918981 |
| I4 | P3 | README/E2E_PROTOCOL document the pre-guard invocation | FIXED 2918981 |
| I5 | Q | `is_test_database` substring heuristic (`latest`, `contest`) | FIXED (documented) 2918981 |
| I6 | Q | SECURITY.md duplicated evidence filename | FIXED 2918981 |
| I7 | Q | check_release_artifacts wheel set lacks `.venv` prefix | DEFERRED (mitigated by pyproject) |

## J. Architecture

| ID | Sev | Finding | Status |
|---|---|---|---|
| A1 | P2 | URL-redaction primitive lives in provider/audit but is consumed by rag/observability (layering) | DEFERRED (documented; hoist to a leaf module when redaction layers consolidate) |
| A2 | P2 | store_fence is not the single fence (providers + oauth_store variants) | PARTIAL: providers delegates (misc lane); oauth_store DEFERRED |
| A3 | P3 | AuditPersistence start/stop asymmetry; conftest pokes privates | BATCH-3 (add reset() API; use in conftest) |
| A4 | P3 | Compaction keyset still `(created_at,id)` (documented residual) | DEFERRED (state boundary explicitly) |
| A5 | P3 | Deferred-import census prose says 688, tree is 687 | DEFERRED (slack, not inflation) |
| A6 | Q | Two new tests pin privates/literals | DEFERRED |

## V. Verified-clean highlights (refutations worth keeping)

- MCP authority ceiling + cancelled-child retirement, HELD-44 write-cancel join,
  R5-B dir-fd anchoring, shared SSE parser, rate-limit binding, R5-F1 lock scope,
  R5-LM generation fences, R5-E installer quoting, R4-021 fail-closed embeddings,
  R5-O validate-then-publish, IPv4-mapped-IPv6 SSRF, plaintext/loopback guards,
  broker deadline/claim-once, audit completeness, keyring default, OAuth SSRF
  rebind, redaction batch/stream parity, migration idempotency, ratchet honesty
  (ceilings equal measured counts; delta arithmetic exact).
- Web/host surfaces: fetch TOCTOU rebind blocked, audit URL-credential redaction
  (except S3), callback state machine, lifecycle/retry/circuit.

Commit trail for fixes: `56c0222` (S1/S8) · `4714e5e` (F12) · `39376d0` (UI) ·
`8864af1` (F09/F10) · plus the lanes landing now.
