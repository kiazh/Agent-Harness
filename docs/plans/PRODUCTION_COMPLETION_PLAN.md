# AgentHarness — Production Completion Plan

**Status:** Release candidate with known approval, persistence, SSRF, and operational gaps.  
**Goal:** Close every remaining defect, verify the complete pipeline end-to-end, and declare a production-ready `v1.0.0`.

This plan covers everything required to make AgentHarness a complete final product: no unresolved functional bugs, no security gaps, no disconnected pipeline paths, no duplicated critical logic, and no untested production guarantees.

---

## 1. Definition of done

AgentHarness is production complete only when **all** of the following are true:

- Every user-facing workflow works end-to-end: CLI, TUI, HTTP/SSE, scheduled jobs, delegation, memory, RAG, skills, approvals, cancellation, and shutdown.
- Every transport uses the same shared pipeline: agent factory, session ownership, permission broker, usage accounting, RAG, audit, metrics, and runtime lifecycle.
- Every security-sensitive action is explicitly proposed, reviewed, authorized, executed exactly once, and auditable.
- No TODO, FIXME, XXX, or unimplemented security control remains in production code.
- No silent exception swallowing remains in cleanup, authorization, persistence, or provider lifecycle paths.
- Every durable state transition is idempotent, crash-safe, and safe across multiple workers.
- Every API, gateway event, database migration, UI event, and test expectation agrees on one versioned contract.
- Full test suite, integration suite, lint, typecheck, formatting, security scan, and smoke deployment all pass.
- Documentation accurately states supported behavior, limitations, threat model, and operational requirements.

Do **not** mark the project complete while any item in this document remains open.

---

## 2. Remaining release blockers

Complete these before any production release.

### 2.1 Implement HTTP approval resume

**Priority:** P0 — release blocker  
**Files:** `ah/api/app.py`, `ah/gateway/server.py`, `ah/gateway/errors.py`, `communications/ui-gateway-protocol.md`, `ui/src/protocol.ts`

**Current issue**

The HTTP prompt stream emits a typed `needs_approval` event and then emits `done`, ending the SSE response. The approval resolver records the human decision and may save a reusable grant, but it does not visibly resume the original stream or begin a new authorized continuation.

A user can approve an action, but the HTTP client has no defined way to receive the resumed execution.

**Required work**

1. Define one explicit approval contract for TUI and HTTP:
   - `needs_approval`
   - `approval.resolved`
   - `approval.resumed`
   - `turn.started`
   - terminal completion event
2. On approval, do **not** continue the stale generator.
3. Revalidate the exact canonical proposal:
   - request ID
   - session ID
   - turn ID
   - operation
   - tool
   - argv
   - cwd
   - backend
   - timeout
   - content digest
   - network destinations
   - capabilities
4. Acquire a fresh fenced turn claim.
5. Start a new SSE stream or explicit continuation stream.
6. Ensure denial, expiration, cancellation, disconnect, and duplicate resolution all produce typed outcomes.
7. Ensure approval resolution cannot authorize a different action, session, user, or backend.

**Acceptance tests**

- HTTP prompt pauses with `needs_approval`.
- Human approves through `/api/v1/approvals/{id}/resolve`.
- A fresh fenced claim is acquired.
- The approved action executes exactly once.
- The client receives a clear resumed stream or explicit continuation event.
- Denial produces a structured terminal outcome, not a retry loop.
- Approval cannot be replayed after consumption.
- A changed proposal requires fresh approval.
- A client disconnect stops owned work and releases ownership.

---

### 2.2 Make session execution mode durable

**Priority:** P0 — release blocker  
**Files:** `ah/core/session_mode.py`, `ah/core/session.py`, `ah/core/turns.py`, `ah/gateway/features/mode.py`, `ah/db/schema.sql`, `ah/core/config.py`

**Current issue**

Session-local execution modes are stored only in an in-process dictionary. Restarting the service, deploying multiple workers, or handling the same session from another process loses the explicit mode.

This affects `ask`, `workspace`, `sandbox`, and `full`. It is especially important because a user may approve a sandbox-isolated session, then restart the service and lose that isolation.

**Required work**

1. Add durable per-session mode state, for example:

```sql
ALTER TABLE sessions
  ADD COLUMN IF NOT EXISTS execution_mode TEXT
  NOT NULL DEFAULT 'ask'
  CHECK (execution_mode IN ('ask', 'workspace', 'sandbox', 'full'));
```

