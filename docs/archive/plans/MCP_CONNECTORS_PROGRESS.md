# MCP connector progress and successor notes

**Current phase (2026-10-12):** saved local configuration, interactive management
and broker-bound native calls are implemented. See [MCP setup](../../MCP.md) and
[current release evidence](../../verification/RELEASE_READINESS.md). Earlier sections
below record the historical read-only/library phase; its deferrals are superseded
only where the final integration section explicitly says so.

Verified on 2026-10-11 in `/home/user/Agent-Harness1`. This is a verified
connector **library plus read-only inspection phase**, not completed agent-loop integration.
Read [MCP_CONNECTORS_PLAN.md](MCP_CONNECTORS_PLAN.md) for the remaining contracts.

## Checkout and boundaries

Initial reconnaissance:

```text
$ git log --oneline -3
dc77017 profile
0331634 thme select'
9ef4ed4 changes{
$ git branch --show-current
main
$ git status --short
 M install.sh
 M pyproject.toml
 M tests/test_integration_permissions.py
?? ah/connectors/
?? docs/archive/plans/MCP_CONNECTORS_PLAN.md
?? docs/verification/
?? tests/support/mcp_fixture_server.py
?? tests/test_connectors_config.py
?? tests/test_connectors_policy.py
?? tests/test_installer_env.py
?? tools/install-helpers.sh
```

The installer changes and existing connector scaffolding were already present.
They were preserved. No commit, push, deployment, git configuration change, or
working-tree reset/stash was performed. Tests use the dedicated
`agentharness_test` database only; no live credentials, production database,
shell startup files, or operator terminal were modified.

## Milestones: implemented versus deferred

| Milestone | Evidence and status |
| --- | --- |
| Validated inventory and policy | Config, registry, and fail-closed schema tests; in-memory only. |
| Stdio wire transport | Actual SDK fixture initialize, list, and call; subprocess PID retirement checked. |
| Streamable HTTP wire transport | Actual SDK fixture on a free 127.0.0.1 port; initialize, list, and call with explicit local consent. |
| Task-safe lifecycle | Owner task enters and exits all SDK contexts; six repeated same/cross-task cycles leave no fixture processes or shutdown errors. |
| Bounded discovery and results | Entire discovery deadline; catalog count; description and returned-result bounds; schema validation before dispatch. |
| Manager behavior | Injected-client tests for transient-only retries, backoff, circuit open/reset, idempotence, and serialized retirement/reconnect. |
| Management service contracts | Injected-client tests for inventory, status, connect/disconnect, list/describe, refusals, and distinct connectors. Read-only gateway `mcp.list` / `mcp.status` and TUI `/mcp` inspection added on 2026-10-11. |
| Agent tool exposure and exact broker approval | **Deferred.** There is no service `tools.call`, native tool adapter, or agent-loop wiring. |
| Durable config, owner/scope enforcement, resume | **Deferred.** No connector PostgreSQL migration or configuration ingestion. The worker runtime now owns the shared in-memory service and its shutdown. |
| HTTP OAuth and credential transport | **Deferred.** Configured credential profiles refuse with `auth_required`; supplied values are not silently ignored. |
| Management CLI/TUI/HTTP UI | **Partial:** TUI server picker and list/status backed by real read-only gateway routes. Configuration ingestion, persistence, connect/disconnect controls, CLI and HTTP management remain deferred. |
| Real third-party service integration | **Deferred.** Synthetic SDK fixtures establish wire behavior, not compatibility with any particular external service. |

## Teardown blocker: empirical verdict and fix

The original cross-task probe reproduced
`RuntimeError: Attempted to exit cancel scope in a different task` from
`AsyncExitStack.aclose()`. The fixture wrote its PID before initialize. After
disconnect, `os.kill(pid, 0)` polling established **process_exited=True** and
`ps -axo pid=,command=` found no fixture matching the per-test `--name` UUID.
Thus, in that probe, SDK subprocess shutdown completed before the scope error:
there was no persistent subprocess leak. This is not a guarantee for every
server or failure mode.

Expanded cycling exposed a second symptom: damaged task/cancel-scope ownership
could hang a subsequent connection. The SDK contexts had been entered through
`asyncio.wait_for`, which itself creates a task; even same-task public API calls
could therefore close them from a different internal task.

`MCPConnection` now creates one persistent owner task. That task opens the
transport and session, signals initialization readiness, waits for a stop event,
and closes its own exit stack. Initialization uses `asyncio.timeout` inside the
owner rather than moving context entry to a `wait_for` child task. Disconnect
from any gateway task signals the owner and waits through a shield.

Caller shutdown waits are bounded. If a deadline expires, the connection reports
`degraded`, retains the cleanup task, and refuses replacement until retirement
finishes. It does not cancel the SDK's process cleanup to satisfy the caller's
deadline. A gated-cleanup regression proves this tracking behavior. The actual
fixture regressions prove normal-process retirement; they do not prove instant
termination of arbitrary hostile process trees at the configured deadline.

## Other defects reproduced red, then fixed

- SDK tool schemas use `input_schema`; the initial adapter read the wrong field.
  SDK-generated `title` annotations are supported without weakening validation.
