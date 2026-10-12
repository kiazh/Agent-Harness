# MCP OAuth (Authorization Code + S256 PKCE)

Local browser authorization for Streamable HTTP MCP servers, verified end to
end against a loopback fake issuer plus a real SDK MCP server. This is part
of the default backend installation (`mcp==2.3.0`, `cryptography`, `keyring`).

## Supported flow

1. Open `/mcp`, add an HTTP server and choose **OAuth** (or pick **Configure
   OAuth** on an existing HTTP server). Settings are non-secret: client mode
   (`auto` / `preregistered` / `metadata_document`), client ID, metadata
   document URL, secret *reference*, issuer, scope allowlist, callback port.
2. Choose **Authorize**: review the issuer, scopes, endpoints and loopback
   callback, then confirm. Registration (dynamic, when advertised) happens
   only after this review.
3. Choose **Open browser** (explicit action) or copy the login link from the
   transient overlay. Consent in the external browser reaches the worker's
   `127.0.0.1` listener; the code is exchanged once and the encrypted grant
   is persisted.
4. The overlay reports **Authorized** (authorization only — not connected).
   Choose **Connect** through the existing exact preview/confirmation, then
   discover and call tools through normal broker approvals.
5. Expiring access refreshes automatically before dispatch (same grant keeps
   its identity). **Disconnect** preserves credentials; **Forget access**
   removes them locally, optionally revoking once at the provider.

OAuth authorizes access at a remote service. It never grants harness
execution permissions, never bypasses the external-call approval boundary,
and never replays a tool call after refresh, 401/403, timeout or relogin.

## Management surface

Human control-plane methods (authenticated, never model tools):
`mcp.oauth.preview/start/status/url/open/cancel/forget`. Status is local
state only and launches nothing. Unknown parameters are rejected; one
session cannot operate another session's preview or operation.

## Credential storage and recovery

- Encrypted at rest (AES-GCM) under `~/.agent-harness/oauth/`; file mode
  0600, directory 0700.
- Master key: `AGENT_HARNESS_OAUTH_MASTER_KEY` (base64, 32 bytes) first,
  otherwise the OS keyring (`AgentHarness/MCP-OAuth`). No plaintext
  fallback: without a usable key the state is `storage_unavailable` with
  setup guidance.
- Lost key or corrupt store: authorize again after a confirmed local reset.
  Ciphertext alone is not recoverable; keep a separate master-key backup.
- Headless/remote workers cannot complete a loopback browser callback;
  configure a local worker for authorization.

## Limitations (deferred scope)

- Native loopback authorization only. Remote hosted callbacks, distributed
  operations, device-code, password/implicit grants, JWT assertions,
  DPoP/mTLS and OIDC user login are separate work.
- HTTP credential *profiles* remain unimplemented (`auth_required`).
- Legacy SSE, remote resources/prompts, catalog subscriptions and
  server-initiated callbacks stay disabled.
- Pre-request DNS classification only: no complete DNS-rebinding defense.
- Windows file ACLs are not independently hardened; prefer the native
  credential backend there.
- No live-provider certification: real-vendor behavior beyond the fixture
  contract (exact S256/DCR/refresh/revocation/401 semantics) is operator
  work. Supported registration modes verified locally: public DCR,
  pre-registered public, pre-registered Basic, client metadata document.

## Evidence

- Backend: `tests/test_connectors_oauth_*.py`,
  `tests/test_gateway_connector_oauth.py` (fake issuer + real SDK MCP;
  cross-process refresh; tombstone races; 401/403/timeout side-effect
  counters exactly one).
- UI: `ui/test/mcp-oauth.test.ts` (menu/overlay/cancel/link privacy plus a
  real gateway wrapper over stdio).
- Full suite plus `npm run check`, Ruff, Bandit (zero medium/high
  severity), `uv lock --check`, isolated wheel build with OAuth modules.
- Progress log: `docs/plans/MCP_OAUTH_PROGRESS.md`.
