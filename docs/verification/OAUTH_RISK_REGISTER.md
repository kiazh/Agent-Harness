# OAuth Risk Register (S5.1 STRIDE-lite)

Date: 2026-10-13. Scope: `machina/connectors/oauth*.py` (coordinator, bearer
adapter, store, callback, network, models) plus the human management routes in
`machina/gateway/features/connector_oauth.py` as the authorization boundary.

## Trust boundaries

| # | Boundary | Code | Mechanism |
|---|----------|------|-----------|
| B1 | Token storage | `oauth_store.py` | AES-256-GCM, fresh nonce/write, AAD binds record version + credential key; file-perm checks; atomic index CAS on generation; tombstones defeat late writers |
| B2 | Token refresh | `oauth.py::ensure_token` | Cross-process refresh lock; `in_flight` journal before POST; generation fencing at commit; scope-widening rejected; revalidation of config/binding after refresh |
| B3 | Browser callback | `oauth_callback.py` | 127.0.0.1-only listener; exact Host/path match; `compare_digest` on 256-bit state; once-only delivery; `iss` pinning when advertised; bounded reads/sockets/params |
| B4 | Network egress | `oauth_network.py` + `http_transport.py` | HTTPS-only (literal-loopback http only with `local_access`); DNS-pinned dialing preserving Host/SNI/TLS verify; no redirects; bounded bounded reads; endpoint pinning; SSRF fence (metadata/link-local/multicast/reserved refused; mixed-class refused; public→private pivot refused) |
| B5 | Human authorization | `gateway/features/connector_oauth.py` + `_run_operation` broker guard | Every management route binds `OAuthOwner` to the authenticated session and checks connector ownership; preview digest freeze; discovery fingerprint revalidation; scope allowlist; broker audit/lease record on the operation |

## Method

STRIDE-lite per boundary (spoofing / tampering / info-disclosure / elevation;
repudiation via broker audit records; DoS via named bounds). Bare-`except:`
count is zero in `oauth*.py`; broad `except Exception` sites were each
reviewed for fail-closed behavior. No behavior was changed: no CRITICAL/HIGH
items were found, so there are no code fixes in this pass — only the two
info-disclosure regression tests in `tests/test_s5_oauth_stride.py` locking
previously untested-but-passing properties.

## Findings (all deferred)

| ID | Boundary | Category | Severity | Finding | Deferred rationale |
|----|----------|----------|----------|---------|--------------------|
| R1 | B5 | Elevation | Medium | `OAuthCoordinator.forget()` takes `owner` but enforces it only via the gateway `_authorized_owner` check, not internally (unlike `cancel()` handle checks). A direct in-process caller could retire credentials without owner proof. | No agent/model path reaches the coordinator directly (not a tool; management routes only). Internal handle check would be defense-in-depth; revisit if coordinator gains new callers. |
| R2 | B3 | Tampering | Low | `_invalid_count` is observability-only: repeated invalid callback attempts never rate-limit or lock the listener. | State is 256-bit random per operation with a 300s TTL; online guessing is infeasible. Listener is loopback-only with bounded sockets. |
| R3 | B3 | DoS | Low | `_sockets` increment/decrement races under concurrent connects (no lock), so the `MAX_SOCKETS` cap is approximate. | Accept loop is bounded by `limit=` + read timeouts; worst case is a slightly soft cap on localhost. |
| R4 | B1 | Info-disclosure | Low | Crashed writers can leave orphan `.enc` envelopes (key-protected ciphertext) until next GC pass. | Ciphertext without the master key is not exploitable; index never references orphans. |
| R5 | B2 | Info-disclosure | Low | `client_secret_basic` secret bytes live in immutable Python strings from `_get_secret` until GC (cannot be scrubbed post-use). | Standard language limitation; secrets never logged (module has no logger) and `OAuthCredentialRecord.__repr__` is secret-free (now regression-tested). |
| R6 | B5 | Spoofing | Low | `_run_operation`'s broker guard uses a management auto-approval handler, so the broker record is audit/lease — the actual human authorization is the gateway-authenticated preview→start→browser drive, not an approval card. | Documented design: the human proves presence by completing the provider flow in their own browser. Changing this would alter the UX contract; out of scope for hardening-only. |
| R7 | B4 | Spoofing | Low | DNS resolve-then-validate has inherent TOCTOU vs DNS rebinding between validation and connect. | `pinned_http_transport` resolves once per connection through the same validated resolver and dials only approved numeric IPs; residual window is the resolver's own TTL behavior. |
| R8 | B1 | Tampering | Low | `credential_key_for` defaults `revision=0`; config-revision changes are enforced by `_record_matches_config`, not by key separation. | Enforced at every load path (`ensure_token`, `_auth_view`, `_primary_binding`); key separation would be redundant rotation churn. |

