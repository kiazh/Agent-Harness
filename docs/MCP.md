# Using MCP servers

MCP support is part of the default backend installation. Restart/reinstall after updating so the pinned SDK dependency and new TUI commands are available.

1. Open `/mcp`, choose **Add MCP server**, and select local stdio or HTTP.
2. Give it a lowercase ID (up to 64 characters), display name and launch fields.
3. For stdio, enter the executable and arguments separately, for example `node` and `["/absolute/path/to/server.js"]`. The executable/server must already be installed; the harness does not download it. Choose its working directory, or use the configured workspace.
4. Optional advanced JSON supports `secret_refs`, `tool_allowlist`, `trust` and `local_access`. Leave `trust` as `untrusted` until you have independently verified specific read-only tools. Private HTTP destinations require `local_access: true`; metadata destinations are always refused.
5. Select the saved server, choose **Connect**, review the complete preview and confirm. This launches the selected process or contacts the endpoint and discovers its tools. Inspection alone never launches anything.
6. Ask the agent to use the connected server. It pages `mcp_list`, reads a selected `mcp_describe`, and calls `mcp_call` with `{name: "server__tool", arguments: {...}}`. Pending tool approvals use the ordinary approval controls.
7. Use **Disconnect** to retire a connection, **View tools** to refresh discovery, or **Remove** to retire and delete its saved configuration.

Provider secrets remain in the existing environment/mounted/Vault/AWS secret boundary. `secret_refs: ["MY_MCP_TOKEN"]` resolves that named secret at launch and passes it to the stdio server. Never put tokens in command arguments, endpoints or configuration JSON. Missing references refuse launch. Errors and server output redact known launch secret values.

## Authority and lifecycle

Human management RPC methods are `mcp.configure`, `mcp.remove`, `mcp.preview`, `mcp.connect`, `mcp.disconnect` and `mcp.tools`; existing `mcp.list` and `mcp.status` remain read-only. `mcp.connect` takes the current session and the digest returned by `mcp.preview`. The human confirmation is recorded/claimed through the existing broker. Configuration is not a model tool, and server callbacks cannot launch another connector or approve a tool.

Native calls bind the complete non-secret configuration/revision, resolved executable identity, schema, remote name and canonical arguments to the broker digest. The existing session/mode/backend and parent tool restrictions also apply. Explicit host configuration may grant narrowly allowlisted trusted reads; server annotations cannot. Altered configurations/schemas/arguments, missing connections, disabled servers and owner mismatches refuse dispatch.

Configurations are installation-local JSON in `~/.machina/mcp.json`, mode 0600, with advisory locks, atomic replacement and monotonic generations/revisions. This deliberately follows existing local configuration instead of adding the originally planned PostgreSQL connector table. Health and transports are worker-local. Inspection reloads configuration without launching; mutations retire stale transports. Workers on different homes have different inventories. Administrator manual changes must advance generation/revision; rollback of a loaded generation fails closed.

Connections do not autostart after worker restart. Reconnect through the menu. A new worker cannot use another worker's connected process. SDK contexts are entered and retired by one owning task; callers may disconnect from other tasks. A shutdown timeout retains cleanup tracking and refuses replacement rather than pretending retirement succeeded.

Calls have a total deadline including concurrency wait. Waiting without dispatch is a known failure; a dispatched timeout/dropped transport is `outcome_unknown`, rendered explicitly in the TUI and retained in typed agent outcomes. No remote call is automatically retried. Connection retries remain bounded and transient-only. Cancellation never proves a remote effect was undone.

## Machina as an MCP server

Machina also acts as an MCP server: `python -m machina.mcp_server` serves the phase-1 shim over stdio to hosts such as OpenCode, Codex and Claude Desktop. Register it with a Python that has Machina installed (a checkout virtualenv is easiest):

```bash
opencode mcp add machina -- /path/to/machina/.venv/bin/python -m machina.mcp_server
```

Project-scope registration writes the host's own config file (`opencode.json` for OpenCode), which this repository gitignores because the interpreter path is host-specific; other hosts use their equivalent stdio configuration. The shim exposes exactly three tools: `machina_status` (read-only, same data path as `machina status`), `machina_chat` (one headless `machina chat` turn) and `machina_session_create`. Phase 1 is text-only — attachments/images are refused — and a turn that pauses for approval (CLI exit 3) returns `{"status": "paused_for_approval", ...}` with the pending request ids instead of hanging; the caller resolves the request in Machina and resumes the session. Children run in the host's launch directory with a minimal environment allowlist, their own process group and a bounded kill-tree deadline; stdout is the reply and stderr is diagnostics-only, so child secret material never enters a tool result.

The `j9` gate journey proves the OpenCode → shim → Machina-session roundtrip end to end and SKIPs unless `opencode` is installed and the project has a registered `machina` server; the recorded round trip, database token check and unit + stdio-handshake coverage are in the [interop proof](verification/proofs/interop_2026-10-14/README.md).

## Scope and remaining work

Only the schema subset enforced by `machina/connectors/policy.py` is accepted: typed values, properties/required, arrays, enum/const, numeric/string/item bounds and supported combinators. Unsupported constraints or malformed schemas fail closed rather than being silently ignored.

Supported: stdio and Streamable HTTP, tools, interactive configuration/connect/disconnect/removal, durable local configuration, broker-bound native tool calls, existing exact HTTP approval continuation machinery, and OAuth browser authorization (Authorization Code + S256 PKCE) for HTTP servers with encrypted persistent credentials, refresh and forget/revoke — see [MCP OAuth](MCP_OAUTH.md).

Not supported: HTTP credential-profile authentication, legacy SSE, remote resources/prompts, catalog subscriptions, server-initiated model/filesystem callbacks, automatic executable installation, shared-process connections or complete transport-level DNS-rebinding defenses. Stdio is host execution and is refused in sandbox mode. HTTP disables redirects and inherited proxies and rechecks destinations; normalized result caps do not bound every SDK receive allocation.

SDK fixture tests prove transport and harness pipeline behavior. No particular external MCP vendor or live-provider quality gain is implied. See [release evidence](verification/RELEASE_READINESS.md) and the historical [connector progress](archive/plans/MCP_CONNECTORS_PROGRESS.md).

OAuth is implemented per the [implementation handoff](archive/plans/MCP_OAUTH_IMPLEMENTATION_HANDOFF.md) with progress in [MCP OAuth progress](archive/plans/MCP_OAUTH_PROGRESS.md). Remote hosted callbacks, device-code grants, OIDC user login, all-vendor certification and total DNS-rebinding defense remain deferred.