- Truncation markers and structured copies could exceed `result_limit`; the
  returned text plus structured content now share one UTF-8 byte budget.
- Server tool errors could echo launch secrets. Known supplied values are
  redacted in returned errors/text/structured content, stderr, and metadata.
- A dropped dispatched call now reports `outcome_unknown`, like a timeout;
  neither path retries. A received SDK tool-error result remains a known failure.
- Unsupported/malformed schema branches, combinator siblings, and typed
  additional properties could evade validation. Schema preflight checks all
  branches; unsupported keywords fail closed. JSON boolean/number distinctions
  and finite JSON values are enforced.
- Permanent connection failures and authentication failures could retry or open
  a transient circuit. Only explicitly transient failures retry/count toward it.
- Failed connection status could disappear, and reconnect could overlap prior
  teardown. Failed clients remain tracked and per-connector locks cover cleanup.
- Service/manager inventories could differ; unknown disconnects and stale
  disconnected catalogs were accepted. Shared inventory and live-state checks
  now enforce these contracts.
- Authentication state was overwritten during cleanup. `auth_required` now
  survives disconnect. HTTP credential profiles fail closed while auth is deferred.
- Per-page timeouts allowed unbounded pagination. Discovery has a total deadline
  and rejects repeated cursors.
- DNS aliases of metadata addresses bypassed local-consent checks. Resolved
  metadata IPs remain forbidden even with local consent.
- String flags and fractional integer bounds were accepted. Configuration types
  now fail before launch. Credential-bearing userinfo/query/fragment endpoints
  are rejected rather than exposed in public summaries; HTTP query endpoints
  are intentionally unsupported in this phase.
- Sanitized catalog names could overwrite an entry. Collisions are rejected
  before replacing the previous catalog; names respect the 128-character bound.

## Dependency and protocol evidence

`mcp==2.3.0` is installed in `.venv` and pinned in the `connectors` extra.
Imports remain lazy; the client test module uses `pytest.importorskip("mcp")`.
Config/manager/service tests use injected clients and require no SDK transport.
A disposable Python module blocked `mcp` imports through `sys.modules` and ran
the same five test modules: **59 passed, 1 skipped in 0.52s**. This confirms the
client module skips cleanly while non-SDK contracts remain testable. It simulated
an unavailable SDK without uninstalling or changing the project's virtualenv.

Both real wire probes negotiated **2025-11-25**. This is observed metadata,
not a hardcoded compatibility target. The SDK's latest protocol constant is
2026-07-28; that constant does not establish the negotiated protocol.
Transport streams are a two-tuple. SDK fields include `server_info`,
`input_schema`, `next_cursor`, `structured_content`, and `is_error`.

The adapter supplies no sampling, elicitation, or roots callbacks. The fixture's
`ask_user` now sends an actual SDK elicitation request; the response is
`Elicitation not supported`. The test fails if either model completion method
is reached. This proves that elicitation path; separate sampling/roots wire
probes are not claimed.

## Verification commands and results

Run from the repo root using `.venv/bin/python -m ...`; the pip shebang is stale.

```sh
.venv/bin/python -m pytest -q tests/test_connectors_client.py tests/test_connectors_config.py tests/test_connectors_policy.py tests/test_connectors_manager.py tests/test_connectors_service.py --tb=short -p no:cacheprovider
# 84 passed in 8.58s (25 client, 19 config, 17 policy, 11 manager, 12 service).

.venv/bin/python -m pytest -q -s tests/test_connectors_client.py -k 'initialize_discover_and_call_round_trip' -p no:cacheprovider
# 2 passed; stdio and HTTP each negotiated 2025-11-25.

AGENT_HARNESS_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agentharness_test .venv/bin/python -m pytest -q --tb=short -p no:cacheprovider
# Initial full run: 2039 passed, 5 skipped in 82.93s.
# Final run after the last ten configuration regressions: 2049 passed, 5 skipped in 78.27s.

npm --prefix ui run check
# Exit 0: typecheck, 132 passed, 5 skipped, lint.
# The five UI gateway/DB tests were skipped because their explicit opt-in env was unset.

.venv/bin/python -m ruff check ah/connectors tests/test_connectors_client.py tests/test_connectors_config.py tests/test_connectors_policy.py tests/test_connectors_manager.py tests/test_connectors_service.py tests/support/mcp_fixture_server.py
# All checks passed.
.venv/bin/python -m ruff format --check ah/connectors tests/test_connectors_client.py tests/test_connectors_config.py tests/test_connectors_policy.py tests/test_connectors_manager.py tests/test_connectors_service.py tests/support/mcp_fixture_server.py
# 13 files already formatted; repository line length 100.
.venv/bin/python -m bandit -r ah/connectors/
# 0 medium, 0 high, 7 low. Unfiltered Bandit exits 1 for those low findings.
.venv/bin/python -m bandit -r ah/connectors/ -ll
# Exit 0: no medium/high findings.
git diff --check
# Exit 0.
```

No suite failures required a clean-HEAD comparison; no comparison worktree was
needed. Do not invent a
baseline failure or treat skipped tests as exercised behavior.