2. Store explicit session activation transactionally.
3. Preserve the distinction between:
   - session override
   - global default for future sessions
   - live permission grant
4. Resolve effective mode as:

```text
broker-stamped execution context
  -> durable session override
  -> global default
```

5. Invalidate or refresh caches across workers after a mode change.
6. Ensure `full` still requires a live, session-bound full-mode grant.
7. Ensure `sandbox` never silently downgrades to host execution.
8. Ensure mode changes cannot affect an already-approved execution snapshot.

**Acceptance tests**

- Activate `workspace` for one session; global default remains unchanged.
- Activate `sandbox` for one session; restart the app; mode remains `sandbox`.
- Activate `full` for one session; another session does not inherit it.
- Activate `full` globally; only subsequently created sessions inherit it.
- A `full` session without a live full-mode grant cannot execute host actions.
- A concurrent mode change cannot alter an already-approved execution.
- Two workers cannot create conflicting mode state for the same session.

---

### 2.3 Close the SSRF gap in `web_extract`

**Priority:** P0 — security release blocker  
**Files:** `ah/tools/builtins.py`, `ah/security/fetch.py`, `tests/test_security.py`, `tests/test_deep_fetch_deadline.py`

**Current issue**

`web_extract()` validates the URL and resolves a safe IP, but then sends the original URL to `https://r.jina.ai/{url}`. The resolved safe IP is never used for the actual outbound connection.

The code contains an unresolved security TODO:

```text
TODO(pinned-ip): actually connect to the pinned IP with Host header / TLS
```

DNS rebinding may therefore bypass the SSRF policy between validation and the Jina proxy fetch.

**Required work**

Choose and implement one secure design:

**Option A — direct pinned fetch**

1. Fetch the target directly using `pinned_fetch()`.
2. Connect only to an IP validated immediately before connection.
3. Preserve TLS SNI and hostname verification for the original hostname.
4. Enforce redirect revalidation on every hop.
5. Cap headers, body bytes, redirects, and overall wall-clock time.
6. Use the extracted content directly.

**Option B — trusted extraction proxy**

1. Remove the misleading `pinned_ip` resolution if it is not enforced.
2. Treat Jina as a trusted third-party extraction service.
3. Clearly document that the target hostname is sent to Jina.
4. Add an explicit allowlist/config flag for enabling third-party extraction.
5. Do not claim SSRF pinning unless the actual connection is pinned.

**Recommended:** Option A for a self-hosted security-first product.

**Acceptance tests**

- Private, loopback, link-local, multicast, reserved, and unspecified addresses are rejected.
- DNS rebinding between validation and fetch cannot reach an internal address.
- Redirects are revalidated hop-by-hop.
- TLS hostname verification remains enabled.
- Body, header, redirect, DNS, connect, and overall deadlines are enforced.
- No unresolved SSRF TODO remains in production code.
- The security scanner reports no unresolved high or medium findings.

---

### 2.4 Stop swallowing provider-close failures

**Priority:** P1  
**Files:** `ah/core/agent_factory.py`

**Current issue**

`close_agent_provider()` catches every exception with `except Exception: pass`. Close failures, timeouts, and unexpected provider errors are silently discarded.

This can hide leaked HTTP clients, file descriptors, connections, or provider resources.

**Required work**

1. Preserve `asyncio.CancelledError`.
2. Log close failures with:
   - agent ID
   - provider type
   - elapsed time
   - exception type
   - safe error message
3. Add a cleanup-failure metric.
4. Do not mask the primary turn exception.
5. Ensure a close timeout still cancels the close task and records the quarantine outcome.

**Acceptance tests**

- A provider `close()` that raises is logged.
- A provider `close()` that exceeds the timeout is cancelled and logged.
- Cancellation propagates correctly.
- The primary turn outcome is not masked.
- No unawaited coroutine warnings remain.

---

## 3. Reliability work

### 3.1 Cancel REST work after ownership loss

**Priority:** P0  
**Files:** `ah/api/app.py`, `ah/core/turns.py`, `ah/core/agent_factory.py`

**Current issue**

The HTTP SSE renewal loop detects failed renewal and logs it, but it does not cancel the active agent stream or signal the generator to stop.

A lost claim can leave the original stream running while another worker owns the session.