## Verdict

No CRITICAL or HIGH items. The highest-risk file cluster (token-handling
`_run_operation`, store CAS logic, callback validation, SSRF fence) is
fail-closed throughout: every reviewed broad-except either degrades to a
safe state (`signed_out` / `storage_unavailable` / `failed`) or propagates.
Regression tests added: `tests/test_s5_oauth_stride.py` (2 tests).
Existing suites `test_connectors_oauth_*` + gateway OAuth suites remain green.

## Subscription-provider compliance update (2026-10-14, owner-directed)

Scope note: this register otherwise covers the connector OAuth boundary above;
this section records the risk decision for Machina's own LLM subscription
sign-ins, whose vendor terms changed during the round-4 review.

| Change | Reason (primary sources) | Disposition |
|---|---|---|
| **Removed:** Claude Pro/Max OAuth flow (`security/claude_auth.py`, `core/provider/claude.py`, the `auth claude` CLI sub-app, `provider=claude` routing, stored `~/.machina/auth/claude.json`) | Anthropic legal-and-compliance (2026-02-19): Free/Pro/Max OAuth tokens are for Claude Code and native Anthropic apps only — "not permitted in any other product, tool, or service"; third-party developers may not offer Claude.ai login or route requests through subscription credentials; enforcement/bans documented (2026-02). | Removed entirely. Kept: `provider=anthropic` API-key path (`ANTHROPIC_API_KEY`), unchanged. Removal guard: `tests/test_subscription_removals.py`. |
| **Removed:** Gemini Code Assist OAuth flow (`security/gemini_auth.py`, `core/provider/gemini.py`, the `auth gemini` CLI sub-app, `provider=gemini` routing, stored `~/.machina/auth/gemini.json`, gemini-cli `~/.gemini/oauth_creds.json` import) | Google Gemini CLI FAQ: third-party OAuth reuse is a direct ToS violation subject to account suspension; abuse detection live (discussion #22970, 2026-10-11) and account/project suspensions documented (#18999, #25648). | Removed entirely. Kept: `provider=google` API-key path (`GOOGLE_API_KEY`), unchanged. Removal guard: `tests/test_subscription_removals.py`. |
| **Added:** Kimi Code API-key provider (`provider=kimi`, `KIMI_API_KEY`, `https://api.kimi.ai/coding/v1`) | Kimi Code docs (2026-10-14): subscribers "can also obtain an API Key to integrate Kimi Code's model capabilities into third-party development tools and platforms" — officially sanctioned, key-based (no OAuth piggyback), first-party guides for Claude Code/OpenCode/Codex. | Accepted. Proof: `tests/test_kimi_provider.py` (spec + MockTransport wire shape). |
| **Kept:** GitHub Copilot seat (`provider=copilot`) | No prohibition found in current GitHub Generative AI Services Terms; GitHub actively ships an ACP server for third-party use. Raw-token reuse remains a grey area; the sanctioned bridge is the official Copilot CLI (ACP/`-p`), scoped as a future migration. | Kept, with the nuance noted. |

A user or saved session pinned to a removed provider name now receives the
standard unknown-provider error; no removal-specific error machinery exists.
Evidence: `docs/verification/proofs/codebase_review_round4_2026-10-14/R4-GEMINI_CLAUDE_KIMI.md`.
