# Subscription-auth wave — proofs (2026-10-14)

Providers added: `claude` (Claude Pro/Max via the Claude Code OAuth flow),
`copilot` (GitHub Copilot seat), `gemini` (Gemini Code Assist via the
gemini-cli Cloud Code Assist flow). Research + verdicts:
[`README.md`](README.md).

## Copilot — LIVE proof (PASS)

Script: [`copilot_live_proof.py`](copilot_live_proof.py) ·
log: [`copilot_live_proof.log`](copilot_live_proof.log) · exit 0.

Ran the real `CopilotProvider` transport against `api.githubcopilot.com`
using this machine's own `gh` credential (read-only `gh auth token`; the gh
store is never modified; the token value is never printed — verified by a
prefix scan of the log).

| step | result |
| --- | --- |
| `GET /models` live | HTTP 200, **55 models** on this account's menu |
| legacy `api.github.com/copilot_internal/v2/token` | HTTP **403** (non-first-party clients blocked — why the provider calls the API with the GitHub token directly) |
| import path | `gh auth token` → `~/.machina/auth/copilot.json` (0600), status `{signed_in: true, source: machina, token_family: gho}` |
| live streaming completion | streamed text, terminal `finish_reason`+`[DONE]`, `"The Machina Copilot proof looks fine."`, usage `{prompt 27, completion 9}` |
| live tool-call completion | `get_weather` `{"city":"Paris"}`, usage `{prompt 56, completion 14}` |

Model menu caveat proven live: availability is per-account (`gpt-4o` and
`gpt-4o-mini` work here; `gpt-5.4-mini`/`gpt-6.1-sol` answer HTTP 400
`model_not_supported`). The provider surfaces that as a typed error naming
the fix; `machina auth copilot status` reports the seat honestly.

## Claude — hermetic proof (PASS) + manual step

Script: [`claude_hermetic_proof.py`](claude_hermetic_proof.py) ·
log: [`claude_hermetic_proof.log`](claude_hermetic_proof.log) · exit 0.
No Claude login exists on this machine, so the real `ClaudeProvider`
(production client + headers + auth flow) ran against `httpx.MockTransport`
with a sandboxed HOME:

- import path: fake `~/.claude/.credentials.json` read **read-only**
  (bytes + mtime unchanged) into `~/.machina/auth/claude.json` (0600),
  `subscription_type: max` reported, no token in status;
- OAuth identity on the wire: `authorization: Bearer <redacted>`,
  `anthropic-beta: claude-code-20250219,oauth-2025-04-20`,
  `user-agent: claude-code/2.1.150 (external, cli)`, `x-app: cli`,
  **no `x-api-key`** (dual auth is rejected by the subscription route);
- 401 → refresh → replay once through the real httpx auth flow (the refresh
  wire shape — JSON body, beta header, single-use rotation, peer adoption,
  console-host fallback on 404 — is covered by `tests/test_claude_auth.py`
  against `MockTransport`);
- streamed completion ending in `message_stop`, `machina claude proof ok`.

**Manual step (needs a real subscription):** `machina auth claude login`
(browser PKCE loopback; `--manual` prints the code-paste flow used on
headless machines; `--import-claude` copies an existing Claude Code login),
then `machina chat -p claude -m claude-sonnet-4-5` — that single live call
against `api.anthropic.com` is the only unverified step.

## Gemini — hermetic proof (PASS) + manual step

Script: [`gemini_hermetic_proof.py`](gemini_hermetic_proof.py) ·
log: [`gemini_hermetic_proof.log`](gemini_hermetic_proof.log) · exit 0.
No Gemini license is signed in on this machine; the real `GeminiProvider`
ran against `httpx.MockTransport` with a sandboxed HOME:

- import path: fake `~/.gemini/oauth_creds.json` read **read-only** into
  `~/.machina/auth/gemini.json` (0600); ms-expiry normalization;
- onboarding wire order verified:
  `loadCodeAssist` → `onboardUser(tierId=standard)` → operation poll →
  `streamGenerateContent?alt=sse`, with the resolved project persisted on
  the Machina store and reused by the stream request;
- streamed completion (`finishReason` terminal), usage mapped;
- consumer-account reality: an ineligible account produces the typed
  `gemini_not_eligible` error carrying Google's reason code
  (`INELIGIBLE_TIER`) and the 2026-06-18 deprecation note.

**Manual step (needs a Code Assist Standard/Enterprise license):**
`machina auth gemini login` (browser PKCE loopback) or `--import-gemini`,
then `machina chat -p gemini -m gemini-2.5-pro`. Google's acceptance for a
licensed account is the only unverified step.

## CLI surfaces — live capture

[`auth_surfaces.txt`](auth_surfaces.txt): `machina auth --help` lists the
four provider groups (`chatgpt` top-level aliases preserved), and the three
new `status` screens render honestly on this machine (`copilot` signed in
via the live proof's import; `claude`/`gemini` correctly report "not signed
in" with their login hints).

## Reproduce

```sh
cd /Users/shadow/machina
.venv/bin/python docs/verification/proofs/oauth_research_2026-10-14/copilot_live_proof.py   # exit 0 (2 = no gh login)
.venv/bin/python docs/verification/proofs/oauth_research_2026-10-14/claude_hermetic_proof.py
.venv/bin/python docs/verification/proofs/oauth_research_2026-10-14/gemini_hermetic_proof.py
.venv/bin/python -m pytest tests/test_gemini_auth.py tests/test_claude_auth.py tests/test_copilot_auth.py \
    tests/test_gemini_provider.py tests/test_claude_provider.py tests/test_copilot_provider.py \
    tests/test_oauth_cli_auth.py -q
```

Redaction: no token value appears in any artifact (the copilot script
asserts it line-by-line; the copilot log was additionally prefix-scanned
against the machine's live token → 0 matches; `auth_surfaces.txt` prints
neither tokens nor account secrets).