**Required work**

1. Add an ownership-lost event or cancellation signal to the SSE generator.
2. On renewal failure, cancel the active agent stream.
3. Ensure no further file, process, network, memory, or database effects occur after ownership loss.
4. Release the turn claim in an independent outer `finally`.
5. Ensure the client receives a distinct cleanup-pending or ownership-lost state, not a false successful completion.

**Acceptance tests**

- Worker A owns a stream.
- Worker B takes over after expiry or forced loss.
- Worker A cancels its stream.
- Worker A performs no further effects.
- Worker B can acquire a clean claim.
- No duplicate terminal completion occurs.

---

### 3.2 Correct approval-time timeout accounting

**Priority:** P1  
**Files:** `ah/gateway/server.py`, `ah/core/turns.py`

**Current issue**

The approval handler adds elapsed wait time in its `finally` block. The timeout loop reads the accumulated total during the wait.

A long approval may still consume compute budget until approval resolves.

**Required work**

1. Track active approval intervals immediately when the wait begins.
2. Maintain both:
   - completed approval wait total
   - currently active approval wait interval
3. Subtract both from compute time in the timeout loop.
4. Prevent overlapping approval waits from being counted twice.
5. Keep the approval deadline broker-bounded.

**Acceptance tests**

- A 10-minute approval wait does not consume 10 minutes of compute timeout.
- Two overlapping approval waits are counted once, not twice.
- Approval denial, timeout, cancellation, and resolution all stop the active wait correctly.
- Compute timeout fires only after actual compute exceeds the configured limit.

---

### 3.3 Renew ownership continuously during approval waits

**Priority:** P1  
**Files:** `ah/gateway/server.py`, `ah/core/turns.py`

**Current issue**

The TUI approval handler renews once after approval resolves. The separate renewal loop runs every 120 seconds, but it is not synchronized with approval waits.

A long approval can race against claim expiry.

**Required work**

1. Renew the claim periodically during the approval wait.
2. Use an interval safely below the claim TTL.
3. Stop renewal immediately on cancellation, denial, disconnect, or ownership loss.
4. Ensure renewal failure sets ownership-lost state and stops owned effects.
5. Do not hold a database transaction or connection across the wait.

**Acceptance tests**

- A long approval wait keeps the claim alive.
- Cancellation stops renewal and releases ownership.
- Renewal failure stops owned effects.
- No SQL transaction or pool connection remains open during the wait.

---

### 3.4 Make timeout completion lifecycle-safe

**Priority:** P1  
**Files:** `ah/gateway/server.py`

**Current issue**

The timeout wrapper can emit a fallback `message.complete` before cleanup and ownership release complete. This conflicts with the contract that terminal completion means ownership is released.

**Required work**

1. Emit a distinct `turn.cleanup_pending` or equivalent state while cleanup is still running.
2. Emit terminal `message.complete` only after ownership release.
3. Preserve exactly-once terminal-event semantics.
4. Ensure late genuine completion cannot duplicate a terminal event.
5. Ensure a quarantined cancellation-resistant task cannot falsely report success.

**Acceptance tests**

- Timeout produces one terminal completion only after ownership release.
- Late completion does not duplicate the terminal event.
- Cancellation-resistant work is quarantined, not falsely completed.
- Next prompt correctly receives `409` while the stale execution still owns the session.

---

### 3.5 Define the script dependency trust boundary

**Priority:** P1  
**Files:** `ah/core/job_scripts.py`, `ah/core/scheduler.py`, `README.md`, `FEATURE_LEDGER.md`

**Current issue**

The approved snapshot protects the reviewed entry script’s bytes, but not imported Python modules, shell-sourced files, or other dependencies.

A changed dependency can alter behavior after approval.

**Required work**

Choose one explicit production boundary:

**Option A — full dependency snapshot**

1. Resolve and snapshot the complete dependency closure.
2. Bind every dependency’s content digest to the approval proposal.
3. Refuse execution if any dependency changes.
4. Use an isolated execution root.

**Option B — trusted dependency boundary**

1. Keep only the entry script snapshotted.
2. Explicitly document that imported dependencies are administrator-trusted.
3. Restrict script jobs to an administrator-controlled allowlist.
4. Add dependency hashing and audit logging.
5. Do not claim approval covers dependencies it does not cover.

