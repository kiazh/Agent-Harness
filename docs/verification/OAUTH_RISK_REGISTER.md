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
