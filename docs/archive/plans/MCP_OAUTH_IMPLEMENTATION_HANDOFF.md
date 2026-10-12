# MCP OAuth implementation plan and Muse 1.3 handoff

> **For the implementing agent:** Work through this document sequentially. Use
> `superpowers:executing-plans` if that skill is available; otherwise use the
> checkbox tasks directly. Do not depend on a particular model, plugin or skill
> package being installed. Do not implement only discovery, a login button or
> mocks and call OAuth complete. Each task has an evidence gate.

**Goal:** A local AgentHarness user can add an HTTP MCP server, authorize it in
an external browser, persist credentials securely, reconnect after restarting,
refresh expiring access, and forget/revoke access through `/mcp`. Agent tool calls
retain the existing approval broker and never replay automatically after OAuth.

**Architecture:** Separate human OAuth authorization from MCP transport and tool
execution. Reuse SDK data models and PKCE utilities, with a harness-owned bounded
flow, secure store and network policy. Attach a bearer-only `httpx2.Auth` adapter
to the existing SDK HTTP client; it must yield the resource request exactly once.

**Tech stack:** Python 3.11+, asyncio, MCP SDK pinned at 2.3.0, httpx2 for the MCP
transport, httpx for a separately guarded OAuth client, cryptography AES-GCM,
OS credential storage through keyring, PostgreSQL approval records, TypeScript
pi-tui, pytest/node:test.

**Spec:** Sections 1–12 of this same document are the design specification;
sections 13–17 are its implementation and verification plan. This avoids a
missing companion document when the handoff is copied elsewhere.

**Status on 2026-10-12:** This is a plan, not implemented OAuth. Browser login,
token persistence and OAuth refresh remain unbuilt. Earlier MCP work is real:
configuration, launch review, stdio/HTTP fixture connections, discovery and native
broker-approved calls. Do not erase those working paths while adding OAuth.

## Contents

