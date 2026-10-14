# Agent-callable MCP management proof — mcp_connect / mcp_list / mcp_disconnect (2026-10-14)

Feature: the agent can now add, connect, list, and remove MCP servers
conversationally. The tools go through the existing permission broker: adding
or removing a server pauses for one explicit approval (operation
`tool.mcp_connect` / `tool.mcp_disconnect`, capability `mcp_external_review`),
then executes exactly once against the same configuration store the human
`/mcp` surface uses.

- Repo: `/Users/shadow/machina` · git HEAD `9806bee7c53ac80bcb07a01ad8216be4747ae1b5`
  (working tree + this wave's uncommitted changes)
- Host: macOS (aarch64) · Python `/Users/shadow/machina/.venv/bin/python` 3.14.5
- Target for the live runs: Machina's own MCP server
  (`python -m machina.mcp_server`), which reports server `machina 0.2.0`,
  protocol `2025-11-25`, and exactly three tools: `machina_chat`,
  `machina_session_create`, `machina_status`.

## 1. Live success flow — `01_live_transcript.txt`

Command: `.venv/bin/python docs/verification/proofs/mcp_connect_2026-10-14/run_live_proof.py`

The script drives the real tool path: `action_for_tool` → `build_request` →
`permission_broker.guard` → `registry.execute` (the same seam the agent loop
uses). Excerpts:

```
[mcp_connect] policy verdict: pending (sensitive capability requires explicit approval: mcp_external_review)
             capabilities=['mcp_external_review'] argv=['/Users/shadow/machina/.venv/bin/python', '-m', 'machina.mcp_server']
[mcp_connect] PAUSED needs_approval: operation=tool.mcp_connect status=pending
[mcp_connect] human decision recorded: approved (principal=proof-human)
[mcp_connect] approved digest claimed; executing exactly once
{
  "connected": true,
  "server": {"id": "machina-self", "enabled": true, "transport": "stdio",
             "state": "connected", "server_name": "machina", "server_version": "0.2.0",
             "protocol_version": "2025-11-25"},
  "tools": ["machina-self__machina_status", "machina-self__machina_chat",
            "machina-self__machina_session_create"]
}
```

`mcp_list` (no approval — routine read) then reports the server with
`enabled: true`, `trust: "untrusted"`, `state: "connected"`, `tool_count: 3`.

`mcp_call machina_status` pauses for its own approval and round-trips through
the real child process; the remote text is real host data:

```
Machina status
postgres: PostgreSQL 16.15 (Homebrew) on aarch64-apple-darwin25.6.0
sessions: 171 | chunks: 1135 | memories: 53 (pending: 0)
tools: 31 registered
...
```

`mcp_disconnect` (approved) removes the server: `{"removed": true}`,
`store generation=2 configs=[]`, `live connection: None`, and the final
`mcp_list` returns `{"servers": []}`.

## 2. Live failure compensation — `02_failed_connect_compensation.txt`

`mcp_connect(url="http://127.0.0.1:1/mcp", local_access=True)` connects to a
loopback port that refuses instantly. The call passes the same approval gate,
fails to launch, and the store write is compensated:

```
{
  "connected": false,
  "server": "broken",
  "error": "connection cancelled",
  "store": "configuration removed"
}
store after compensation: generation=2 configs=[]
live connection: None
final mcp_list: {"servers": [], "next_offset": null}
```

Nothing half-configured remains: no config, no tracked connection, no tools.
(`"connection cancelled"` is the pre-existing `MCPConnection` message for a
refused streamable-HTTP launch — reproduced with a direct
`MCPConnection.connect()` on the same endpoint, independent of this feature.
A replaced server's failure path restores its prior configuration instead of
removing it; see `test_failed_connect_restores_replaced_config`.)

## 3. Automated evidence — `03_gates.txt` + tests

- `tests/test_mcp_manage_tool.py` (42 tests): validation (exactly one
  transport, connector-id policy, URL credential/query/fragment/scheme
  rejection, shell-string and missing-executable rejection), allowlist
  name-only enforcement, approval classification (pending in ask/workspace/
  sandbox; full-mode session grant allowed), approval-snapshot requirement
  and post-approval change refusal, store compensation (new → removed,
  replaced → restored, discovery failure → retired), removal semantics
  (remove vs disconnect-only, unknown server), list redaction/owner filter,
  and the live self-connect round trip above through the real tool path.
- Gate suites (required): 196 passed
  (`test_connectors_manager`, `test_connectors_client`, `test_mcp_server`,
  `test_permission_risk_policy`, `test_architecture_layers`).
- Adjacent sweep: 246 + 204 passed across connector/gateway/approval/
  permission/agent/registry suites.
- Full suite (with the local test database from `.env` active): 3953 passed,
  1 skipped, 1 xfailed, plus one pre-existing failure outside this change —
  `tests/test_features_split.py::test_all_feature_methods_accessible_after_split`
  (the chatgpt-provider lane's new `auth.chatgpt.*` RPC registrations; no
  file touched by this change is involved).
- `ruff check` + `ruff format --check`: clean on all touched files.
- Ratchets unchanged: deferred-import census 683 = cap; `tools` module-level
  core imports 21; `connectors` 0; `permissions` 1 (no delta table needed —
  the new connectors-side module carries no function-level imports).

## 4. Design notes

- **Shared core**: `machina/connectors/management.py` owns the canonical
  request identity (`connect_spec`/`disconnect_spec`), the composite
  configure → connect → compensate flow, and removal. Both the tools and
  `machina/permissions/tools.py` call it, so the guard-time digest and the
  execution-time rebuild are byte-identical. The launch/discovery primitive
  `ConnectorService.connect_and_discover` is now shared by the human
  management route (`connect_authorized`) and the agent path, replacing the
  duplicated inline block in `execution.py`. Config writes go through the
  same `ConnectorStore`/`ConnectorService.configure/remove` internals the
  `/mcp` RPCs use (`_configure_locked`/`_remove_locked` splits only move the
  lock boundary so composite flows stay atomic).
- **Approval classification**: reused the existing `mcp_external_review`
  review capability (defined in `REVIEW_CAPABILITIES`) — no new capability
  class, no policy change. Every `mcp_connect`/`mcp_disconnect` is `pending`
  in ask/workspace/sandbox; full mode still requires its explicit live
  session grant. `mcp_list` stays `context.read` (routine).
- **env_allowlist**: names only (validated by the connector's own
  `[A-Z_][A-Z0-9_]*` rule); values are never accepted as arguments, and the
  exact list is bound into the approved digest and shown on the card. There
  is no silent path that copies host environment values to a new server.
- **Approval lifecycle in this proof**: the headless runs use the minimal
  explicit path — the guard PAUSES (no handler), a decision is recorded
  through the permissions store (the same transition the TUI/approvals API
  performs), and the retry claims the exact digest and grants exactly one
  execution. The approval store backend is in-memory because no test
  database was connected; the audit events in the raw transcript show the
  pending → allowed transition. The automated live test stubs only the
  session-ownership DB read (`machina.tools.agents.db.fetchval`); every
  other step is production code.

## 5. UI notes

`/mcp` reads the same store through `mcp.list`, so agent-added servers appear
there automatically (manage/connect/disconnect/remove like any other).
Agent-added HTTP servers that need OAuth land in `auth_required`; the user
authorizes them via `/mcp → Authorize`, and the agent can call them
afterwards.