## OpenCode reference

The user suggested borrowing [OpenCode MCP tooling](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/mcp/index.ts).
The indexed upstream source illustrates explicit transport cleanup on failed
connect, closing clients on discovery failure, and instance teardown finalizers.
Direct retrieval of that path returned 404 during this review, so it was not
treated as a verified current checkout. No OpenCode code was vendored/copied.
Its TypeScript SDK lifecycle cannot substitute for owning Python AnyIO contexts
in the task that entered them. Keep these language/runtime distinctions explicit.

## Successor priorities and honest limitations

1. Wire a single connector manager into `RuntimeServices`, with cancellation and
   worker shutdown tests before exposing it through gateway requests.
2. Build the native tool adapter and exact broker binding from the plan, including
   owner/scope, config/schema revisions, inherited delegation restrictions,
   canonical arguments, destination identity, denial/revocation, and durable resume.
   Synthetic fixture calls here are explicitly authorized tests; the low-level
   adapter itself is not an authorization boundary.
3. Add concurrency enforcement and lazy relevant tool selection in the real
   agent loop. The configuration field alone is not enforcement. Preserve bounded
   context without placing entire catalogs into every prompt.
4. Add durable non-secret inventory, ownership checks, authenticated gateway/HTTP
   routes, typed contracts, and the interactive management UI together.
5. Implement OAuth/explicit credential transport and complete outbound enforcement:
   DNS rebinding, redirects/origin changes, token endpoints, and transport-level
   body limits remain unproven. Current host checks are not complete SSRF protection.
6. Verify an authorized real service, packaging with/without the optional extra,
   and cross-entry-point approval/resume/cancellation before release claims.

Result caps bound normalized returned content, not the SDK's memory use while
receiving a response. Registry state is in-memory and catalogs are explicitly
refreshed; subscriptions/catalog-change invalidation are not implemented.
Resources/prompts, legacy SSE, and server-initiated model/filesystem access are
not exposed. Docker, other OSes, live-provider calls, and production deployment
remain outside this evidence. Do not claim a completed connector product or
zero remaining bugs from this library verification.

## Read-only gateway and TUI inspection — 2026-10-11

A lazy runtime-owned ConnectorService now supplies authenticated `mcp.list {}`
(`{servers:[...]}`) and `mcp.status {id}` (`{server:{...}}`). Inspection uses no
SDK/DB initialization and launches/discovers nothing. Unknown IDs return 1002;
invalid IDs return -32602. Owned gateway/runtime shutdown closes connections;
HTTP adapters borrow the runtime and cannot close other consumers’ connections.

The TUI adds `/mcp` (server picker), `/mcp list`, `/mcp status <id>`, help and
argument completion. Empty inventory is explicit. These are this harness’s
worker connections, not servers managed by OpenCode or Codex. Inventory is still
in memory and default-empty: adding read-only inspection does not supply durable
server configuration, connection controls, OAuth, agent tool calls, or third-party
service compatibility. No catalog/mock is presented as remote execution.

New tests cover actual stdio gateway RPC, lazy inspection/auth/input validation,
shared sanitized views, real SDK connected→disconnected state and PID retirement,
owned/borrowed lifecycle, UI command dispatch and real-gateway empty inspection.
Final aggregate evidence is in TUI_OVERHAUL_REVIEW.md and the release review.

## 2026-10-12 — integrated native tool and management phase

This section supersedes the earlier deferred configuration/tool-dispatch rows.
MCP is now a default dependency. Human gateway/TUI management configures saved
servers, previews exact launch, explicitly connects, inspects tools, disconnects
and removes. Configuration persists atomically in installation-local `mcp.json`
with revisions/generations and worker invalidation; this is a deliberate local
configuration alternative to the originally planned PostgreSQL connector table.
Connection health remains worker-local, and restart never auto-launches.

`mcp_list` pages catalogs; `mcp_describe` loads individual schemas; `mcp_call`
uses the actual agent broker/registry pipeline. Complete config/schema/argument
identity binds approval, stale review refuses dispatch, unknown tools require
exact consent in every mode, explicit trusted-read allowlists are supported,
and parent tool/mode restrictions remain authoritative. Host stdio is refused
in sandbox mode. Typed uncertain outcomes travel through gateway/HTTP/TUI.
The existing exact HTTP continuation path rebuilds current connector bindings.

Review fixes include immutable approved launch snapshots, cwd/executable identity,
venv launcher preservation and bounded concurrency/configuration waits. HTTP
redirect/proxy inheritance is disabled and destinations are rechecked. Actual
SDK fixture agent calls cover approval pause/resume, errors, truncation, timeout
uncertainty and elicitation refusal. Setup and limitations: [MCP.md](../../MCP.md).
Final verification is recorded in [release readiness](../../verification/RELEASE_READINESS.md).

Still deferred: OAuth and HTTP credential-profile auth, remote resources/prompts,
subscriptions, automatic executable installation, complete DNS-rebinding/body
allocation protections, and verified third-party/live-provider integrations.
The implementation is not an MCP server and does not expose external apps'
connection inventories.