**Recommended:** Option A for production; Option B only if script jobs are restricted to a tightly controlled administrator environment.

**Acceptance tests**

- A changed imported module is detected under Option A.
- A changed sourced shell file is detected under Option A.
- Approval cannot authorize a dependency change without fresh approval.
- The actual executed dependency set matches the approved dependency set.

---

## 4. Pipeline coherence

### 4.1 Unify the approval event contract

**Priority:** P1  
**Files:** `communications/ui-gateway-protocol.md`, `ui/src/protocol.ts`, `ui/src/app.ts`, `ah/api/app.py`, `ah/gateway/server.py`

**Current issue**

The TUI uses `permission.required`; HTTP emits `needs_approval`. The attached UI protocol does not visibly define `needs_approval`, even though the HTTP serializer emits it.

This creates ambiguity about which event names, fields, and resume semantics are canonical.

**Required work**

1. Define one versioned protocol for both transports.
2. Use consistent event names and field names.
3. Document every event:
   - purpose
   - payload
   - session/turn binding
   - terminal semantics
   - error semantics
   - approval/resume semantics
4. Add protocol conformance tests.
5. Ensure the UI type definitions match the server serializer exactly.

**Acceptance tests**

- Every emitted event has a declared type and schema.
- Every UI event handler matches the server contract.
- No undocumented event fields are emitted.
- TUI and HTTP use compatible approval semantics.
- Protocol version changes are explicit and backward-compatible or migrated.

---

### 4.2 Remove duplicate critical-path logic

**Priority:** P2  
**Files:** `ah/core/job_scripts.py`, `ah/core/runtime.py`, `ah/tools/file.py`, `ah/tools/terminal.py`, test modules

**Current issue**

Several security-critical helpers are duplicated or nearly duplicated:

- Script path resolution and command construction occur twice in `run_job_script()`.
- Mode resolution logic appears in terminal and file backends.
- Bounded cleanup and task tracking patterns appear in multiple places.
- Test helpers and fake database adapters are repeated across test modules.

**Required work**

1. Resolve the script path once in `run_job_script()`.
2. Construct the execution command once.
3. Extract a shared execution-context resolver for mode/backend/workspace identity.
4. Extract a shared bounded-cleanup helper where appropriate.
5. Move repeated test factories and fakes into `tests/support/`.
6. Do not create a new abstraction layer that obscures the security boundary.

**Acceptance tests**

- No duplicated script path resolution remains.
- No duplicated command construction remains.
- All execution paths use the same mode/backend/workspace resolver.
- Test suite remains green after refactor.
- Security tests still cover symlink, TOCTOU, hard-link, sandbox, and cancellation paths.

---

### 4.3 Make configuration and secrets coherent

**Priority:** P2  
**Files:** `ah/core/config.py`, `ah/security/secrets.py`, `ah/gateway/features/secrets.py`, `.env.example`, `README.md`

**Current issue**

Configuration resolution is coherent in principle, but secrets and non-secrets have overlapping paths. The global config singleton, per-session overrides, environment variables, legacy variables, `.env`, Vault, AWS, and file-backed secrets need one explicit precedence contract.

**Required work**

1. Document precedence exactly:

```text
per-session override
  -> explicit environment variable
  -> legacy environment variable
  -> secret backend
  -> config file
  -> default
```

2. Ensure secrets never enter:
   - config YAML
   - prompts
   - tool payloads
   - logs
   - audit events
   - subprocess environments
   - database rows
3. Ensure all secret reads use the same backend resolver.
4. Add tests for precedence, rotation, missing secrets, invalid values, and secret redaction.
5. Ensure `.env` writes remain atomic, mode `0600`, and reject newline injection.

**Acceptance tests**

- Real environment variables override `.env`.
- `.env` does not override real environment variables.
- Secret values never appear in logs or audit payloads.
- Invalid configuration does not partially mutate state.
- Secret rotation rebuilds affected components where required.
- Subprocess environments do not inherit secrets unless explicitly allowed.

---

## 5. Security hardening

### 5.1 Complete the security threat model

**Priority:** P0  
**Files:** `SECURITY.md`, `README.md`, `FEATURE_LEDGER.md`

Create a `SECURITY.md` that explicitly defines:

- Trust boundaries: user, gateway, agent, tool, plugin, database, provider, Docker, external web.
- What an agent may access by default.
- What requires approval.
- What can never be approved implicitly.
- How approvals bind to exact actions.
- How grants expire, revoke, and are consumed.
- How secrets are protected.
- How SSRF, path traversal, symlink, hard-link, command injection, and privilege escalation are prevented.
- What is **not** protected: plugins, imported script dependencies, third-party extraction services, and cancellation-resistant tasks.

**Acceptance criteria**

- No undocumented privileged capability.
- No vague “safe” or “sandbox” claim without enforcement.
- Every security control has a test.
- Every limitation is explicit.

---

### 5.2 Add security regression tests

**Priority:** P0  
**Files:** `tests/test_security.py`, `tests/test_deep_fetch_deadline.py`, `tests/test_approvals_ownership.py`, `tests/test_integration_permissions.py`

Add or verify tests for:

- DNS rebinding during `web_extract`.
- Redirect rebinding.
- Private IPv4 and IPv6 targets.
- Loopback targets.
- Link-local targets.
- Multicast and reserved targets.
- Symlink parent traversal.
- Symlink target swap.
- Hard-link escape.
- TOCTOU file swap.
- Command injection through argv.
- `find -exec`, `git -c`, and test-runner override blocking.
- Docker container cleanup after timeout and cancellation.
- Approval replay prevention.
- Agent self-approval prevention.
- Cross-session approval prevention.
- Cross-worker claim fencing.
- Secret leakage through logs, audit, exports, RPC, and subprocesses.

**Acceptance criteria**

- Every test fails before the corresponding fix.
- Every fix has a regression test.
- No security test is skipped in CI unless its required infrastructure is unavailable and the skip is explicit.

---

### 5.3 Remove unresolved security TODOs

**Priority:** P0  
**Files:** `ah/tools/builtins.py`

Remove this TODO only after implementing the behavior:

```text
TODO(pinned-ip): actually connect to the pinned IP with Host header / TLS
```

A security control must not remain marked as unfinished.

**Acceptance criteria**

```bash
grep -RInE '\b(TODO|FIXME|XXX|HACK)\b' ah/ ui/src/
```

Returns no production-code result.

---

## 6. Testing requirements

### 6.1 Unit and regression tests

Run and require zero failures:

```bash
pytest tests/ -q --tb=short
```

Minimum requirements:

- No skipped test unless its dependency is unavailable and the skip is explicit.
- No unawaited-coroutine warnings from production paths.
- No deprecated `datetime.utcnow()` usage in production code.
- Coverage remains above the repository gate.
- Every new behavior has both positive and negative tests.
- Every bug fix has a regression test that failed before the fix.

---

### 6.2 Integration tests

Run and require zero failures:

```bash
pytest tests/ -q -m "integration or db"
npm --prefix ui test
npm --prefix ui run check
```

Required integration scenarios:

- CLI one-shot chat.
- TUI gateway turn.
- HTTP SSE prompt.
- HTTP approval and resume.
- Session creation, resume, fork, rename, goal update, delete.
- Memory add, search, approval, rejection, forgetting.
- RAG index, search, reindex, delete.
- Scheduled interval, heartbeat, cron, script job.
- Approval pause and resume.
- Delegation sequential and parallel.
- Cancellation from TUI, HTTP disconnect, and scheduler lease loss.
- Shutdown while a turn, job, script, or provider cleanup is active.

---

### 6.3 Multi-worker tests

Add tests for:

- Two gateway processes claiming the same session turn.
- Two HTTP workers claiming the same session turn.
- Two scheduler workers claiming the same due job.
- Two workers attempting the same mutation.
- One worker losing a lease while another takes over.
- Stale worker attempting to release or finish a newer owner’s claim.
- Session-mode change visible to another worker after durable persistence.

**Acceptance criteria**

- No double execution.
- No lost ownership.
- No stale release.
- No partial commit.
- No silent fallback to concurrent execution.

---

### 6.4 Failure and chaos tests

Add tests for:

- Database unavailable during turn start.
- Database reconnect during a long turn.
- Provider 429 then recovery.
- Sustained provider 429.
- Provider timeout.
- Provider malformed stream.
- Provider disconnect.
- Docker unavailable during sandbox mode.
- Docker daemon unavailable.
- Script timeout.
- Script nonzero exit.
- Script output overflow.
- Cancellation-resistant task.
- Audit writer failure.
- Config file corruption.
- Invalid environment configuration.