1. [Working rules and recon](#1-working-rules-and-recon)
2. [What exists and exactly where to integrate](#2-what-exists-and-exactly-where-to-integrate)
3. [Chosen approach and alternatives](#3-chosen-approach-and-alternatives)
4. [Sources and protocol scope](#4-sources-and-protocol-scope)
5. [Configuration and public contracts](#5-configuration-and-public-contracts)
6. [Flow lifecycle and ownership](#6-flow-lifecycle-and-ownership)
7. [Network and metadata validation](#7-network-and-metadata-validation)
8. [Registration, PKCE and callback](#8-registration-pkce-and-callback)
9. [Secure credential persistence](#9-secure-credential-persistence)
10. [Refresh, identity and request execution](#10-refresh-identity-and-request-execution)
11. [Gateway and TUI behavior](#11-gateway-and-tui-behavior)
12. [Errors, limits and deferred scope](#12-errors-limits-and-deferred-scope)
13. [File map and interfaces](#13-file-map-and-interfaces)
14. [Sequential implementation tasks](#14-sequential-implementation-tasks)
15. [Security and acceptance matrix](#15-security-and-acceptance-matrix)
16. [Verification commands and release evidence](#16-verification-commands-and-release-evidence)
17. [Continuation prompt and checkpoints](#17-continuation-prompt-and-checkpoints)

## Global constraints

- Work in the AgentHarness checkout. Keep unrelated user changes intact.
- Do not commit, push, publish, deploy or change git configuration unless the
  operator explicitly changes the existing authorization. Tasks below say
  checkpoint, not commit.
- Never inspect the live `.env`, dump live environment variables, read existing
  OAuth secrets, contact paid providers, initialize the application database or
  edit shell startup files to test this feature.
- Database tests may use only
  `postgresql://postgres:postgres@localhost:5432/agentharness_test`, provided as
  `AGENT_HARNESS_TEST_DATABASE_URL`. Use private schemas where existing tests do.
- Use `.venv/bin/python -m ...`; do not trust the pip script's old shebang.
- Python floor 3.11; use `asyncio.timeout`, not APIs available only on 3.14.
- Keep MCP 2.3.0 pinned initially. An SDK upgrade requires separate compatibility
  evidence; it is not the default solution to missing integration.
- OAuth authorizes access at a remote service. It never grants harness execution
  permissions. FULL mode never bypasses the external-call approval boundary.
- No remote tool is automatically retried after token refresh, 401, 403,
  disconnect, timeout, ambiguous token exchange or a new browser login.
- No tokens, codes, verifiers, client secrets or browser state in agent prompts,
  tool results, plaintext config, PostgreSQL approval proposals or audit logs.
- Do not enable sampling, elicitation or roots callbacks as part of OAuth.
- Add default dependencies needed for the feature; this must work in a normal
  installer/source installation, without an undocumented OAuth extra.
- Update documentation to describe supported scope accurately. OAuth completion
  does not close unrelated staging/production-release gates.

## Review focus

These five conditions are particularly easy to miss. They have explicit tests
in tasks 3, 4, 6, 7 and 9 respectively:

1. Metadata changes a destination after review: never send tokens/codes/secrets
   to the substituted issuer, token endpoint or resource.
2. A random local process hits the callback first: wrong state/path/Host must not
   consume the legitimate user's attempt or exchange a code.
3. Two workers refresh a rotating token concurrently, or one crashes mid-refresh:
   one exchange maximum; uncertain rotation must demand reauthorization.
4. Login finishes after remove/logout/replacement: cancellation/generation checks
   prevent deleted credentials from being resurrected.
5. A remote mutation returns 401/403 or drops the stream: no automatic browser,
   refresh-plus-replay or second invocation, even if authentication later succeeds.

---

## 1. Working rules and recon

Run and record before editing:

```bash
pwd
git branch --show-current
git log --oneline -3
git status --short
.venv/bin/python -m pip show mcp httpx2
node --version
```

The research checkout was `/home/user/Agent-Harness1`, on `main`; HEAD observed
at the end of research was `4c945cc`. This is a locator, not an assertion that
Muse's checkout is unchanged. Check actual files and signatures. No AGENTS.md was
found in the prior review; search again if the checkout has changed.

Read first, in this order:

1. `docs/MCP.md` — current supported behavior and boundaries.
2. `ah/connectors/models.py`, `store.py`, `service.py`, `manager.py`.
3. `ah/connectors/mcp_client.py`, `execution.py`, `policy.py`.
4. `ah/permissions/broker.py`, `tools.py`, `policy.py` and `store.py`.
5. `ah/gateway/features/connectors.py` and method registry `features/__init__.py`.
6. `ui/src/features/mcp.ts`, `features/types.ts`, `gateway.ts`, `protocol.ts`.
7. Existing connector tests and `tests/support/mcp_fixture_server.py`.
8. `ah/security/secrets.py`, `ah/memory/redaction.py`,
   `ah/observability/safe_logging.py` and `ah/core/runtime.py`.

Do not reread every vendored ECC skill/agent. OAuth belongs in connectors,
security, the authenticated gateway and the MCP UI. Those Markdown libraries are
unrelated package data and should remain byte-for-byte intact.

## 2. What exists and exactly where to integrate

| Existing file | Verified behavior | OAuth integration requirement |
| --- | --- | --- |
| `models.py` | `ConnectorConfig` validates non-secret stdio/HTTP config; HTTP has `credential_profile`, currently deferred | Add strict nested non-secret `oauth`; do not repurpose `credential_profile` as a raw token |
| `store.py` | Atomic `~/.agent-harness/mcp.json`, revisions, tombstones and generation; Unix/Windows locking | Persist OAuth settings only; credentials use a separate encrypted store |
| `service.py` | Shared `ConnectorService`, configuration lock, inventory refresh, stale-connection retirement | Own the OAuth coordinator and reconcile public auth identity during refresh |
| `manager.py` | Factory-injected lifecycle, transient connection retries, per-connector lock | Supply auth-aware clients without removing fake-factory support or existing retry rules |
| `mcp_client.py` | Dedicated owning task enters/exits SDK AnyIO scopes; HTTP uses explicit httpx2 client, no redirects/proxies | Attach bearer-only auth; preserve owner-task teardown |
| `execution.py` | Exact launch and call digests, approved immutable config, bounded waits, `outcome_unknown` | Bind non-secret grant identity; token preflight happens before dispatch, never browser login |
| `ah/tools/mcp.py` | Native `mcp_list`, `mcp_describe`, `mcp_call`; uncacheable | Surface sanitized auth-required results; do not expose management/login as model tools |
| `gateway/features/connectors.py` | Authenticated config/preview/connect/disconnect/tools methods | Add human-only OAuth management methods with session/operation ownership |
| `ui/src/features/mcp.ts` | Interactive add/manage menu and exact connection preview | Add authorize/status/forget flow using ephemeral UI overlays |
| `ui/src/gateway.ts` | RPC requests default to a 30,000ms timeout | Browser waits must be operation+poll, not a five-minute RPC |
| `runtime.py` | Runtime services own connector shutdown | Cancel auth tasks and close callback listeners at shutdown |

### SDK facts: verify these before coding

Installed SDK sources observed:

```text
.venv/lib/python3.14/site-packages/mcp/client/auth/oauth2.py
.venv/lib/python3.14/site-packages/mcp/client/auth/__init__.py
.venv/lib/python3.14/site-packages/mcp/shared/auth.py
```

Do not hardcode `python3.14` into code or scripts. Locate files with
`importlib.util.find_spec('mcp')` or inspect the installed package.

The installed SDK exposes:

```python
from mcp.client.auth import PKCEParameters
from mcp.shared.auth import OAuthToken, OAuthClientMetadata
```

`PKCEParameters.generate()` supplies a verifier and S256 challenge.
`OAuthToken` is a Pydantic model; inspect its actual fields and validators.
`AuthorizationCodeResult` contains `code`, `state`, `iss` but does not represent
all callback error cases: use a harness callback result type for errors.

`OAuthClientProvider` accepts storage and redirect/callback handlers. Its
`_auth_flow` currently yields the original protected request, handles 401 or
insufficient scope, performs OAuth, then yields the original request again.
That replay is the principal reason not to attach it directly to MCP tool calls.
It also logs OAuth exceptions, may fall back to legacy endpoints, and is not
sufficient by itself for this harness's SSRF/secret-handling policy.

The existing HTTP request hook rejects destination changes relative to the MCP
origin. Do not broaden this hook to permit every OAuth endpoint: metadata,
registration and token exchange use a separately guarded client.

## 3. Chosen approach and alternatives

**Selected:** Harness-owned human flow plus SDK PKCE/data models, then a
one-request bearer adapter for normal MCP HTTP traffic. More integration code,
but explicit ownership, no tool replay and precise network/credential bounds.

**Rejected default:** Put `OAuthClientProvider` directly on the SDK client. It
handles useful protocol details but introduces automatic reauthorization/replay
and a browser wait inside connect/call deadlines. A future audited adapter may
reuse it only after proving all equivalent guarantees; this plan does not require
that risky refactor.

**Rejected default:** Ask the user to paste tokens into JSON or ship only an
Authorization header field. That is static bearer configuration, not complete
OAuth, and would leave browser consent, refresh and secure persistence unbuilt.

This implementation supports native/local TUI authorization. HTTP control-plane
methods can inspect/revoke credentials and obtain a local operation's status,
but a browser callback must reach the worker's loopback listener. Remote hosted
login callbacks, distributed callbacks, device-code grants and tunneling are
separate features; return a clear unsupported-local-browser error instead of
pretending a remote browser can reach a container's `127.0.0.1`.

## 4. Sources and protocol scope

Read the current official documents, using the dated MCP version as the baseline.
Do not silently swap in a new draft or promise blanket MCP certification.

- [MCP authorization, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization): HTTP auth scope, discovery and registration choices. STDIO retains environment credentials. The dated text describes a subset of OAuth 2.1, which it references as an IETF draft.
- [RFC 9728: protected-resource metadata](https://www.rfc-editor.org/rfc/rfc9728): identify the authorization service from the resource's metadata.
- [RFC 8414: authorization-server metadata](https://www.rfc-editor.org/rfc/rfc8414): discover endpoints and validate issuer identity.
- [OIDC discovery](https://openid.net/specs/openid-connect-discovery-1_0.html): additional server-metadata discovery; this does not add user login/ID-token validation to the harness.
- [RFC 7636: PKCE](https://www.rfc-editor.org/rfc/rfc7636): S256 protects the authorization-code exchange.
- [RFC 8252: native apps](https://www.rfc-editor.org/rfc/rfc8252): external browser and loopback redirect design.
- [RFC 8707: resource indicators](https://www.rfc-editor.org/rfc/rfc8707): bind authorization/token requests to the intended resource.
- [RFC 9207: response issuer](https://www.rfc-editor.org/rfc/rfc9207): validate callback `iss` and advertised issuer-response support.
- [RFC 7591: dynamic registration](https://www.rfc-editor.org/rfc/rfc7591): register supported public-client metadata and validate the response.
- [RFC 6749: grant and refresh semantics](https://www.rfc-editor.org/rfc/rfc6749): code/refresh requests and returned token scope.
- [RFC 6750: bearer usage](https://www.rfc-editor.org/rfc/rfc6750): Authorization header and authentication/scope challenges.
- [RFC 7009: revocation](https://www.rfc-editor.org/rfc/rfc7009): supported server-side revocation endpoint behavior.
- [RFC 9700: security BCP](https://www.rfc-editor.org/rfc/rfc9700): modern OAuth security boundaries.
- [Official Python SDK](https://github.com/modelcontextprotocol/python-sdk): inspect the locally pinned version; default branch APIs may differ.

Implementation decisions below (timeouts, file layout, gateway methods and UI)
are harness-specific design choices, not claims that standards prescribe them.

Protocol requirements for this project:

- Authorization Code + S256 PKCE only. No implicit/password grants.
- Protected-resource discovery precedes issuer selection. Support header-specified
  metadata and path/root well-known fallback. Validate every discovered URL.
- Support OAuth server metadata and OIDC discovery. No guessed `/token` fallback
  if metadata fails or is malformed.
- Static public-client registration and public Dynamic Client Registration work.
  An operator-supplied valid Client ID Metadata Document URL is also supported;
  the harness does not fabricate or automatically publish a public metadata URL.
- Client registration selection: configured pre-registration first; configured
  metadata-document mode if advertised; DCR if advertised; otherwise request
  operator configuration. No fake global GitHub OAuth app/client ID.
- Send a validated resource indicator to code authorization, code exchange and
  refresh. Never treat an access token intended for server A as authority for B.
- Remote authentication does not enable remote resource/prompts/callback features.

## 5. Configuration and public contracts

### 5.1 Non-secret ConnectorConfig addition

Add `oauth: dict[str, Any] | None = None` to `ConnectorConfig`. Validate the nested
mapping through an `OAuthSettings.from_dict()` helper. Keep raw persisted fields
JSON-serializable so current dataclass `asdict()`/store constructors work.

Example:

```json
{
  "id": "remote-docs",
  "transport": "streamable_http",
  "endpoint": "https://mcp.example.com/mcp",
  "trust": "untrusted",
  "oauth": {
    "client_mode": "auto",
    "client_id": "",
    "client_metadata_url": "",
    "client_secret_ref": "",
    "token_endpoint_auth_method": "none",
    "issuer": "",
    "scope_allowlist": [],
    "callback_port": 0
  }
}
```

`example.com` is illustrative; do not use it as a working test service.

| Field | Validation and semantics |
| --- | --- |
| `client_mode` | `auto`, `preregistered`, `metadata_document`; default `auto` |
| `client_id` | Nonempty only for pre-registration; max 2,048 chars; no controls |
| `client_metadata_url` | Required only in metadata-document mode; HTTPS non-root path, no userinfo/query/fragment |
| `client_secret_ref` | Existing secret-name syntax; a reference, never a value |
| `token_endpoint_auth_method` | `none` or `client_secret_basic`; Basic only for explicitly pre-registered clients with a secret reference |
| `issuer` | Optional exact selected issuer; HTTPS URL except the explicit literal-loopback local fixture exception |
| `scope_allowlist` | At most 64 valid OAuth scope tokens; empty means require explicit review of discovered scopes, not approve all automatically |
| `callback_port` | Integer 0 or 1024–65535; 0 binds an ephemeral port; fixed ports help pre-registered clients whose server needs an exact redirect |

Reject unknown nested fields, bool-as-port, nested tokens, literal secrets,
OAuth on STDIO, OAuth plus nonempty `credential_profile`, secret refs without the
proper client mode/auth method and invalid combinations. Existing HTTP configs
without `oauth` continue working unauthenticated. A 401 should produce a useful
`auth_required` state, not secretly enable OAuth or open the browser.

`public_view()` exposes validated non-secret OAuth settings (or a concise summary)
and public auth state. Never expose encrypted blobs, token endpoint responses,
client secrets or authorization URLs here.

### 5.2 Public auth state, separate from transport state

Use a separate object; do not turn the existing connection state enum into an
OAuth state machine:

```typescript
type MCPOAuthState =
  | "not_configured" | "signed_out" | "authorizing" | "authorized"
  | "reauthorization_required" | "storage_unavailable";

interface MCPOAuthView {
  state: MCPOAuthState;
  issuer?: string;
  resource?: string;
  grantedScopes?: string[];
  expiresAt?: string;
  grantId?: string;
  generation: number;
  reasonCode?: string;
}
```

`authorized` means stored credentials are locally eligible, not proof the issuer
has not remotely revoked them. No network activity in list/status. Expiration
with no refresh means reauthorization_required. Valid refresh availability can
remain authorized with an expired access timestamp; the next bounded preflight
must refresh before any resource request.

### 5.3 Authorization operation view

```typescript
type MCPOAuthOperationState =
  | "preparing" | "awaiting_browser" | "exchanging" | "complete"
  | "cancelled" | "expired" | "failed";

interface MCPOAuthOperationView {
  operationId: string;
  connectorId: string;
  state: MCPOAuthOperationState;
  expiresAt: string;
  reasonCode?: string;
}
```

Operation IDs are random, worker-local, owner-bound handles. Knowing an ID alone
is insufficient; authenticated caller and owning session must match. No `code`,
`state`, verifier or tokens in operation views.

Authorization URLs contain sensitive transient state. Expose them only via a
separate owner-checked URL method into an ephemeral overlay. They must not be
included in logs, normal status, transcript history or agent context.

## 6. Flow lifecycle and ownership

### 6.1 User flow

```text
/mcp → choose HTTP server → Authorize
  → fetch bounded login metadata (no tokens; user-selected action)
  → choose issuer if multiple are advertised
  → review issuer, requested scopes, destinations and callback
  → Cancel [default] / Continue
  → register client if necessary
  → Open browser [explicit action] / show-copy URL / Cancel
  → browser consent → validated loopback callback
  → exchange code once → persist encrypted grant
  → show Authorized
  → obtain fresh existing mcp.preview → review → Connect
  → discover → model can call mcp_call through normal broker approvals
```

Do not auto-connect or invoke a remote tool upon login completion. Do not use a
fresh login as blanket approval for all tools. Preserve the draft and active
session while opening/cancelling the auth overlay.

### 6.2 Preview

`oauth.preview` performs bounded unauthenticated discovery after the human selects
Authorize. This is a metadata read, not a token request. Network restrictions
apply even though no OAuth credentials are attached. With multiple issuers and
no configured issuer, return a choice list first; do not select the first blindly.

After issuer selection, reserve the callback listener on literal `127.0.0.1`
(port 0 or configured port) before finalizing the preview. The preview displays
its actual redirect URI, validated destinations and scope list. The listener is
inert until start, accepts no authorization result, and closes after 120 seconds
if abandoned. Allocate a random callback path immediately; generate state and
PKCE only at confirmed start. Keep at most one pending preview per connector/
session; replace/cancel the older one and close its listener.

The preview object is held in the coordinator, has a random previewId, and hashes
canonical non-secret content: connector ID/revision/full OAuth settings,
resource, exact selected issuer, validated metadata fingerprint, endpoint URLs,
redirect URI, requested scopes and registration mode. No raw metadata descriptions
or secrets enter the approval content. A changed configuration requires a fresh
preview; no browser wait while holding the service configuration lock.

### 6.3 Start and operation ownership

`oauth.start` checks preview TTL, owner session/agent and digest, refreshes current
config and atomically consumes the preview. Route it through the broker with
operation `tool.mcp_oauth_authorize`, capability `mcp_external_review`, principal
`mcp-oauth-management`; reuse the human-confirmed management pattern in
`connect_authorized`, but keep the new action distinct. An identical digest with
a different owner/session is invalid.

Return `{operation}` immediately after creating/tracking the task. Registration,
browser wait and token exchange happen in that task. No RPC waits for five
minutes, no request-context token resets from a different task. Copy immutable
reviewed data into the task; set/reset any ContextVars within their owning task.
Approval completion reflects `completed`, `failed` or `cancelled` authorization,
not successful MCP connection. Runtime tracks and shuts down the task.

For clarity: the quick RPC creates the `preparing` operation; its owner task
revalidates metadata/configuration and consumes the exact human-confirmed broker
proposal before registration or token traffic. Rediscovery with a changed
fingerprint stops with oauth_config_changed. Pin the reviewed endpoints after
that check. Do not attempt metadata rediscovery and a browser wait synchronously
inside the 30-second RPC. Cancellation before broker consumption performs no
registration and records no successful authorization.

### 6.4 Transitions and checks

| Event | Required action |
| --- | --- |
| Preview replaced/abandoned | Close inert listener; expire preview; no token writes |
| User confirms | Revalidate exact config/metadata digest; claim approval; start task |
| DCR succeeds | Validate response and retain confidential info only in secure memory/store |
| Browser selected | Validate URL from reviewed snapshot; open with `webbrowser`, never shell |
| Wrong callback | Fixed error response; keep legitimate pending operation alive |
| Valid callback | Consume once under operation lock; check state/issuer; begin one exchange |
| Denial callback | Validate its state/issuer first; mark cancelled; no exchange |
| Exchange succeeds | Check unchanged config/auth generation; atomically persist grant |
| Exchange response lost | Mark failed/unknown exchange; never repeat code exchange |
| Connector edited/removed/disabled | Cancel previews/tasks; retire connection; invalidate grant identity |
| Forget/log out | Advance persistent auth generation before any late writer can commit |
| User changes session | Cancel and close that session's auth overlay/operation |
| Runtime shutdown | Cancel owned tasks, close listeners/clients, preserve encrypted grants |
| Worker restart | No pending previews/flows restored; durable grants remain usable |

Terminal operation views may remain for 60 seconds for polling, then be evicted.
Never retain code/verifier/browser URL after terminal completion. At most eight
active auth operations per worker, one per connector; excess fails immediately.

## 7. Network and metadata validation

### 7.1 Dedicated network client

Create `OAuthNetworkClient` using an explicit `httpx.AsyncClient` with
`follow_redirects=False`, `trust_env=False` and normal TLS certificate validation.
The MCP resource client remains `httpx2` with the same current-origin hook.
No OAuth login client carries ambient Authorization/Cookie headers.

Validate each outbound URL before DNS resolution/dispatch, including metadata,
issuer discovery, registration, token and revocation endpoints. Preserve the
existing `_classify_host` metadata/link-local bans. HTTPS is mandatory except
literal-loopback HTTP under explicit `local_access=True`, used for native local
servers/fixtures. Do not extend that exception to arbitrary private plaintext
networks, `localhost` aliases or remote issuer URLs.

A remote public MCP resource cannot advertise a loopback/private OAuth issuer
and inherit local consent automatically. Local access is connector-specific and
must be visible in the preview; reject public-to-private metadata traversal.
Never forward resource bearer credentials to metadata/registration endpoints.

Block redirects rather than following them with credentials. For supported
servers behind redirects, configure their final published endpoint; display a
sanitized redirect-not-supported reason. Browser redirects inside the selected
provider's login page are browser-owned; the backend must not fetch those pages.

Enforce response byte limits while streaming, before JSON parsing. Do not use
unbounded `.json()` or only trust Content-Length. Refuse compressed OAuth JSON
responses in the initial implementation (`Accept-Encoding: identity` and reject
non-identity content encoding) to keep decompression allocation bounded.

### 7.2 SSRF scope and honest evidence

Pre-request DNS classification is required, including mixed public/private DNS
answers, but it does not prove connect-time DNS pinning. Keep the repository's
existing limitation explicit. Do not claim complete rebinding protection from a
request hook. If implementation adds pinning, preserve Host/SNI/TLS validation and
prove it with tests; do not disable TLS or patch global DNS as a shortcut.

### 7.3 Discovery algorithm

For protected-resource metadata, parse Bearer challenges with a parser that
handles quoted values, commas and multiple challenges; don't `split(',')`.
Use SDK parsing helpers where appropriate after inspecting versioned behavior.
Validate their result before fetching. No challenge-derived URL is trusted.

For endpoint `https://host.example/team/mcp` with no explicit metadata URL:

```text
https://host.example/.well-known/oauth-protected-resource/team/mcp
https://host.example/.well-known/oauth-protected-resource
```

For selected issuer `https://auth.example/tenant` try, in order:

```text
https://auth.example/.well-known/oauth-authorization-server/tenant
https://auth.example/.well-known/openid-configuration/tenant
https://auth.example/tenant/.well-known/openid-configuration
```

For a root issuer, try the root OAuth and OIDC well-known endpoints. These URL
construction/discovery rules follow the dated MCP text and the linked
[RFC 9728](https://www.rfc-editor.org/rfc/rfc9728),
[RFC 8414](https://www.rfc-editor.org/rfc/rfc8414) and
[OIDC discovery](https://openid.net/specs/openid-connect-discovery-1_0.html).

Fallback occurs only on definite absent metadata (404/410). Malformed JSON,
issuer/resource mismatch, unsafe URLs, auth-required metadata, server errors or
429 must stop instead of silently trying a guessed login system. Return a
sanitized discovery error; no registration or token traffic afterward.

The optional resource challenge probe is a bounded unauthenticated GET of the
configured MCP URL. Inspect status/headers without consuming an indefinite SSE
body and close immediately. 401 provides challenge metadata; 405/no challenge
uses well-known discovery. Never probe with `tools/call` or send a dummy mutation.

### 7.4 Validate and freeze metadata

Require a matching protected `resource`, at least one valid authorization server,
issuer equality, supported authorization-code/response type, S256 support, and
safe authorization/token endpoints. Issuer URLs compare exactly according to the
issuer contract; do not erase paths or normalize away a trailing slash to make a
mismatch pass. Resource normalization is its own helper: lowercase scheme/host,
normalize default port, preserve significant path, omit fragment; document and
test the supported endpoint/resource matching boundary. Reject broader/different
resources unless an explicit future policy implements and reviews that mapping.
This initial policy may be stricter than servers using an origin-wide resource.

Endpoints need not share the issuer hostname; some legitimate providers separate
them. Every endpoint still needs its own validation, appears in the reviewed
snapshot, and may receive only its intended credential type. Require explicit
S256 capability; missing/plain-only metadata is unsupported, not permission to
weaken PKCE. This is a project compatibility choice.

Multiple issuers require explicit selection. A configured issuer must occur in
resource metadata; a different advertised issuer does not inherit old credentials.
Pin endpoint values for the entire flow and refresh/revoke lifecycle. Rediscovery
changing them requires fresh human authorization, never automatic migration of a
refresh token to a new endpoint.

Scopes: use a challenge's scope list when present, otherwise the protected
resource's advertised scopes, otherwise omit scope. Do not infer that challenge
scopes must be a subset of advertised scopes. A configured allowlist must contain
the requested scopes or authorization stops with a reviewable scope error. Every
first/expanded scope set gets human review. Do not add offline_access secretly;
it must be part of the approved set if requested.

## 8. Registration, PKCE and callback

### 8.1 Registration

Public dynamic-registration request:

```json
{
  "client_name": "AgentHarness",
  "redirect_uris": ["ACTUAL_RESERVED_REDIRECT_URI"],
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none"
}
```

Build these through SDK models, not this literal placeholder. Only request grant
capabilities the discovered service supports. For DCR, accept a valid nonempty
client_id with public `none` auth and a compatible redirect. Reject substituted
unsupported auth methods. If the server requires an initial registration access
token, report preregistration required; do not use a resource access token there.
No registration POST occurs before reviewed start. Do not retry registration
automatically after a network error: a client may already have been created.

Persist useful client registration alongside encrypted credentials. DCR servers
with exact redirect matching may need a fresh registration when a new ephemeral
callback port is chosen. Reuse only when the previous registration's redirect is
compatible; otherwise register anew within the new reviewed flow. Do not assume
all fixture/server implementations implement loopback port matching identically.
Pre-registered clients can configure a fixed callback_port and must fail clearly
if it is occupied. Native loopback redirects and public-client handling are
covered by [RFC 8252](https://www.rfc-editor.org/rfc/rfc8252).

Metadata-document mode needs an operator-owned public HTTPS URL containing
matching client_id/client_name/redirect_uris and compatible public auth. Do not
publish the local callback listener as the metadata document, invent a URL on
this project's domain, or fetch it with any secret. Fixtures can simulate this
mechanism locally under the explicit test-local exception. A public deployed
metadata document and real-provider compatibility remain operator work.

### 8.2 Authorization and exchange

Use SDK `PKCEParameters.generate()` and independently generate state with
`secrets.token_urlsafe(32)`. The callback compares state with `compare_digest`.
Keep state/verifier only in operation memory; never in approval proposals.

Build the authorization URL from the pinned endpoint and encoded parameters:
response_type=code, client_id, exact redirect_uri, state, S256 challenge/method,
resource and approved scope if present. Preserve safe provider endpoint query
parameters if intentionally supported; reject duplicate reserved OAuth parameter
names rather than allowing metadata to override them. No callback from the
provider may change the resource or token endpoint.

Exchange a valid code via a single bounded POST with grant_type=authorization_code,
code, identical redirect_uri, client_id, verifier and resource. For supported
explicitly pre-registered Basic clients, resolve the named secret only at this
boundary; never include it in public state. Prefer SDK encoding helpers or verify
[RFC 6749](https://www.rfc-editor.org/rfc/rfc6749) Basic credential encoding;
plain concatenation of unescaped client_id/secret is not enough.

Validate Bearer token_type, finite positive expires_in when supplied, bounded
nonempty access token and any refresh token, and granted scopes no broader than
human-approved. A missing scope means the approved requested scope under the
grant contract; preserve original scopes when refresh omits scope. Missing
expires_in is supported as unknown expiry: tokens remain until challenged, but
never invent an expiry or unlimited server validity. Reject unsupported token
formats/types; access tokens are opaque, not locally verified JWT identities.

### 8.3 Loopback callback implementation

Use a narrowly scoped asyncio server or a dedicated bounded HTTP handler; do
not add an unauthenticated general gateway route. Bind only `127.0.0.1`, never
`0.0.0.0`. Reserve port before registration. Exact redirect form:

```text
http://127.0.0.1:PORT/oauth/callback/RANDOM_PATH_NONCE
```

- GET only, exact path; bound request line, headers, query count and read time.
- Validate literal Host equals `127.0.0.1:PORT`; reject missing/duplicate Host.
- Reject duplicate `state`, `code`, `error`, `iss`; no code+error ambiguity.
- Validate owner operation pending, TTL, state and issuer before consuming.
- If issuer-response support is advertised, missing `iss` fails. A supplied
  `iss` must match pinned issuer. Use [RFC 9207](https://www.rfc-editor.org/rfc/rfc9207).
- When issuers lack `iss` support, the unique listener/path/state and immutable
  selected issuer bind the flow; never share one callback across selected issuers.
- Wrong state/path or unsolicited requests receive fixed 400/404 responses and
  do not kill a legitimate attempt. Rate-limit invalid requests and connection
  counts so an attacker cannot accumulate tasks indefinitely.
- Valid code or validated denial consumes the operation's callback once. Replays
  cannot reach token exchange. Close listener and accepted sockets afterward.
- Success HTML is a fixed string: no query/code/state/error reflection, scripts,
  external assets, refresh redirects or provider content.
- Headers: Cache-Control: no-store, Referrer-Policy: no-referrer,
  Content-Security-Policy: default-src 'none'; frame-ancestors 'none'.
- No access logs containing request targets or query parameters.
- Browser closure alone does not signal cancellation; the TUI Cancel action or
  operation TTL provides cleanup.

Codes are exchanged only at the reviewed token endpoint. Do not accept a pasted
arbitrary callback URL as a convenience fallback or support OOB code-paste login.

## 9. Secure credential persistence

### 9.1 Layout and encryption

Proposed fixed root, never supplied by RPC:

```text
~/.agent-harness/oauth/
  index.json                 # non-secret generations/grant IDs and tombstones
  index.lock                 # cross-process mutation lock
  grants/HASH.WRITE_ID.enc    # immutable encrypted grant envelopes
  locks/HASH.lock             # per-credential refresh lock
```

Credential key is SHA-256 of canonical connector ID + endpoint/resource + selected
issuer + client registration identity; config revision is retained in the binding.
Do not derive filenames from raw URLs or use arbitrary paths from clients.

Use cryptography AESGCM with a random 32-byte master key, a fresh random 12-byte
nonce for every write and associated data containing the record version and
credential key. Store version/nonce/ciphertext; validate decrypted binding fields
against expected connector/revision/resource/issuer/client. Do not invent custom
cryptography or encrypt using a token, machine name, public config or a hardcoded
password. This is encrypted-at-rest protection, not protection from a compromised
OS account that can use the keyring.

Master key sources, in priority order:

1. Operator-provided named secret `AGENT_HARNESS_OAUTH_MASTER_KEY`, resolved through
   `get_secret()` and supported existing backends; require valid base64 of exactly
   32 bytes. Missing is distinct from malformed; malformed does not fall back.
2. OS secure keyring for desktop installs. Generate/store a key under service
   `AgentHarness/MCP-OAuth`, account `master-key-v1` only through an available
   secure native backend. Reuse across processes/restarts.

Keyring unavailable/locked or an insecure plaintext fallback backend produces
`storage_unavailable`, with setup guidance. Never silently fall back to a JSON
master key, .env insertion, unencrypted tokens or logging the generated key.
Implement an allowlist/explicit backend capability check for native macOS,
Windows and Linux Secret Service/KWallet backends; do not accept arbitrary
third-party plaintext keyring plugins. Bounded keyring calls must not hang RPCs;
an underlying uninterruptible thread cannot later commit a stale key/grant.
Acquire/create master key atomically across processes to prevent racing workers
from writing grants under different keys. Do not auto-rotate an existing key.

An OS keyring call running in a thread may be uninterruptible. If it times out,
keep the master-key creation operation/lock tracked until it retires; do not
release the lock and let a second worker create a conflicting key while the first
thread may still store one. Report storage_unavailable meanwhile. Treat this
like the existing tracked connector cleanup boundary, not a successful cancel.

Both dependencies belong in the default Python installation and uv.lock. Resolve
supported versions through normal dependency tooling; do not guess a stale pin.
No installer editing of real operator keyrings is required during verification.

### 9.2 File security and atomicity

Unix directory mode 0700 and file mode 0600, atomic same-directory temp files,
fsync data then replace and directory fsync where supported. Check ownership,
regular file type, symlink/reparse boundaries and file size before reads/writes.
Use no-follow handles where available; lstat-only checks do not close symlink
races. Reject group/world-readable secret material. The encrypted file still
contains sensitive metadata and receives restrictive permissions.

Windows needs user-restricted ACL handling or a vetted secure storage method;
`chmod(0o600)` alone is not a Windows ACL guarantee. Prefer a secure native
credential backend for key protection and establish the file-directory ACL with
an explicit tested approach. Do not mark real Windows hardening complete from
mocked os.name branches. If an ACL guarantee cannot be established, fail closed
with platform guidance and record that compatibility boundary.

Corrupt cipher/index, missing master key or lost keyring secret does not cause
plaintext recovery or old-generation resurrection. Report signed-out/storage
error and permit a confirmed reset of encrypted local state. Backups need a
separate secure master-key recovery procedure; ciphertext alone is insufficient.

### 9.3 Stored record and generation rules

Private encrypted `OAuthCredentialRecord` fields:

```text
version, credential_key, connector_id, config_revision,
resource, issuer, client_id, registration metadata,
authorization_endpoint, token_endpoint, revocation_endpoint,
redirect_uri_used, token_endpoint_auth_method,
grant_id, generation, approved_scopes, granted_scopes,
access_token, refresh_token?, acquired_at_utc, expires_at_utc?,
refresh_state [idle|in_flight|reauth_required]
```

Pre-registered client secrets stay in the existing named secret backend; do not
copy them into a second store. DCR is public-only in this scope. Index contains
only non-secret binding hashes, generation, grant ID and invalidation tombstones;
no tokens, refresh tokens or client secret values. Auth status is derived from
validated local records, without a server call.

Make the index an atomic commit pointer to an immutable encrypted file identified
by a random write ID. Write/fsync the new encrypted envelope first, then under
the index lock compare generation and atomically replace the index pointer.
Readers follow only a committed pointer and verify its authenticated binding.
A crash before pointer commit leaves an orphan envelope, not a half-installed
grant. Garbage-collect unreferenced envelopes safely. This also supports token
rotation within a stable authority generation. Never overwrite the last committed
envelope before the index commit or claim two independent os.replace calls form
one atomic transaction.

Generation increments on login replacement, forgotten access, scope/identity
changes or config invalidation. New authorization gets a new random grant_id.
Refresh retaining issuer/client/resource/scopes preserves grant_id and authority
generation; token bytes may rotate without invalidating every pending approval.
A changed granted scope set is not silently treated as the same authority: advance
generation, retire/invalidate the cached catalog/approval binding and require a
new reviewed call.

Each writer supplies expected generation. Under the cross-process store lock,
compare current config revision and generation before committing. Logout/remove
writes a tombstone first, so a late browser exchange or refresh cannot recreate
forgotten credentials. Old indices/backups moving generation backwards fail
closed like the connector inventory contract. Never write tokens on a failed
local persistence step then claim authorization complete.

## 10. Refresh, identity and request execution

### 10.1 Auth identity in broker proposals

Current `_identity(config)` and `call_spec()` bind config/schema/arguments. Add a
non-secret auth identity from the OAuth service:

```json
{
  "credentialKey": "HASH",
  "grantId": "RANDOM_NONSECRET_ID",
  "generation": 4,
  "resource": "https://mcp.example.com/mcp",
  "issuer": "https://auth.example",
  "clientId": "PUBLIC_CLIENT_ID",
  "grantedScopes": ["docs:read"]
}
```

Include this identity in both connect and call proposal content only when OAuth
is configured. It never contains access token hashes or refresh secrets. Loading
it must not run a browser/network operation. Reconcile durable auth state during
`ConnectorService.refresh()` before permission classification and again before
dispatch. Do not cache auth forever in a connection's initial token string.

`call_spec()` currently runs synchronously. Preserve that contract: service
refresh asynchronously loads the public identity into a per-connector cache;
`service.auth_identity(connector_id)` synchronously reads that immutable cache.
Refresh it before building the proposal and re-read durable generation/token
eligibility at dispatch. Do not add a coroutine to `_identity()` and accidentally
serialize it, or call asyncio.run() inside the running agent loop.

Proposed adapter contract in `oauth_auth.py`:

```python
class OAuthBearerAuth(httpx2.Auth):
    def __init__(self, coordinator: OAuthCoordinator, binding: OAuthBinding,
                 expected_grant: dict[str, Any], deadline_factory): ...
    async def async_auth_flow(self, request: httpx2.Request):
        # Validate resource destination; bounded ensure_token; set header.
        # Exactly one `yield request`. Never yield a retry after a response.
        ...
```

Treat this block as an interface sketch, not a finished implementation. Reuse the
same expected grant for the connection's requests; new authority requires an
explicitly reviewed reconnect. Add OAuth token values to connection-result and
outbound redaction, including tokens rotated after connect. Stdio-only
`_secret_values` does not already cover these new values.

Revocation blocks new requests once their pre-dispatch generation check observes
the tombstone. A request already in flight may have an effect; no local logout
can recall an HTTP request already sent. Document this boundary rather than
promising instant remote cancellation or universal exactly-once effects.

Authority changing after preview/approval fails before dispatch, even for the
same endpoint. Same-grant refresh does not spuriously prompt again. Revocation,
reauthorization, issuer/resource change, config replacement and changed scopes
require fresh review. A forged model-provided grant ID is ignored; service state
is the authority source.

### 10.2 Token preflight

Before connect and each actual HTTP tool dispatch, obtain eligible credentials
within the existing operation deadline. Access tokens expiring within 60 seconds
need a refresh if one exists. A token without expiry is used until challenged.
No refresh token means auth_required when expired. No browser opens here.

A bearer adapter can also perform the same bounded lookup per outgoing SDK HTTP
request (initialize/list/GET/POST) to handle long-lived sessions, but mutation
preflight must still be explicit so pre-dispatch failures are distinguishable.
Use an async auth generator that yields the protected request ONCE and does not
process 401 with another yield. Credential-specific failures must never trigger
`manager` transient connection retries or count as network circuit failures.

The adapter verifies exact reviewed resource origin/path rules, active grant
identity and local access policy before setting `Authorization: Bearer TOKEN`.
Never set Authorization on arbitrary redirected/metadata traffic or put tokens
in URLs. Use [RFC 6750](https://www.rfc-editor.org/rfc/rfc6750) bearer semantics.

### 10.3 Refresh concurrency and uncertainty

Use an async per-key lock and a cross-process per-key advisory lock with bounded
nonblocking acquisition. Re-read durable tokens after acquiring; a different
worker may already have refreshed them. Hold the per-key refresh lock across the
one bounded token POST; never hold the global config/store mutation lock across
network/browser waits. Establish/document lock order to prevent cycles.

Persist refresh_state=in_flight before sending a rotating refresh token. On a
crash or ambiguous response after POST, treat that journal as reauthorization
required; do not re-send the possibly consumed refresh token. One successful
response replaces token material atomically and clears in_flight. Validate
configuration/generation again when committing; late refresh after logout cannot
succeed locally. If a refresh response omits refresh_token, preserve the prior
value under the standard contract; if it supplies one, replace atomically.

`invalid_grant`/revoked access clears usable token material, advances invalidation
state and produces a sanitized reauthorization_required result. No retry of the
same refresh token. A definite pre-send failure can be reported without declaring
a remote refresh happened; if transport cannot prove pre-send, be conservative.
Refresh failures do not rerun the resource request or start interactive auth.

### 10.4 Dispatch outcomes

| Situation | Result | Automatic retry? |
| --- | --- | --- |
| Token absent/expired and cannot refresh before dispatch | auth_required, known not dispatched | No |
| Refresh invalid_grant | reauthorization required, known no resource dispatch | No |
| Approval/auth identity changed while waiting | fresh review required, no dispatch | No |
| Resource request gets 401 | mark auth required; retire auth/connection safely; report conservative unknown if SDK cannot prove no effect | No |
| Resource gets 403 insufficient_scope | expose sanitized scope-challenge evidence for later human step-up; do not widen scopes or replay | No |
| Resource request times out/transport fails after dispatch | existing `outcome_unknown` | No |
| User reauthorizes later | new grant; fresh connect/call preview | No resumption of the old uncertain call |

It is acceptable to conservatively classify a post-dispatch auth failure as
unknown. It is not acceptable to claim a known failure when dispatch completion
cannot be established. Preserve typed outcomes through agent, HTTP/gateway and
TUI; do not collapse them to plain "Error" text that invites blind retries.

OAuth introspection must not grow a second tool execution path. Do not add raw
`mcp.call` RPC or bypass `execute_call()` to make authenticated demo calls work.

## 11. Gateway and TUI behavior

### 11.1 Authenticated methods

Add exact method names, centrally registered and typed:

| Method | Parameters | Result and behavior |
| --- | --- | --- |
| `mcp.oauth.preview` | `{id, sessionId, issuer?}` | `{issuers}` when selection is needed, otherwise `{previewId, digest, review, expiresAt}`; metadata only |
| `mcp.oauth.start` | `{id, sessionId, previewId, digest}` | `{operation}` immediately; consumes reviewed preview once |
| `mcp.oauth.status` | `{id, sessionId, operationId?}` | `{auth, operation?}`; local state only |
| `mcp.oauth.url` | `{id, sessionId, operationId}` | `{authorizationUrl}` only while awaiting_browser; owner-bound and ephemeral |
| `mcp.oauth.open` | `{id, sessionId, operationId}` | `{opened: boolean}`; explicit human action, backend validates stored URL |
| `mcp.oauth.cancel` | `{id, sessionId, previewId?, operationId?}` | `{cancelled: boolean}`; exactly one handle; idempotent for own terminated flow |
| `mcp.oauth.forget` | `{id, sessionId, expectedGeneration, revoke: boolean}` | `{auth, remoteRevocation: "succeeded"|"unsupported"|"failed"|"not_requested"}` |

`review` includes only connector revision/endpoint, exact issuer/resource,
requestedScopes, registration mode, endpoint list and actual redirect URI.
Do not let the caller supply token/auth URLs, verifier/state/code, filesystem path,
client-secret value, grant identity or callback URL. Strict unknown-param checks
are particularly useful here. Resolve session owner via `gw.get_session(params)`
like current mcp.preview/connect, never caller-provided agentId.

Control-plane token authentication applies to every method. A loopback callback
is separately state/path validated and is not a replacement for gateway auth.
HTTP `/rpc` is a control-plane route, not a model tool; preserve its denied-method
rules and implement local-browser capability checks for remote/headless setups.
Status/open/cancel must not allow one session to operate another's handles.
Installation-wide forget remains an explicit human admin action; check the
connector owner boundary and expected generation, not only operation ownership.

### 11.2 UI implementation

Add an HTTP authentication choice during Add: None / OAuth. Default None; selecting
OAuth saves validated non-secret settings. Existing configured HTTP servers can
choose Configure OAuth to add/edit those settings, going through the normal
configuration revision/retirement path. Never require typing tokens into advanced
JSON or changing global execution mode to authorize.

Server actions include Authorize/Reauthorize, Authorization status and Forget
access. Stdio servers never show OAuth controls. Auth status is visible alongside
transport state (e.g., authorized + disconnected), with no false "connected"
after login alone. Awaiting login is an overlay state, not a chat assistant turn.

Preview shows domain/issuer, scopes and callback in readable text. Provider names,
scopes and descriptions are untrusted: sanitize terminal control characters and
bound text before rendering. Do not style a provider's text as a harness guarantee.
Cancel is the default choice. Once confirmed, start and poll roughly every 500ms,
with one outstanding status RPC maximum and an overall operation deadline.

`FeatureHost` currently provides pick/askText/request but no cancellable progress
primitive. Add a reusable `showOAuthProgress` or generic `showProgress` overlay
contract that supports a cancel callback and status updates. Implement it in
`ui/src/app.ts`, `ui/src/components/OAuthProgress.ts` and the existing fake test hosts. Do not simulate an interactive wait
with endless transcript prints or a while loop the Escape key cannot interrupt.

While awaiting_browser show Open browser, View/copy login link, Cancel. Open calls
the human RPC; no `terminal`, shell command or unsafe OSC hyperlink is required.
Copying is optional if clipboard support is unavailable; a selectable transient
URL viewer suffices. Opening failure must preserve the flow and offer the link.
Never `host.print()` the authorization URL; host.print feeds transcript/history.
Do not persist a URL in a session message, learning record or command history.

On cancel, call cancel in finally, clear poll timers/listeners and restore the
prior draft/menu. On success, print only a concise authorization result and offer
Connect; connection still uses the existing exact preview/confirmation. On a
config change, logout or failed key storage, show the correct terminal state,
not a generic spinner until the gateway's RPC timeout.

### 11.3 Forget and revocation

UI: "Forget saved access?" with Cancel default. Optional "Also revoke at provider"
is clearly distinct. Disconnect alone preserves tokens; Forget cancels flows,
invalidates local credentials and retires the connection/catalog.

Implement [RFC 7009](https://www.rfc-editor.org/rfc/rfc7009) when the pinned issuer
advertises a supported revocation endpoint. Prefer refresh-token revocation when
available; supported endpoint auth follows registration configuration. Do not
send tokens to an endpoint discovered from the caller. Local invalidation occurs
first; retain only a short-lived private snapshot needed for one bounded revoke
attempt. A failed/unsupported remote revoke never prevents local forgetting and
never says the provider revoked access. Other workers see the durable tombstone
before their next request and cannot refresh from stale cached state.

## 12. Errors, limits and deferred scope

### 12.1 Sanitized reason codes

```text
oauth_not_configured, oauth_discovery_failed, oauth_unsafe_destination,
oauth_issuer_mismatch, oauth_resource_mismatch, oauth_pkce_unsupported,
oauth_client_configuration_required, oauth_registration_failed,
oauth_scope_not_allowed, oauth_callback_invalid, oauth_cancelled,
oauth_expired, oauth_exchange_failed, oauth_refresh_failed,
oauth_reauthorization_required, oauth_storage_unavailable,
oauth_config_changed, oauth_operation_not_found,
oauth_local_browser_unavailable, oauth_redirect_unsupported
```

Map validation to INVALID_PARAMS, missing handles/connectors to NOT_FOUND,
unauthorized ownership to UNAUTHORIZED, other known failures to OPERATION_FAILED.
The gateway currently serializes RpcError code/message; do not add ad hoc secret
exception data. Status can carry reasonCode safely. Use stable display messages
from codes, not raw provider response bodies or exception repr.

Audit events may contain connector ID, safe issuer/resource, grant ID/generation,
operation stage, duration and outcome. No response bodies, query strings, tokens,
verifiers, authorization URLs, code/state or Authorization/Cookie headers.
Register token material with redaction as defense in depth, but do not rely on
replace-after-log; encode/JSON-escaped/token-rotation variants need coverage.
If adding a secret-value registration helper, ensure forget/expiry bounds its
memory retention and existing secret-backend behavior remains intact.

### 12.2 Fixed initial limits (project choices)

| Bound | Value |
| --- | --- |
| Preview discovery total | 20 seconds, including DNS and all candidates |
| Individual metadata fetch | 3 seconds within total deadline |
| Metadata / registration response | 128 KiB decoded JSON maximum |
| Token/revoke response | 64 KiB maximum |
| Reserved preview lifetime | 120 seconds |
| Auth operation lifetime | 300 seconds from confirmed start |
| Code exchange / registration / refresh / revoke | 10 seconds each, bounded by parent deadline |
| Callback request line + headers | 8 KiB total |
| Callback query params | 16; critical parameter lengths at most 4 KiB each |
| Callback connection read | 3 seconds |
| Callback concurrent accepted sockets | 8 |
| Invalid callback attempts | 30/minute per listener; keep legitimate operation alive |
| Issuers in metadata | 8 |
| Scope count / per-scope length | 64 / 256 characters |
| Token/client-secret maximum | 16 KiB each; CR/LF/NUL rejected |
| Encrypted grant file | 256 KiB |
| OAuth index | 1 MiB |
| Refresh/store lock acquisition | 5 seconds or remaining caller deadline, whichever is shorter |
| Active worker operations | 8, one per connector |
| Terminal operation retention | 60 seconds |
| Proactive refresh skew | 60 seconds |
| UI status polling | 500ms, one request outstanding |

If a real supported provider needs a different bound, document evidence, change
one named constant and add a regression; do not remove the bound entirely.

### 12.3 Explicit deferred scope

Remote public callback hosting, distributed login operations, device-code grants,
password/implicit grants, JWT client assertions, DPoP/mTLS, OIDC user authentication,
static arbitrary HTTP header profiles, all-vendor certification and total DNS
rebinding defense are separate work. STDIO secret refs remain supported as-is.
Normal installation includes OAuth code/dependencies, but an OS secure keyring or
operator-provided master key and a compatible service are real prerequisites.
Do not claim "every MCP server works" or production v1 readiness from fixtures.

## 13. File map and interfaces

Keep modules focused. These proposed files are NEW unless marked modify. Names
below are the contracts for subsequent tasks; change them consistently if an
actual existing module imposes a better interface. Do not create placeholders
that later tasks cannot call.

| File | Responsibility |
| --- | --- |
| `ah/connectors/oauth_models.py` | Strict settings, review/snapshot/callback/token binding types and reason codes |
| `ah/connectors/oauth_network.py` | URL policy, bounded requests, metadata parsing/discovery and registration/exchange HTTP boundaries |
| `ah/connectors/oauth_callback.py` | Loopback listener, exact request validation and once-only callback delivery |
| `ah/connectors/oauth_store.py` | Encrypted records, tombstones, atomic writes and bounded cross-process locking |
| `ah/security/oauth_keys.py` | Master-key source and native keyring/Windows file-security adapter |
| `ah/connectors/oauth.py` | Coordinator, immutable previews, owner-bound operations, lifecycle and refresh |
| `ah/connectors/oauth_auth.py` | One-yield bearer-only httpx2 auth and post-response challenge handling |
| `ah/gateway/features/connector_oauth.py` | Authenticated human OAuth routes |
| `ui/src/features/mcp-oauth.ts` | UI flow, preview/start/status/cancel/forget sequencing |
| `ui/src/components/OAuthProgress.ts` | Cancellable transient overlay, link viewer, polling lifecycle |
| `tests/support/mcp_oauth_fixture.py` | Local fake issuer + protected real SDK MCP endpoint and fault controls |
| `tests/test_connectors_oauth_config.py` | Strict settings/backward compatibility |
| `tests/test_connectors_oauth_network.py` | Discovery, scopes, metadata and SSRF |
| `tests/test_connectors_oauth_callback.py` | Listener validation/cancellation/leak cases |
| `tests/test_connectors_oauth_store.py` | Encryption, file security, generation and persistence |
| `tests/test_connectors_oauth_flow.py` | Code/registration flow and lifecycle races |
| `tests/test_connectors_oauth_refresh.py` | Refresh rotation, process concurrency and crashes |
| `tests/test_connectors_oauth_execution.py` | Real SDK transport, approval binding, no replay |
| `tests/test_gateway_connector_oauth.py` | Serialized authenticated management and ownership |
| `ui/test/mcp-oauth.test.ts` | Menu/overlay/auth/error/cancel behavior |
| `docs/MCP_OAUTH.md` | Finished feature setup, limitations, recovery and evidence |

Modify existing files deliberately:

- `models.py`: nested oauth field/validation and sanitized public summary.
- `service.py`: coordinator injection, auth identity refresh and invalidation.
- `manager.py`: optional auth-aware default factory, retry classification.
- `mcp_client.py`: bearer adapter, auth preflight, post-response auth state;
  preserve transport task ownership and httpx2 origin checks.
- `execution.py`: non-secret auth identity in both exact proposals and dispatch.
- `runtime.py`: lazy service creation and full connector/OAuth shutdown.
- `security/secrets.py`/redaction plumbing: explicit bounded registration of new
  secrets, if needed; tests prove no change to backend resolution behavior.
- `gateway/features/__init__.py`: new method registration.
- `ui/src/protocol.ts`: config/auth/operation/review result types.
- `ui/src/features/mcp.ts`: OAuth choices/actions; reuse existing Connect flow.
- `ui/src/features/types.ts`, `ui/src/app.ts`, UI fake hosts: overlay contract.
- `pyproject.toml`, `uv.lock`: default OAuth dependencies and platform conditions.
- Existing connector/protocol/UI tests whose exact method or field inventory is
  intentionally extended; do not loosen them to accept arbitrary methods/fields.
- `docs/MCP.md`, README, SECURITY, OPERATIONS, handoff and connector progress:
  supported scope and operational evidence after verification.

### Required Python contracts

Use frozen dataclasses or strict models for immutable reviewed values. Secret
fields have repr=False or a custom safe repr; ordinary `asdict(secret_record)`
is not a public serialization API.

```python
# oauth_models.py: names, not implementation stubs to leave in production.
@dataclass(frozen=True)
class OAuthSettings:
    client_mode: str
    client_id: str
    client_metadata_url: str
    client_secret_ref: str
    token_endpoint_auth_method: str
    issuer: str
    scope_allowlist: tuple[str, ...]
    callback_port: int

    @classmethod
    def from_dict(cls, fields: dict[str, Any]) -> OAuthSettings: ...

@dataclass(frozen=True)
class OAuthOwner:
    session_id: str
    agent_id: str

# DiscoveredOAuth contains validated resource, issuer candidates, endpoint
# metadata, scopes and fingerprint; secrets are absent.

# oauth_network.py
class OAuthNetworkClient:
    async def discover(
        self, config: ConnectorConfig, *, issuer: str | None, deadline: float
    ) -> DiscoveredOAuth: ...
    async def register(
        self, snapshot: OAuthReviewSnapshot, *, deadline: float
    ) -> RegisteredOAuthClient: ...
    async def exchange_code(
        self, snapshot: OAuthReviewSnapshot, client: RegisteredOAuthClient,
        code: str, verifier: str, *, deadline: float
    ) -> OAuthToken: ...
    async def refresh_token(
        self, record: OAuthCredentialRecord, *, deadline: float
    ) -> OAuthToken: ...
    async def revoke_token(
        self, record: OAuthCredentialRecord, *, deadline: float
    ) -> str: ...
    async def aclose(self) -> None: ...

# oauth_callback.py
class LoopbackOAuthCallback:
    @classmethod
    async def reserve(cls, *, port: int, deadline: float) -> LoopbackOAuthCallback: ...
    @property
    def redirect_uri(self) -> str: ...
    def arm(self, *, state: str, issuer: str, require_iss: bool) -> None: ...
    async def wait(self, *, deadline: float) -> OAuthCallbackResult: ...
    async def aclose(self) -> None: ...

# oauth_store.py
class OAuthCredentialStore:
    async def load(self, binding: OAuthBinding) -> OAuthCredentialRecord | None: ...
    async def public_identity(self, binding: OAuthBinding) -> dict[str, Any] | None: ...
    async def save(
        self, record: OAuthCredentialRecord, *, expected_generation: int
    ) -> OAuthCredentialRecord: ...
    async def forget(self, binding: OAuthBinding, *, expected_generation: int) -> int: ...
    # Async context manager: bounded acquisition; safe release in finally.
    def refresh_lock(self, binding: OAuthBinding, *, deadline: float): ...

# oauth.py
class OAuthCoordinator:
    async def preview(
        self, connector_id: str, owner: OAuthOwner, *, issuer: str | None = None
    ) -> dict[str, Any]: ...
    async def start(
        self, connector_id: str, owner: OAuthOwner, *, preview_id: str, digest: str
    ) -> dict[str, Any]: ...
    async def status(
        self, connector_id: str, owner: OAuthOwner, *, operation_id: str | None = None
    ) -> dict[str, Any]: ...
    async def authorization_url(self, operation_id: str, owner: OAuthOwner) -> str: ...
    async def open_browser(self, operation_id: str, owner: OAuthOwner) -> bool: ...
    async def cancel(
        self, owner: OAuthOwner, *, preview_id: str | None = None,
        operation_id: str | None = None
    ) -> bool: ...
    async def forget(
        self, connector_id: str, owner: OAuthOwner, *, expected_generation: int,
        revoke: bool
    ) -> dict[str, Any]: ...
    async def invalidate_connector(self, connector_id: str) -> None: ...
    async def ensure_token(
        self, binding: OAuthBinding, *, expected_grant: dict[str, Any], deadline: float
    ) -> str: ...
    async def shutdown(self, *, deadline: float) -> None: ...
```

`deadline` is monotonic loop.time, not a wall-clock timestamp. Store expirations
as UTC and inject clocks for tests. `ensure_token` returns private token material
only to the bearer adapter/transport, never RPC or a model tool. It resolves
current config by ID and compares binding/generation, not merely a stale config
object passed by the connection.

Define `OAuthReviewSnapshot`, `OAuthBinding`, `RegisteredOAuthClient`,
`OAuthCredentialRecord`, `DiscoveredOAuth` and `OAuthCallbackResult` fully in task
1/owning tasks. Their fields are enumerated in sections 7–9; subsequent modules
import those shared definitions rather than manufacturing incompatible dicts.

Coordinator dependencies are injected: connector config resolver/current revision,
store, network client, callback factory, browser opener and clocks. Defaults are
wired by RuntimeServices. Tests use fakes explicitly; no global live keyring is
required. An injected fake ConnectorManager factory keeps its current one-config
call contract; auth injection belongs in the runtime's default factory closure or
optional keyword constructor, not in arbitrary test fake classes.

### Lock order and mutation contract

Document and enforce this order:

1. Resolve/read current connector config under its short service lock, then release.
2. Acquire per-grant refresh/flow lock if needed; do not hold it while acquiring
   the service configuration lock.
3. Short global credential index lock for journal/generation writes; release
   before network. Per-grant refresh lock may remain across the bounded POST.
4. Re-read connector state and use index compare-and-swap at commit. Removal
   invalidates generations before cancelling workers. Avoid a config/store lock
   inversion by keeping shared mutation helpers narrow and non-networking.

Master-key creation lock is independent and acquired before secret file locks;
never wait for an interactive keyring unlock while holding store/config locks.
If these constraints reveal a race, add a single service-level invalidation
transaction/helper with tests; do not solve it with a process-global boolean.

All asynchronous disk work that can outlive a cancelled await must use an
operation/generation guard immediately before commit. A timeout on to_thread
alone does not cancel its function. Late writers must be unable to replace a
forgotten grant, stale master key or advanced index generation.

## 14. Sequential implementation tasks

Each task: write a failing regression, run it and record the failure, implement,
run green, then checkpoint progress. Do not continue with unresolved failures in
the task's own module. Existing unrelated failures require clean-HEAD evidence,
not a waiver based on memory. No database suites run concurrently.

### Task 1 — Contracts and validated settings

**Files:** create oauth_models.py and config tests; modify models.py/store input
validation/UI protocol type. Add core dependencies and lock entries only when
used by the owning task.

- [ ] Define the frozen non-secret review/binding/owner types and private record
  types with safe repr, stable reason codes and named constants.
- [ ] Write `test_oauth_settings_rejects_raw_secrets_and_unknown_fields`:
  nested access_token/client_secret, unknown key, OAuth on STDIO and
  credential_profile+oauth each raise ConfigError before store writes.
- [ ] Write parametrized invalid port/mode/issuer/scope/client combinations,
  valid None/DCR/preregistration/metadata-document cases and legacy config roundtrip.
- [ ] Verify config revision and canonical asdict identity change when settings
  change. Serialize public_view and assert no test secret sentinel appears.
- [ ] Run red, implement strict validation, run green.

```bash
.venv/bin/python -m pytest -q tests/test_connectors_oauth_config.py tests/test_connectors_config.py tests/test_connectors_management.py -p no:cacheprovider
```

**Gate:** Old configs keep their behavior; unsupported fields cannot reach disk.

### Task 2 — Encrypted store and key source

**Files:** oauth_store.py, security/oauth_keys.py, store tests, pyproject/uv.lock.
**Consumes:** OAuthBinding/OAuthCredentialRecord; **produces:** secure storage,
public identity and generation/tombstone APIs for flow and refresh.

- [ ] Use fake keyring/key provider in every automatic test. Test native-backend
  selection, no secure backend, locked backend, malformed supplied master key,
  multi-process master-key creation and zero plaintext fallback.
- [ ] Write `test_encrypted_grant_survives_restart_and_contains_no_secret_bytes`:
  store with key K, reopen store, recover tokens; neither token/code/secret sentinel
  appears in any persisted file or public identity.
- [ ] Test AES-GCM tamper/wrong-key/wrong-AAD rejection and binding mismatch.
- [ ] Test atomic replacement fault preserves last complete record; permissions,
  oversized files, symlinks, non-regular files and hostile filename inputs refuse.
- [ ] Test logout tombstone/CAS rejects stale writes and generation rollback.
- [ ] Test cancelled disk/keyring worker cannot write after generation advances.
- [ ] Implement bounded advisory locks and explicit Windows ACL boundary. Test
  fakes are evidence for algorithm only, not a real Windows security claim.
- [ ] Resolve/install dependency declarations in the isolated environment using
  `.venv/bin/python -m pip`; refresh uv.lock and run lock freshness check.
- [ ] Run store module green and existing secret/config tests once.

**Gate:** No usable storage key → no token exchange/persistence, clear actionable
error. No plaintext test token occurs on disk or in captured output.

### Task 3 — Bounded network/discovery

**Files:** oauth_network.py/network tests. **Consumes:** settings and URL policy;
**produces:** validated immutable DiscoveredOAuth and candidate choices.

- [ ] Stub DNS/HTTP transports. Write challenge parsing tests with escaped quoted
  commas, multiple challenges, missing scope and malformed values.
- [ ] Test header-first/path/root resource discovery and all issuer-path OAuth/
  OIDC fallback candidates; count requests and check order exactly.
- [ ] Test strict issuer/resource mismatch, changed token endpoint, unsafe schemes,
  userinfo, mixed DNS results, metadata/link-local, public-to-private pivots and
  explicit literal-loopback fixture exception.
- [ ] Test 404 fallback versus 429/5xx/malformed JSON fail-stop; no registration or
  token request follows any fatal discovery result.
- [ ] Test streaming size cap with absent/lying Content-Length and reject compressed
  metadata before excessive allocation. Response close happens on each branch.
- [ ] Test scopes from challenge can differ from scopes_supported; allowlist and
  human preview remain authority. Missing scopes omit the parameter.
- [ ] Implement registration/exchange request builders with safe fixed destinations;
  protocol response validation remains exercised in task 5.

**Gate:** All metadata remains untrusted until validated; no broadening the existing
MCP origin hook; unsafe requests are rejected before a server receives them.

### Task 4 — Callback listener

**Files:** oauth_callback.py/callback tests. **Consumes:** reserved port, armed
state/issuer; **produces:** exactly one validated OAuthCallbackResult.

- [ ] `test_bad_callback_does_not_consume_good_flow`: wrong state/path/Host first,
  then correct callback; exactly one legitimate result and no earlier exchange.
- [ ] Parametrize missing/duplicate state/code/error/iss, code+error, malformed
  requests, too-long query/header, unsupported method, unexpected Host and port.
- [ ] Test advertised issuer support without iss fails; unexpected/mismatched
  issuer fails; verified denial returns a cancellation result without exchange.
- [ ] Test inert preview refuses results before arm, valid callback replay refuses,
  concurrent duplicate callbacks resolve only once.
- [ ] Test reserve(0) uses actual bound port; fixed occupied port fails cleanly.
- [ ] Test expiry, cancellation, shutdown and repeated lifecycle leave no listening
  sockets or pending callback tasks; port can be rebound after close.
- [ ] Assert response HTML/headers and captured logs contain none of the code,
  state, query, arbitrary provider text or secret sentinels.

**Gate:** Browser callbacks cannot issue RPC commands, log secrets or consume an
operation without state/issuer/path validation.

### Task 5 — Coordinator and actual code grant

**Files:** oauth.py/flow tests and local fixture. **Consumes:** tasks 1–4;
**produces:** reviewed previews, fast operation start, code exchange and durable
public auth state.

- [ ] Build the local fixture as an ASGI app on a bound ephemeral loopback socket,
  with metadata/authorize/token/register/revoke and real SDK MCP mounted behind
  access-token enforcement. Use one async server owner and shut it down in finally.
  Do not race find-free-port then launch; bind first and pass the owned socket.
- [ ] Fixture asserts S256 verifier/challenge, redirect/client/resource, one-use
  code, granted scope and token destination. Counters track each exchange.
- [ ] Test DCR+PKCE flow, preregistered public client, metadata-document mode,
  explicit preregistered Basic client and unsupported registration response.
- [ ] Test browser opener is an injected fake; only a deliberate open action
  invokes it, and no shell or real browser runs during tests.
- [ ] Test frozen preview mismatch/expiration/reuse/owner mismatch; registration
  only after reviewed start. No secret in digest/proposal.
- [ ] Test broker record is created/consumed/completed for exact OAuth action.
  Do not set all test calls to full/trusted to avoid verifying approval.
- [ ] Test callback resource/client/issuer/verifier mismatch, denial, code replay,
  broader granted scope, invalid token type and missing expiration semantics.
- [ ] Test lost code-exchange response increments token endpoint count once;
  retry does not re-use that code. A fresh login is a separate operation.
- [ ] Complete token persistence before reporting complete; failure to encrypt/
  write marks failed and clears private code/verifier/state.
- [ ] Start returns promptly (<1 second with fake scheduling); work finishes via
  poll; no 30-second RPC timeout for browser wait.

**Gate:** A real HTTP exchange yields a durable encrypted grant through human
review. This still does not mean transport connection/tool invocation is complete.

### Task 6 — Refresh and identity

**Files:** refresh tests; OAuthCoordinator.ensure_token; store journal helpers.
**Consumes:** encrypted record/generation; **produces:** bounded private tokens and
stable public authority under same-grant refresh.

- [ ] Expiring access token refreshes proactively; valid token requires no POST;
  missing expiry waits for challenge; no refresh token refuses before dispatch.
- [ ] Rotation response replaces refresh token atomically; omitted refresh token/
  scope preserves previous values; wider scopes fail or trigger re-review.
- [ ] Run N concurrent ensure_token calls; token endpoint counter is exactly one.
  Use two independent processes sharing tmp credential storage to prove file lock,
  not merely asyncio lock; fake master key provider avoids real keyring.
- [ ] Crash a disposable worker after writing in_flight/sending refresh. Restarted
  worker reports reauthorization rather than sending the old rotating token again.
- [ ] Time out the token response; no automatic retry; journal becomes reauth.
- [ ] invalid_grant erases eligibility; resource call count stays zero.
- [ ] Grant refresh preserves grant_id/generation; reauth/logout/scope change
  changes identity and prevents an old pending approval from dispatching.
- [ ] All waits respect remaining caller deadline; lock timeout is a known
  pre-dispatch failure and leaves locks usable for another operation.

**Gate:** At most one refresh exchange under concurrency, conservative recovery
under ambiguity, and no secret/public identity confusion.

### Task 7 — Mutation and runtime cancellation races

**Files:** service.py/runtime.py/coordinator lifecycle tests.
**Consumes:** coordinator/store; **produces:** invalidation and shutdown integration.

- [ ] `test_late_login_after_forget_cannot_restore_credentials`: pause token
  response, forget, release response, assert signed_out and no old grant persisted.
- [ ] Repeat for remove/replace/disable and concurrent worker config changes.
- [ ] Test a refresh already running at logout cannot persist after tombstone.
- [ ] Configuration change retires active HTTP connection/catalog and invalidates
  associated authority; normal reconnect must receive a fresh preview.
- [ ] Disconnect preserves authorized credentials; Forget removes eligibility;
  Remove cancels operations and retires connection.
- [ ] Session switch cancellation and runtime shutdown close previews/operations/
  callback sockets/HTTP clients, leaving no task exceptions or secret logs.
- [ ] Cancellation/reset of ContextVars happens in their owner task; repeated
  calls from distinct gateway tasks stay quiet. Preserve stdio ownership tests.

**Gate:** No forgotten/deleted authority can reappear from late async work. No
browser wait blocks the service config lock or general session interaction.

### Task 8 — Authenticated gateway contracts

**Files:** connector_oauth.py, method registry, gateway tests, protocol types.
**Consumes:** coordinator methods; **produces:** the methods in section 11.

- [ ] Serialized WireHarness tests require gateway token on every new method.
- [ ] Wrong session/owner/connector/operation ID/expired preview denies access.
- [ ] URL/open/cancel never accept a caller-supplied destination or token.
- [ ] Status returns safe local state without launching/probing servers.
- [ ] Fast start + poll + explicit open + callback + completed status tested
  across separate RPC tasks, not direct coordinator calls only.
- [ ] Forget expected-generation mismatch cannot delete a newly authorized grant;
  local invalidation succeeds even if remote revocation fails/unsupported.
- [ ] Register exact methods in test_features_split.py; raw mcp.call remains absent.
- [ ] HTTP /rpc checks preserve blocked prompt/shutdown methods and do not add
  an unauthenticated callback endpoint. Headless/remote browser constraints are
  correctly reported rather than opening a worker browser without human intent.

**Gate:** Human control-plane authority is distinct from agent tools and callback
possession. Inspect RPC outputs and audit logs for all secret sentinels.

### Task 9 — Existing MCP transport and native execution

**Files:** oauth_auth.py, mcp_client.py, execution.py, manager/service factories,
execution integration tests. **Consumes:** ensure_token/public auth identity;
**produces:** approved authenticated native MCP calls with no replay.

- [ ] Red regression: direct SDK OAuth flow can replay; test the new adapter
  against protected fixture and assert one resource request for each invocation.
- [ ] Connect through existing reviewed mcp.preview/mcp.connect with stored tokens;
  initialize/list/describe/echo round-trip through the real SDK HTTP transport.
- [ ] Actual BaseReActAgent tool pipeline pauses for approval in full mode, resumes
  with approved grant identity, sends bearer only to the resource and returns echo.
- [ ] Pending approval followed by reauthorization/forget/resource/issuer change
  fails before request; same-grant refresh retains the approval binding.
- [ ] Fixture mutate_then_401 increments a side-effect counter then returns 401.
  Counter equals one; no login/open/refresh/retry path invokes it a second time.
- [ ] Repeat with 403 insufficient_scope, timeout and drop-after-effect; typed
  uncertainty survives agent → gateway/HTTP → UI event handling.
- [ ] GET stream reopening may be part of SDK transport lifecycle; prove it never
  resends a tools/call mutation. Measure counters on tool requests, not every
  harmless SSE network request.
- [ ] Expired/no-refresh token refuses before dispatch with fixture counter zero.
- [ ] Authorization headers never go to discovery, redirects, another resource,
  registration or callback. Endpoint hook still rejects destination changes.
- [ ] No auth failure contributes to transient retry/circuit storm. Retiring a
  connection from a response hook must signal owner cleanup rather than await
  shutdown recursively from an SDK reader task; test no deadlock.
- [ ] Repeated OAuth HTTP connect/disconnect cycles from different tasks release
  clients/tasks; old stdio fixtures still leave no subprocesses.

**Gate:** This is the first milestone proving usable MCP OAuth in the agent loop.
Transport discovery alone, mocks and status reporting cannot substitute for it.

### Task 10 — TUI interaction and end-to-end UI check

**Files:** features/mcp-oauth.ts, components/OAuthProgress.ts, feature host/app/types,
MCP menu/protocol and UI tests. **Consumes:** gateway contracts from task 8.

- [ ] Add OAuth selection/fields for HTTP; no controls for STDIO; existing None
  choice stays functional. Configure change saves through mcp.configure.
- [ ] Preview Cancel triggers cancel/cleanup and sends no start/open/exchange.
- [ ] Confirm → start → progress → explicit Open → poll → authorized flow tested
  using fake FeatureHost and real overlay input tests.
- [ ] Escape, session switch, deadline, gateway disconnection and application exit
  each cancel/clear timers; no polling runs after dialog dispose.
- [ ] Browser opening failure leaves View link/Cancel; URL appears only in the
  transient viewer and never transcript/history/notice/error serialization.
- [ ] Sanitize ANSI/control characters, constrain long domains/scope lists, keep
  issuer/scope review and Cancel visible at narrow/short terminal sizes.
- [ ] Authorized does not claim connected; Connect runs the original exact preview
  path; Forget confirmation and remote-revocation result are accurate.
- [ ] Auth-required/expired/storage-unavailable messages provide the next action
  and do not instruct the user to paste a token into the agent prompt.
- [ ] Run npm check; then add at least one real Node/Python gateway wrapper test
  using the isolated fixture/token store to verify the new method/type boundary.
  No real browser/keyring required in automated tests.

**Gate:** `/mcp` completes configuration → browser authorization → connection →
agent-call capability in the supported local environment, with usable cancellation.

### Task 11 — Full verification, documentation and package checks

**Files:** finished docs/MCP_OAUTH.md, README/MCP/security/operations/progress/handoff,
artifact checker if new packaged files/dependencies need explicit coverage.

- [ ] Run the acceptance matrix, focused tests, full backend suite and npm check
  sequentially as appropriate. Fix genuine failures with regressions.
- [ ] Run Ruff/Bandit/format, lock freshness, diff whitespace and distributions.
- [ ] Verify wheel installs include all OAuth modules and dependencies in a clean
  disposable environment; no developer-home token/keyring/DB is used.
- [ ] Record actual commands, pass/skip counts, module versions and platform,
  credential-store mode, supported registration modes, replay/callback evidence.
- [ ] Remove deferred-OAuth statements only after corresponding flow/transport/UI
  evidence exists. Retain unrelated MCP deferred features and network limitations.
- [ ] Document key storage/backup/lost-key reset, headless prerequisites,
  disconnect versus forgetting and provider revocation limitations.
- [ ] Deliver a changed-file list, unresolved failures with clean-HEAD evidence,
  compatibility gaps and next operational gates. Do not publish or bump v1.

## 15. Security and acceptance matrix

No row is satisfied solely by code inspection. Link its test name and command in
`docs/archive/plans/MCP_OAUTH_PROGRESS.md` when implementing.

| Requirement | Minimum proving test |
| --- | --- |
| Default install includes OAuth | Isolated wheel import/dependency resolution |
| Old stdio/unauthenticated HTTP unchanged | Existing connector client/manager/service suites |
| Header/path/root metadata | All candidate order branches against controlled HTTP |
| OAuth and OIDC metadata | Both root and path issuer cases |
| Malicious discovery fails closed | Unsafe URL, mixed DNS, wrong issuer/resource, oversized/compressed data |
| Multiple issuers not silently selected | UI/server choice then exact preview |
| Public DCR | Fixture registers only after consent; validates redirect/auth method |
| Preregistered public and Basic clients | Fixture checks correct request/auth with no public secret |
| Configured metadata document | Advertised support accepted; missing support/config refuses |
| S256 and exact exchange | Fixture independently recomputes challenge from verifier |
| Callback state/issuer/path | Invalid-first valid-later + duplicate valid concurrent callback |
| Browser wait bounded/cancellable | Expiry/cancel/shutdown/session switch, sockets/tasks closed |
| Secrets not exposed | Sentinels across disk, JSON-RPC, prompts, transcript, logs, error chains |
| At-rest encryption | Restart recovery, tamper/wrong-key/AAD/permissions tests |
| Secure master key | Native backend validation; absence/malformed source refuses |
| Cross-worker refresh | Independent-process counter exactly one |
| Refresh crash safety | In-flight restart forces reauth, no second rotation attempt |
| Logout wins late callbacks | Tombstone/CAS tests against suspended exchange/refresh |
| Approval binds auth identity | New grant invalidates old; same-grant refresh retains authority |
| No replay | mutate_then_401/403/drop/timeout side-effect counter exactly one |
| Pre-dispatch auth failures known | Remote tool counter zero |
| Broker remains mandatory | Full-mode actual agent tool pause/resume |
| Teardown ownership preserved | Repeated same/cross-task cleanup with no fixture leaks |
| UI usable | Keyboard/cancel/review/link privacy + real gateway wrapper |
| Provider revoke honest | Success/unsupported/failure with local invalidation always effective |
| Operational docs honest | Supported/deferred matrix agrees with tests |

### Required fixture fault switches

Implement explicit in-test knobs/counters, not live provider behavior:

- 401 with header metadata, or 401 without header to exercise well-known.
- Resource/issuer mismatch and hostile public-to-private URLs.
- Missing/plain-only PKCE, absent DCR, registration with incompatible auth method.
- Wrong callback state/iss, denial, repeated authorization code.
- Malformed/huge/compressed token responses; no expiry; scope widening.
- Rotating refresh tokens, invalid_grant, lost response after refresh consumed.
- Token exchange suspended while config/remove/forget changes.
- Remote tool effect before 401/403/timeout/disconnect.
- Revocation supported/unsupported/failed.

Every fixture binds literal loopback, has a per-test UUID name, receives no real
keys and stops in finally. Tests must independently assert server counters;
client-side mocks cannot prove a remote mutation was not retried.

## 16. Verification commands and release evidence

### 16.1 Safe test environment

Do not assume conftest alone prevents import-time config reads. Run tests with a
disposable home and blank AH_ENV_FILE, clear live keys/backend selectors before
importing project modules, and explicitly supply only the dedicated test DSN.
The earlier /tmp bootstrap is not a permanent project dependency; create your
own disposable verification runner if it is absent.

An isolated runner pattern (run using `.venv/bin/python -I`) is:

```python
from pathlib import Path
import os, subprocess, sys, tempfile

repo = Path.cwd()
with tempfile.TemporaryDirectory(prefix="ah-oauth-verification-") as task_root:
    task_path = Path(task_root)
    env_file = task_path / "empty.env"
    env_file.write_text("")
    env = dict(os.environ)
    for key in list(env):
        if key.endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_FILE")):
            env.pop(key, None)
        elif key.startswith(("AGENT_HARNESS_VAULT_", "AGENT_HARNESS_AWS_")):
            env.pop(key, None)
    env.pop("DATABASE_URL", None)
    env.pop("AGENT_HARNESS_DATABASE_URL", None)
    env.pop("AGENT_HARNESS_OAUTH_MASTER_KEY", None)
    env.pop("AGENT_HARNESS_PROVENANCE_KEY", None)
    env.pop("PYTHONPATH", None)
    env["HOME"] = task_root
    env["USERPROFILE"] = task_root
    env["AH_ENV_FILE"] = str(env_file)
    env["AGENT_HARNESS_SECRET_BACKEND"] = ""
    env["AGENT_HARNESS_TEST_DATABASE_URL"] = (
        "postgresql://postgres:postgres@localhost:5432/agentharness_test"
    )
    # All OAuth tests inject fake key providers; never create/use a live keyring.
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short",
         "-p", "no:cacheprovider", "--cov=ah", "--cov-report=term",
         "--cov-fail-under=60"],
        cwd=repo, env=env, check=True,
    )
```

Also unset project-specific config/UI/home override variables that may exist in
Muse's environment after examining their names in source, without printing their
values. Use the same isolation for non-editable import/build probes. Clear
KEYRING_BACKEND overrides or inject fake providers before any keyring access;
a temporary HOME alone does not guarantee OS keyring isolation.

### 16.2 Focused checks

Use the same safe runner to pass a targeted pytest argument list:

```bash
.venv/bin/python -m pytest -q tests/test_connectors_oauth_config.py tests/test_connectors_oauth_store.py tests/test_connectors_oauth_network.py tests/test_connectors_oauth_callback.py -p no:cacheprovider
.venv/bin/python -m pytest -q tests/test_connectors_oauth_flow.py tests/test_connectors_oauth_refresh.py tests/test_gateway_connector_oauth.py tests/test_connectors_oauth_execution.py -p no:cacheprovider
.venv/bin/python -m pytest -q tests/test_connectors_client.py tests/test_connectors_manager.py tests/test_connectors_service.py tests/test_connectors_management.py tests/test_connectors_execution.py -p no:cacheprovider
npm --prefix ui run check
.venv/bin/python -m ruff check ah/ tools/
.venv/bin/python -m ruff format --check ah/ tools/
.venv/bin/python -m bandit -r ah/ tools/ -ll -x ah/cli/
uv lock --check --offline
git diff --check
```

Run Ruff on new/touched test files too, using the same 100-column style. Do not
add Bandit suppressions to hide insecure TLS, shell browser opening or token-file
writes. Medium/high must remain zero; report low findings honestly.

Build into a fresh disposable output directory, then check both artifacts:

```bash
.venv/bin/python -m hatchling build -d /tmp/ah-oauth-dist
.venv/bin/python tools/check_release_artifacts.py /tmp/ah-oauth-dist/*
```

If output paths already contain old artifacts, select only the newly built
wheel/sdist; do not report stale artifacts as verification. No production
installer execution is necessary to check that dependencies are declared.

### 16.3 Regressions and clean HEAD

A failing current test is not pre-existing because it "looks unrelated". Reproduce
on a temporary clean HEAD worktree with isolated config/home and the same dedicated
test DB, sequentially; never reset/stash away the user's work to compare. Record
HEAD, test name and result for both. Fix current-change regressions and rerun
focused/full checks when the fix affects runtime behavior.

The 2026-10-12 pre-OAuth recorded baseline was 2237 passed, 5 skipped, 82.26%
coverage and 295 real-wrapper UI tests. These counts are historical, not a target
to hardcode. Muse's new checkout or test additions can change counts. Count real
fixture tests and environment skips separately; no live-provider accuracy claim.

### 16.4 Required final report

Report:

1. Files changed and each module's responsibility.
2. Commands/results, red-to-green regressions and final full-suite outcome.
3. Registration modes and platform credential storage actually verified.
4. Loopback callback/HTTP client/task cleanup verdict.
5. Proof side-effect counters stay at one after 401/403/timeout/drop.
6. Proof stale callback/refresh cannot revive logout/remove credentials.
7. Remaining skips/deferred features/real-provider gaps and hosted release gates.
8. Confirmation no live config/production DB/browser/account/publishing actions.

## 17. Continuation prompt and checkpoints

### Paste this into Muse 1.3

```text
Implement MCP OAuth in the AgentHarness checkout using
 docs/archive/plans/MCP_OAUTH_IMPLEMENTATION_HANDOFF.md as your specification and plan.
Read sections 1–12 before editing and execute task checkboxes sequentially.
Start with recon and preserve unrelated modifications. No commit/push/deploy,
live credential reads, production database operations or shell startup changes.
Use .venv/bin/python -m commands and isolated HOME/config/fake key providers.
The only allowed DB is agentharness_test via AGENT_HARNESS_TEST_DATABASE_URL.

Deliver the complete supported local flow: /mcp config → reviewed authorization
→ external browser/loopback callback → encrypted persistent tokens → reviewed
connection → actual broker-approved MCP tool invocation → refresh → forget/revoke.
Use Authorization Code + S256 PKCE. Keep OAuth separate from tool execution.
Do not attach the SDK's replaying OAuthClientProvider directly to tools/call.
No auth step may replay an uncertain side effect, widen permissions, expose a
secret or resurrect access after logout. Preserve owner-task SDK teardown.

For each task write a regression, prove red, implement, prove green, record
progress in docs/archive/plans/MCP_OAUTH_PROGRESS.md. Inspect actual SDK signatures.
Use real local HTTP issuer + SDK MCP fixtures; no paid providers or real browser
in automated tests. Verify cross-process refresh/tombstone races and server-side
mutation counters. Complete the full backend/UI/security/package checks and
accurate docs. Do not call mocks, discovery or login UI alone finished OAuth.
If an external OS/provider prerequisite blocks a compatibility claim, keep
independent work moving and report the exact boundary with evidence.
```

### Progress document format

Create `docs/archive/plans/MCP_OAUTH_PROGRESS.md` at task 1:

```markdown
# MCP OAuth progress
Base HEAD / branch / date:
Implementation plan: MCP_OAUTH_IMPLEMENTATION_HANDOFF.md

## Decisions and supported scope
- Current SDK/dependency/platform facts:
- Credential-store mode:
- Any justified deviation, with test evidence:

## Tasks
- [ ] Task 1 contracts/config
- [ ] Task 2 secure store
- [ ] Task 3 network/discovery
- [ ] Task 4 callback
- [ ] Task 5 actual grant
- [ ] Task 6 refresh
- [ ] Task 7 cancellation/invalidation
- [ ] Task 8 gateway
- [ ] Task 9 native transport/execution
- [ ] Task 10 UI
- [ ] Task 11 final verification/docs

## Evidence
For each completed task: failing test, failure observed, implementation,
passing command/result, limitations. Keep secret values out of evidence.

## Current checkpoint
Files changed:
Last passing command:
Unresolved failure and reproduction:
Next exact action:

## Acceptance matrix
Requirement → named regression → command/result.

## Deferred and operational gates
Only list explicit scope boundaries; do not mark unbuilt planned work complete.
```

When context gets short, finish the current atomic test/fix cycle or record the
exact failing reproduction. Save the next command, interface decisions and any
in-flight fixture PID/port cleanup. Do not restart the feature from scratch,
rerun every completed test on every task or delete existing working modules.

### Final self-review before calling OAuth complete

- [ ] Every new interface has a real implementation and tests; no TODO stubs.
- [ ] No signature/property naming drift between coordinator, gateway and UI.
- [ ] All secrets stay behind the storage/transport boundary.
- [ ] Auth lifecycle does not hold config locks during browser/network waits.
- [ ] Resource requests are one-yield and side-effect counters prove no replay.
- [ ] Cross-worker logout/refresh races are exercised, not assumed.
- [ ] Default installation and supported local TUI path are verified end to end.
- [ ] Deferred remote callback/platform/vendor scope is explicit.
- [ ] Documentation matches observed behavior and full verification evidence.
