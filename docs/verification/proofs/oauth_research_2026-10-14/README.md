# OAuth / subscription auth for cloud LLM providers — research (2026-10-14)

Scope: which non-ChatGPT cloud LLM subscriptions can Machina authenticate to
with a first-class provider, given the constraints: local-only (vendor API is
the only network peer), no secrets scraped from non-consented sources, no
speculative transports. Method: on-disk references (installed CLIs, the
Hermes agent checkout at `/Users/shadow/.hermes/hermes-agent`), vendor / OSS
source, live HTTP probes on this machine. All token material below is
public client metadata or observed protocol facts — never user tokens.

## Verdict table

| Provider | Flow | Client-id basis | Refresh protocol | Wire API | Risk / ToS caveat | Verdict for Machina |
| --- | --- | --- | --- | --- | --- | --- |
| **GitHub Copilot** | device code (`github.com/login/device`) **or** read-only `gh auth token` import | `Iv1.b507a08c87ecfe98` — VS Code's public GitHub App id, published in OSS (`hermes_cli/copilot_auth.py`); modern Copilot API also accepts the user's own `gho_/ghu_/github_pat_` token directly | none needed (GitHub token is long-lived; the legacy `copilot_internal/v2/token` JWT exchange now answers **403 WAF** for non-first-party clients — probed live 2026-10-14) | `https://api.githubcopilot.com/chat/completions` (OpenAI-compatible), `Authorization: Bearer <github_token>` + `Copilot-Integration-Id`, `Editor-Version`, `x-initiator` headers — **verified live: HTTP 200, 51 models listed, real completions** | Uses the user's own Copilot seat; headers are the same self-identification VS Code/Copilot CLI send | **IMPLEMENTABLE — implemented + live proof** |
| **Claude Pro/Max (Claude Code)** | PKCE loopback `http://localhost:{port}/callback` **or** manual code paste (`https://platform.claude.com/oauth/code/callback`), `code=true` | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` — the Claude Code production client id, confirmed in the locally installed `claude` 2.1.150 binary (strings) and the Hermes reference | `POST https://platform.claude.com/v1/oauth/token`, JSON `{grant_type: refresh_token, refresh_token, client_id, scope}`; `anthropic-beta: oauth-2025-04-20`; single-use rotation. Token endpoint 429s a `claude-code/` UA (Hermes observation) → use a neutral UA | `POST https://api.anthropic.com/v1/messages` with `Authorization: Bearer` + `anthropic-beta: claude-code-20250219,oauth-2025-04-20` + `user-agent: claude-code/<v> (external, cli)` + `x-app: cli`; no `x-api-key` (dual-auth is rejected) | Anthropic's subscription is provisioned for Claude Code; using the same public client from another harness is what every OSS clone does, but it is not an officially supported third-party client | **IMPLEMENTABLE — implemented; hermetic + import proof (no Claude login on this machine)** |
| **Google Gemini (gemini-cli / Cloud Code Assist)** | PKCE loopback `http://127.0.0.1:{port}/oauth2callback` (random port) or user-code redirect `https://codeassist.google.com/authcode` | `REDACTED` + (explicitly non-secret) installed-app secret — both in the Apache-2.0 `google-gemini/gemini-cli` repo, still present on `main` | `POST https://oauth2.googleapis.com/token` form-encoded, `refresh_token` grant with client id+secret (Google desktop clients; no rotation) | `POST https://cloudcode-pa.googleapis.com/v1internal:{loadCodeAssist,onboardUser,streamGenerateContent?alt=sse}`, Bearer access token; request wrapper `{model, project, user_prompt_id, request:{contents, systemInstruction, tools, generationConfig}}` | **Google killed the consumer path**: since **2026-06-18** "Login with Google" for free/AI Pro/Ultra tiers stopped being served (`developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals`). Only Code Assist Standard/Enterprise/Workspace licenses still work, and those subscriptions stop being purchasable (2026-10-09) / renewable (2027-01-31) | **POSSIBLE-WITH-CAVEATS (enterprise-only) — implemented; hermetic proof (no Gemini login on this machine)** |
| Antigravity (agy) | Google sign-in; credential in OS keyring (macOS service `gemini`, account `antigravity`) | Closed-source binary; the OAuth client id is only obtainable by reverse-engineering the client (community plugin `NoeFabris/opencode-antigravity-auth`, 10.9k stars, archived 2026-08-27) | Opaque keyring blob | `daily-cloudcode-pa`-family private API | The plugin's own readme: using it **violates Google's ToS; real bans/shadow-bans reported**. Credentials are product-locked to Antigravity apps | **NOT-FEASIBLE** (scraped client id + explicit ToS/ban risk; violates the no-scrape rule) |
| Qwen (qwen-cli) | device flow (qwen-code OSS); Hermes imports `~/.qwen/oauth_creds.json` | `f0304373b74a44d2b584a3fb70ca9e56` (per Hermes `auth_constants.py`; lives in Qwen's OSS CLI) | `POST https://chat.qwen.ai/api/v1/oauth2/token` refresh_token grant | `https://portal.qwen.ai/v1` (OpenAI-compatible) | Free-tier vendor; ToS silent on third-party clients. Nobody on this machine is logged in; not verified this wave | POSSIBLE — not implemented (next steps below) |
| xAI Grok | device code (`https://auth.x.ai/oauth2/device/code`) | `b1a00492-073a-47ea-816f-4c329264a828` (Hermes) | refresh_token grant, ~1h proactive skew | `https://api.x.ai/v1` | — | POSSIBLE — next wave |
| MiniMax | `urn:ietf:params:oauth:grant-type:user_code` device-style grant | `78257093-7e40-4613-99e0-527b14b39113` (Hermes) | refresh_token grant | `https://api.minimax.io/anthropic` | — | POSSIBLE — next wave |
| OpenRouter | PKCE loopback, documented at `openrouter.ai/docs/guides/overview/auth/oauth`; mints a plain API key | public docs (`openrouter.ai/auth`) | **no refresh token** (it mints a user-controlled key) | `https://openrouter.ai/api/v1` | none significant | POSSIBLE (low value — Machina already supports OpenRouter API keys) |
| Z.ai / GLM | API key only (Hermes probes base URLs, no OAuth exists) | — | — | OpenAI-compatible | — | N/A — no OAuth to implement |
| **Moonshot / Kimi Code (K3)** | **device-code OAuth** (`auth.kimi.com/api/oauth/device_authorization`), official OSS CLI (MIT) | client id `17e5f671-d194-4dfb-9706-5516cb48c098` — public in the published MIT `@moonshot-ai/kimi-code` bundle (OSS build, not scraped) | `POST {oauthHost}/api/oauth/token` form `{client_id, grant_type: refresh_token, refresh_token}`; 429/5xx retried with backoff; 401/403/`invalid_grant` → re-login | `https://api.kimi.com/coding/v1` (global: `api.kimi.ai/coding/v1`), Bearer token; Kimi's `/coding` route is Anthropic-compatible (claude-code UA required per the reference) and the CLI's managed provider is OpenAI-protocol | Subscription ToS is written around the official apps + API keys ("subscribers can obtain an API Key to integrate into third-party tools"); OAuth-token reuse by other harnesses is done by community tooling but not officially blessed | **POSSIBLE-WITH-CAVEATS** (round 2 — see below; not implemented this wave) |
| **Kiro (AWS Kiro IDE/CLI)** | AWS SSO OIDC **device flow** (`--use-device-flow`; Builder ID / IdC / Google / GitHub / Amazon social), dynamic client registration — no fixed app id | none needed: the CLI registers its own SSO OIDC client (`~/.aws/sso/cache/<hash>.json` clientId+clientSecret) per login | `POST https://oidc.{region}.amazonaws.com/token` refresh_token grant with the registered client pair (AWS SSO OIDC standard) | CodeWhisperer/Q service (`q.{region}.amazonaws.com`, `codewhisperer.{region}.amazonaws.com`, AWS JSON + event-stream) and the newer Kiro runtime (`runtime.{region}.kiro.dev`, v3 engine); models are Kiro aliases (`claude-sonnet-5` default on this machine, opus/deepseek/minimax/glm per tier) | **Kiro's ToS prohibits third-party proxy/harness use; AWS enforces it** (anti-fraud on non-IDE fingerprints/IPs; community proxy users report banned accounts and an identity-verification support process — OmniRoute maintainer, Aug 2026). Credentials importable (`~/.aws/sso/cache/kiro-auth-token{,-cli}.json` on this machine, Builder ID, live) but that is exactly the prohibited path. The clean path is driving the official `kiro-cli acp` binary | **NOT-FEASIBLE as a native provider; POSSIBLE-WITH-CAVEATS as an ACP interop bridge** (round 2 — see below) |
| Cursor | browser login, opaque local state; private gRPC API | requires reverse engineering | n/a | private | ToS + reverse-engineering of a closed client | NOT-FEASIBLE |
| Windsurf (Codeium) | proprietary login; private API | requires reverse engineering | n/a | private | same | NOT-FEASIBLE |
| iFlow | n/a | n/a | n/a | n/a | **Service shut down 2026-04-17** (`platform.iflow.cn`) | NOT-FEASIBLE (dead) |

## Evidence captured during research

- Live Copilot wire probe (read-only, this machine's own `gh` credential;
  token value never printed):
  - `GET https://api.githubcopilot.com/models` with `Authorization: Bearer gho_…`
    → **HTTP 200**, 51 models (`gpt-4o`, `gpt-5.x`, `claude-sonnet-5`, …).
  - `POST /chat/completions` `gpt-4o-mini` → **HTTP 200**
    `"machina copilot wire ok"` + usage `{prompt 17, completion 6}`.
  - `GET https://api.github.com/copilot_internal/v2/token` (both `token` and
    `Bearer` schemes) → **HTTP 403** "For more on scraping GitHub…" — the
    legacy JWT exchange is no longer usable by non-first-party clients, so
    Machina must call the API with the GitHub token directly.
- Claude Code 2.1.150 OAuth config extracted from the installed binary
  (`strings`), matching Hermes `agent/anthropic_credentials.py`:
  client id, `platform.claude.com/v1/oauth/token`, manual redirect,
  scopes `user:profile user:inference user:sessions:claude_code
  user:mcp_servers user:file_upload`, loopback `http://localhost:{port}/callback`,
  JSON token requests, `oauth-2025-04-20` beta.
- Gemini CLI OAuth flow + Cloud Code Assist wire read from the current
  `google-gemini/gemini-cli` `main` (Apache-2.0):
  `packages/core/src/code_assist/oauth2.ts` (client id/secret, scopes,
  loopback + user-code, credential caching) and `server.ts`/`converter.ts`
  (endpoints, request wrapper, SSE chunk shape).
- Deprecations: gemini-cli consumer login (2026-06-18), Code Assist Standard/
  Enterprise sunset schedule (2026-10-09 announcement), iFlow shutdown
  (2026-04-17), Antigravity plugin archive (2026-08-27).

## What this wave implements

1. `gemini` provider — gemini-cli Cloud Code Assist OAuth (enterprise
   licenses; typed refusal for consumer accounts).
2. `claude` provider — Claude Pro/Max subscription via the Claude Code
   public client (Messages API with Bearer + OAuth betas).
3. `copilot` provider — GitHub Copilot subscription (device flow + `gh`
   import; direct Bearer against `api.githubcopilot.com`).

Exact next steps for the POSSIBLE rows are in the lane report and the
per-provider proofs.

## Round 2 (2026-10-14): Kiro sign-in + Kimi K3 sign-in

### Kiro (AWS) — NOT-FEASIBLE as a native provider; POSSIBLE as ACP interop

**On-disk evidence (this machine, read-only).** `kiro-cli` v2.12.1 is
installed (`/Users/shadow/.local/bin/kiro-cli` → `/Applications/Kiro CLI.app`)
and logged in: `kiro-cli whoami` → `Logged in with Builder ID` (free tier).
Credentials live in:

- `~/.aws/sso/cache/kiro-auth-token-cli.json` and `kiro-auth-token.json` —
  `{accessToken, refreshToken, expiresAt, region, authMethod: "IdC",
  provider: "BuilderId", clientIdHash, startUrl, oauthFlow, scopes:
  [codewhisperer:completions, codewhisperer:analysis,
  codewhisperer:conversations]}` (values never printed);
- `~/.aws/sso/cache/<clientIdHash>.json` — the dynamically registered AWS
  SSO OIDC client `{clientId, clientSecret, expiresAt}` (per-user, not a
  scraped app id);
- macOS Keychain items `kirocli:odic:token` / `kirocli:odic:device-registration`
  (binary strings); `~/.kiro/` holds settings only; the IDE keeps its state
  under `~/Library/Application Support/Kiro/`.

**Flow (official docs + binary).** `kiro-cli login --license free` = Builder
ID (`https://view.awsapps.com/start`) or Google/GitHub/Amazon social;
`--license pro --identity-provider <start-url> --region <r>` = IAM Identity
Center; `--use-device-flow` forces the headless device flow (docs: "The CLI
displays a URL and a one-time code…"). Tokens refresh through
`https://oidc.{region}.amazonaws.com/token` (refresh_token grant with the
registered client pair; `kiro-cli debug refresh-auth-token` exists).

**Wire.** The binary names the service surfaces: `codewhisperer.{region}.amazonaws.com`
(older CodeWhisperer protocol; `AmazonCodeWhispererService.ListAvailableProfiles`,
`GetUsageLimits`, `GenerateRecommendations`, `SendTelemetryEvent` targets,
AWS event-stream framing), `q.{region}.amazonaws.com` (Amazon Q family) and
the newer `runtime.{region}.kiro.dev` (v3 agent engine; `kiro-cli acp
--agent-engine v3`). Models are Kiro aliases: `chat.defaultModel =
"claude-sonnet-5"` on this machine; community listings show opus-4.x,
sonnet-4.x, haiku-4.5, deepseek-3.x, minimax, glm-5, qwen3-coder, `auto`.
Free tier = Builder ID (credits), Pro = Identity Center.

**Why native is NOT-FEASIBLE.** Kiro's ToS prohibits third-party
proxy/harness use and AWS actively enforces it: the OmniRoute maintainer's
ban discussion (Aug 2026) documents anti-fraud flags on non-IDE
fingerprints/IPs, rapid post-import traffic, bulk imports, and an
identity-verification recovery process for banned accounts. Reading
`~/.aws/sso/cache` tokens would be technically clean (dynamic client, no
scraped secret) but it *is* the community-proxy pattern that gets accounts
banned → violates Machina's no-user-harm rule.

**Exact next steps (interop lane, if pursued).** The legitimate pattern is
driving the official binary over ACP (the "compliance-first" approach used by
`ankitcharolia/kiro-gateway`): a follow-up lane would add an ACP client
transport (`kiro-cli acp --agent-engine v2|v3`), mirroring the harness's
subprocess discipline (`machina/core/child_process.run_bounded`), exposing it
as a provider or a `machina interop` surface. Files a follow-up touches:
`machina/core/provider/kiro_acp.py` (new), `machina/core/provider/factory.py`
+ `specs.py` (registration), `machina/cli/` (surface), tests + a proof that
runs one official-CLI turn. Risks to state up front: ACP is a moving target,
per-turn latency, credit consumption, and it only works where the user is
already legitimately logged in to Kiro.

### Kimi Code (K3) — POSSIBLE-WITH-CAVEATS (not implemented this wave)

**Flow pinned from the official OSS CLI** (`MoonshotAI/kimi-code`, MIT;
published bundle `@moonshot-ai/kimi-code@2.2.0`):

- Device authorization: `POST https://auth.kimi.com/api/oauth/device_authorization`
  (form `{client_id: "17e5f671-d194-4dfb-9706-5516cb48c098"}`) →
  `user_code`, `device_code`, `verification_uri(_complete)`, `interval`,
  `expires_in`;
- Poll: `POST /api/oauth/token` (form `{client_id, device_code,
  grant_type: "urn:ietf:params:oauth:grant-type:device_code"}`) with
  `authorization_pending` / `slow_down` / `expired_token` / `access_denied`
  handling;
- Refresh: `POST /api/oauth/token` (form `{client_id, grant_type:
  refresh_token, refresh_token}`), 429/5xx retried with exponential backoff,
  `401/403/invalid_grant` → re-login;
- Store: `~/.kimi-code/credentials/<name>.json`, 0600 file / 0700 dir, atomic
  temp+fsync+rename (the exact Machina store discipline); config
  `~/.kimi-code/config.toml` carries the managed provider under
  `managed:kimi-code`;
- Wire: `https://api.kimi.com/coding/v1` (global region:
  `https://api.kimi.ai/coding/v1`), usage at `{base}/usages`; Kimi's
  `/coding` route is Anthropic-compatible and asks for a claude-code
  User-Agent (Hermes reference), and the CLI's managed provider speaks the
  OpenAI protocol — a follow-up must pin which one Machina uses (the
  Anthropic route lets the existing `AnthropicProvider` machinery be reused);
- Model catalog in the bundle: `moonshotai/Kimi-K3` / `moonshot-ai/kimi-k3`
  (1M context, reasoning, tools, image input) plus `Kimi-K2.7-Code`.

**Caveat.** Moonshot documents third-party integration as "obtain an API Key
to integrate Kimi Code's model capabilities into third-party tools" while
`/login` OAuth is described as the official-app path. Community bridges
(e.g. `pi-provider-kimi-code`) reuse the OAuth login, but Moonshot does not
explicitly bless that; unlike Kiro there is no observed ban program — hence
POSSIBLE-WITH-CAVEATS rather than NOT-FEASIBLE. Re-verify the current
`kimi.com/code/docs` third-party page before shipping.

**Exact next steps (kimi lane).** Mirror the claude/copilot wave:
`machina/security/kimi_auth.py` (device flow against `auth.kimi.com`, own
store `~/.machina/auth/kimi.json`, read-only import from
`~/.kimi-code/credentials/*.json`, refresh with backoff), `machina/core/provider/kimi.py`
(thin subclass over `AnthropicProvider` or `DirectProvider` depending on the
pinned wire; Kimi K3 default), factory/specs/`__init__`/runtime wiring,
`machina auth kimi login|status|logout`, tests (device-flow polling matrix,
refresh, import, stream) and a live proof — the device-flow login is
interactive, so the live-proof shape is: user runs `kimi` once (or the
device flow in the CLI), then the import path + a real K3 completion are
provable. No Kiro-style ban evidence exists today; keep the caveat in the
status output.