**Acceptance criteria**

- No hang.
- No partial commit.
- No silent data loss.
- No secret leak.
- No orphaned claim without expiry/recovery.
- No false success.

---

## 7. Code-quality requirements

### 7.1 Remove duplication

Complete these refactors:

- One script resolver.
- One command builder.
- One execution-context resolver.
- One bounded-cleanup helper.
- One test-support module.
- One protocol definition source of truth.
- One secret-resolution path.

Do not introduce unnecessary indirection. Keep security boundaries explicit and auditable.

---

### 7.2 Enforce static quality gates

Run and require zero failures:

```bash
ruff check ah/
ruff format --check ah/
npm --prefix ui run typecheck
npm --prefix ui run lint
npm --prefix ui run check
bandit -r ah/ -ll -x ah/cli/
git diff --check
```

Additional requirements:

- No bare `except:` in production code.
- No `except Exception: pass` in cleanup, authorization, persistence, or provider lifecycle paths.
- No unused imports.
- No duplicate logger initialization.
- No deprecated APIs in production code.
- No broad exception handling without a documented reason and safe fallback.
- No scanner suppression without a rule-specific explanation.

---

## 8. Database and migration requirements

### 8.1 Add required migrations

Add idempotent migrations for:

- Durable session execution mode.
- Any approval continuation state required by the new HTTP contract.
- Any script dependency snapshot metadata required by the chosen trust boundary.
- Any protocol version or event-contract metadata required for compatibility.

Every migration must:

- Be idempotent.
- Preserve existing user data.
- Avoid destructive cleanup unless explicitly necessary.
- Be tested against a fresh database and an upgraded database.
- Fail safely rather than partially applying.

---

### 8.2 Verify schema/code agreement

Add a schema conformance test that verifies:

- Every SQL column read by Python exists.
- Every SQL column written by Python exists.
- Every enum value used by Python is accepted by the schema.
- Every foreign key and cascade behavior matches the service contract.
- Every index supports the documented hot query paths.
- Every migration can run repeatedly.

---

## 9. Operational requirements

### 9.1 Production configuration

Document and validate:

- PostgreSQL 16+ connection requirements.
- `pgvector` and `pg_trgm` extension requirements.
- Required environment variables.
- Required secrets.
- Provider keys.
- Docker requirements for sandbox mode.
- SearXNG requirements for self-hosted search.
- Rate-limit configuration.
- Usage budgets.
- Approval timeout.
- Turn timeout.
- Lease duration.
- Log retention.
- Metrics retention.
- Backup and restore requirements.

---

### 9.2 Observability

Ensure production exposes:

- Health endpoint.
- Readiness endpoint.
- Prometheus metrics.
- Structured sanitized audit events.
- Gateway logs.
- Turn progress stages.
- Cleanup-failure metrics.
- Claim-loss metrics.
- Approval outcome metrics.
- Provider error and retry metrics.
- RAG cache and retrieval metrics.
- Scheduler lease and job outcome metrics.

No prompt, completion, API key, secret, credential, or raw tool payload may appear in logs or metrics.

---

### 9.3 Deployment checklist

Before deploying:

1. Run full test suite.
2. Run integration suite.
3. Run UI suite.
4. Run lint, format, typecheck, and security scan.
5. Apply migrations to a staging database.
6. Verify fresh-install bootstrap.
7. Verify upgrade from the prior schema.
8. Verify rollback plan.
9. Verify secrets are provisioned through the approved backend.
10. Verify Docker sandbox image is available when sandbox mode is enabled.
11. Verify database backups and restore procedure.
12. Verify monitoring and alerting.
13. Verify log retention and redaction.
14. Deploy to staging.
15. Run smoke tests.
16. Deploy to production only after every gate is green.

---

## 10. Required new tests

Create these test modules if they do not already exist:

| Test module | Required coverage |
|---|---|
| `tests/test_http_approval_resume.py` | HTTP approval, fresh fenced claim, resume, denial, replay, disconnect |
| `tests/test_session_mode_persistence.py` | Durable mode, restart, multi-worker, global vs session scope |
| `tests/test_web_extract_pinned_fetch.py` | DNS rebinding, redirect rebinding, private-address rejection, deadline enforcement |
| `tests/test_provider_close_visibility.py` | Close success, close failure, timeout, cancellation, metrics/logging |
| `tests/test_rest_ownership_loss.py` | Claim loss, stream cancellation, no further effects, clean takeover |
| `tests/test_approval_budget_active_wait.py` | Active approval wait, overlapping waits, compute timeout separation |
| `tests/test_script_dependency_boundary.py` | Entry snapshot, dependency snapshot or documented trusted boundary |
| `tests/test_protocol_conformance.py` | Event names, fields, types, terminal ordering, UI/server agreement |
| `tests/test_multi_worker_fencing.py` | Turn, mutation, job, approval, and mode fencing across workers |
| `tests/test_production_smoke.py` | Fresh install, init, health, ready, chat, approval, job, shutdown |

---

## 11. Implementation order

Complete in this exact order:

1. Add durable session execution mode.
2. Implement HTTP approval resume.
3. Fix SSRF enforcement in `web_extract`.
4. Add REST ownership-loss cancellation.
5. Fix approval-time timeout accounting.
6. Add continuous approval-wait renewal.
7. Make timeout completion lifecycle-safe.
8. Define the script dependency trust boundary.
9. Make provider-close failures visible.
10. Unify the approval event protocol.
11. Remove duplicate critical-path logic.
12. Add multi-worker, failure, security, and smoke tests.
13. Add database migrations and schema conformance tests.
14. Update documentation and threat model.
15. Run the full release gate.
16. Cut `v1.0.0`.

---

## 12. Final release gate

Do not release until every command below passes:

```bash
pytest tests/ -q --tb=short
pytest tests/ -q -m "integration or db"
npm --prefix ui test
npm --prefix ui run check
ruff check ah/
ruff format --check ah/
bandit -r ah/ -ll -x ah/cli/
git diff --check
```

Also verify:

```bash
grep -RInE '\b(TODO|FIXME|XXX|HACK)\b' ah/ ui/src/
```

Returns no production-code result.

And manually verify:

- Fresh install works.
- Upgrade works.
- Rollback plan works.
- TUI chat works.
- HTTP SSE chat works.
- HTTP approval and resume works.
- TUI approval works.
- Scheduled job works.
- Script job works.
- Delegation works.
- Memory works.
- RAG works.
- Cancellation works.
- Multi-worker fencing works.
- Sandbox mode works or is explicitly unavailable.
- Secrets never leak.
- Metrics and audit events contain no sensitive data.
- Monitoring and alerting work.
- Backups and restore work.

---

## 13. Release checklist

- [ ] HTTP approval/resume implemented and tested.
- [ ] Session execution mode durable across restart and workers.
- [ ] `web_extract` SSRF enforcement complete; no security TODO remains.
- [ ] REST ownership-loss cancellation implemented and tested.
- [ ] Approval-time timeout accounting correct.
- [ ] Approval-wait renewal continuous.
- [ ] Timeout terminal event emitted only after ownership release.
- [ ] Script dependency trust boundary implemented or explicitly documented.
- [ ] Provider-close failures visible.
- [ ] Unified protocol contract implemented and tested.
- [ ] Duplicate critical-path logic removed.
- [ ] Multi-worker fencing tests pass.
- [ ] Failure and chaos tests pass.
- [ ] Database migrations idempotent and tested.
- [ ] Schema/code conformance tests pass.
- [ ] Full Python suite passes.
- [ ] UI suite passes.
- [ ] Real gateway/database integration suite passes.
- [ ] Lint, format, typecheck, and security scan pass.
- [ ] Security threat model complete.
- [ ] Documentation matches implementation.
- [ ] Staging deployment passes smoke tests.
- [ ] Monitoring, alerting, backups, and restore verified.
- [ ] Version bumped to `v1.0.0`.
- [ ] Release notes list all fixed defects, supported platforms, limitations, and security guarantees.

---

## 14. Non-goals

Do not claim the following unless explicitly implemented and tested:

- Complete dependency snapshotting for scripts.
- Strong isolation for in-process plugins.
- Protection against a malicious administrator.
- Protection against a compromised host.
- Protection against a compromised third-party extraction provider.
- Guaranteed termination of cancellation-resistant asyncio tasks.
- Cross-process session-mode consistency without durable state.
- Security guarantees from Bandit alone.

A passing test suite and security scanner are necessary, but they are not sufficient. Production completion requires the implementation, integration tests, operational verification, and documentation to agree.
