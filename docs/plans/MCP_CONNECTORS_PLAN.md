# MCP connectors plan

Status: approved direction, partially implemented — see
[MCP_CONNECTORS_PROGRESS.md](MCP_CONNECTORS_PROGRESS.md) for what is real
versus deferred. Nothing here claims existing capability.

## Objective

Let AgentHarness (MCP host/client) connect to user-configured MCP servers
and use their tools through the existing execution, permission, accounting,
and lifecycle boundaries. AgentHarness is NOT exposed as an MCP server in
this plan (separate optional phase, not started).

## Verified dependency baseline (installed and probed 2026-10-11)

- Official Python SDK `mcp==2.3.0` (PyPI latest at review time), protocol
  latest protocol constant `2026-07-28` (`mcp.types.LATEST_PROTOCOL_VERSION`), matching the
  specification links in the commissioning brief. Pinned as a `connectors`
  extra in `pyproject.toml`; default installs stay lean and connector tests
  skip cleanly without it.
- Actual stdio and loopback HTTP fixture handshakes negotiated `2025-11-25`;
  record negotiated metadata rather than hardcoding the latest constant.
- `mcp.ClientSession` takes optional `sampling_callback`,
  `elicitation_callback`, `list_roots_callback` — all default `None`, so
  server-initiated sampling/elicitation/roots stay **disabled** unless a
  future explicit policy opts in (verified by signature inspection).
- `mcp.client.stdio` + `mcp.client.streamable_http` present for the two
  declared transports. Legacy SSE is out of scope unless labeled separately.

## Architecture (adapted to repo conventions)

```text
ah/connectors/
  models.py       # ConnectorConfig validation, status, errors (no raw tokens in public views)
  registry.py     # Inventory + collision-safe namespaced tool lookup
  manager.py      # Runtime-owned lifecycle, bounded deadlines, reconnect policy
  mcp_client.py   # SDK adapter; all transport details stay here
  policy.py       # Effect classification mapped to the existing broker
  service.py      # Shared management/discovery operations (call path deferred, see progress)
```

Non-negotiables (from the brief, restated as build rules):

1. Connector calls enter the same tool pipeline and approval broker as
   built-ins. Transport auth is not action authorization.
2. Server content (descriptions, schemas, annotations, results, resources)
   is untrusted. A read-only annotation never skips approval by itself.
3. Every invocation needs applicable human consent: exact per-action
   approval or a narrow configured trusted-read policy. Unknown or
   side-effecting tools require exact approval.
4. No connector widens a delegated agent's inherited restrictions.
5. Secrets live in the existing secret boundary — never config.yaml,
   prompts, DB JSON, history, or audit payloads.
6. No external tool approves itself, changes host security, or starts
   another connector. No silent sandbox→host fallback.
7. Sampling/elicitation/broad roots default disabled.
8. Local executables run argv-without-shell under an explicit trust
   decision; host launch is not sandbox isolation. Never auto-install
   packages or launch downloaded code.

## Configuration contract

- Stable id, display name, kind (`mcp`), enabled flag, owner/scope,
  transport (`stdio` | `streamable_http`), revision counter.
- Stdio: executable + argv as separate fields, approved cwd, explicit env
  allowlist + secret-name references (resolved at launch, never stored).
- HTTP: validated endpoint (http/https), credential-profile reference.
- Timeouts, result-byte caps, concurrency caps, retry bounds, tool
  allowlist, trust classification (`untrusted` default; `trusted_read`
  only via explicit configuration).
- Unknown fields rejected; invalid bounds rejected before persistence.
  Listing a connector never launches it.
- Durable state: non-secret config + connection metadata in PostgreSQL
  (migrations + ownership checks) when the UI/HTTP management lands;
  this turn persists nothing (in-memory registry).

## Runtime behavior

- Manager owned by RuntimeServices semantics (one instance per worker;
  worker-local connections, never shared across workers as proof of health).
- States: enabled/disabled, connecting, connected, disconnected,
  auth_required, degraded, error — sanitized reason codes only.
- Bounded connect/initialization/discovery/call/shutdown; subprocesses and
  tasks tracked to retirement; cancellation never claims an effect stopped.
- Backoff with jitter + bounded circuit breaker on transients.
- Unknown-outcome rule: a dropped connection around a write reports
  `outcome_unknown`, never auto-retries a side-effecting call.

## Discovery and adaptation bounds

- Pagination, capability negotiation, catalog-change awareness.
- Stable names `connector_id__tool_name` (sanitized to provider limits);
  original names + validated schemas retained internally.
- JSON Schema subset validated before approval/execution; unsupported
  schemas fail closed. Catalog size, schema depth, description length,
  discovery time, and injected context all bounded; on-demand schema
  loading over bulk injection.
- Text + structured results with byte caps and truncation marks;
  images/binary/links typed explicitly, never stringified blindly.
- Resources/prompts are lower-authority untrusted context, on demand only.

## Approval binding (Phase C contract, to be wired)

Approval binds: connector id + owner/scope, config revision, transport
destination or executable identity, credential-profile identity (no secret
values), remote tool name + schema revision, canonical complete arguments,
session mode/backend/workspace, timeout, required capabilities. Exact
arguments re-validated at dispatch; any change requires fresh approval.
One-time approval consumed atomically via the existing durable broker.
Denials never re-request; revocation stops future calls without claiming
to undo dispatched effects.

## Auth and network (Phase D contract, deferred)

- MCP-version authorization discovery + client flow via SDK facilities;
  explicit configured credentials for self-hosted endpoints; OAuth with
  PKCE/audience/state/minimal scopes; loopback callback only where
  appropriate; explicit auth_required/login/logout/revoke states.
- Endpoint/outbound policy enforced at connection, discovery, redirects,
  token endpoints, and fetches — not only at configuration. Private/
  link-local/metadata rejected by default; localhost only via explicit
  scoped admin config. TLS verified; no downgrade redirects; no auth
  headers across unapproved origins. Bounded headers/body/duration/
  redirects/connections. Stdio: minimal env, no shell, capped/redacted
  stderr, owned process tree.

## Milestones and acceptance

1. Installer fixes + honest baseline. (Done — verification report.)
2. Config + runtime manager + isolated stdio/HTTP mock integration.
3. Discovery + namespacing + bounded tool adaptation.
4. Exact approvals + ownership + durable resume.
5. Remote auth + network security validation.
6. CLI/TUI/HTTP management and documentation.
7. One verified service integration, then more connectors.
8. Full cross-entry-point regression and release verification.

This turn targets 1 (done), 2, and 3 (read paths; call execution deferred
to 4). Definition of done for the connector release is unchanged from the
brief and is NOT claimed here.
