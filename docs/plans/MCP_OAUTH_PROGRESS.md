# MCP OAuth progress
Base HEAD / branch / date: 4c945cc / main / 2026-10-12
Implementation plan: MCP_OAUTH_IMPLEMENTATION_HANDOFF.md

## Decisions and supported scope
- Current SDK/dependency/platform facts: mcp==2.3.0, httpx 0.28.1, httpx2 2.13.1, cryptography 50.0.2, keyring absent (added as default dep, lazy import, fake provider in tests)
- Credential-store mode: AES-GCM file store under ~/.agent-harness/oauth with master key from AGENT_HARNESS_OAUTH_MASTER_KEY or OS keyring; fake provider in tests
- Any justified deviation, with test evidence: none yet

## Tasks
- [x] Task 1 contracts/config
- [x] Task 2 secure store
- [x] Task 3 network/discovery
- [x] Task 4 callback
- [x] Task 5 actual grant
- [x] Task 6 refresh
- [x] Task 7 cancellation/invalidation
- [x] Task 8 gateway
- [x] Task 9 native transport/execution
- [x] Task 10 UI
- [x] Task 11 final verification/docs
- [ ] Task 7 cancellation/invalidation
- [ ] Task 8 gateway
- [ ] Task 9 native transport/execution
- [ ] Task 10 UI
- [ ] Task 11 final verification/docs

## Evidence
### Task 1 contracts/config — GREEN 2026-10-12
- Failing test first: tests/test_connectors_oauth_config.py (new) + existing test_public_view_carries_no_secrets (exact field set now includes oauth).
- Failure observed: public_view set mismatch; secret-sentinel substring rule rejected legitimate client_secret_ref.
- Implementation: ah/connectors/oauth_models.py (OAuthSettings strict validation, reason codes, bounds); models.py oauth field + _validate_oauth + normalized public_view.
- Passing command: .venv/bin/python -m pytest -q tests/test_connectors_oauth_config.py tests/test_connectors_config.py tests/test_connectors_management.py -p no:cacheprovider → 57 passed.
- Limitations: none.
### Task 2 secure store — GREEN 2026-10-12
- Red: new modules absent (import error); tamper/symlink/tombstone cases fail without guards.
- Implementation: ah/connectors/oauth_store.py (AES-GCM envelopes, atomic index pointer, CAS generations, tombstones, advisory locks); ah/security/oauth_keys.py (env secret → keyring with backend allowlist, atomic creation lock, no plaintext fallback); pyproject + uv.lock (cryptography, keyring default deps; uv lock --check --offline clean).
- Passing command: .venv/bin/python -m pytest -q tests/test_connectors_oauth_store.py -p no:cacheprovider → 6 passed. Fake key provider in all tests; no live keyring touched.
- Limitations: Windows ACL boundary is chmod + documented fail-closed guidance; no real Windows host verified (fakes prove algorithm only).
### Task 3 network/discovery — GREEN 2026-10-12
- Red: initial run 3 failed (issuer candidate paths mis-guessed as {issuer}/.well-known/... instead of spec base-path order; helper clobbered explicit 404; live-DNS hostnames in hermetic tests).
- Implementation: ah/connectors/oauth_network.py (httpx client follow_redirects=False/trust_env=False/identity encoding; pre-request DNS incl. mixed-answer rejection; https-only except literal-loopback + local_access; public-to-private pivot rejection; quoted-comma challenge parser; path/root PRM + OAuth/OIDC issuer candidates; 404/410-only fallback with fail-stop; 128KiB/64KiB streaming caps; S256/resource/issuer/endpoint validation; scope allowlist authority; DCR/exchange/refresh/revoke boundaries with token validation).
- Passing command: .venv/bin/python -m pytest -q tests/test_connectors_oauth_network.py -p no:cacheprovider → 7 passed.
- Limitations: connect-time DNS pinning not claimed (pre-request classification only, documented residual).
### Task 4 callback — GREEN 2026-10-12
- Red: new module absent (import error).
- Implementation: ah/connectors/oauth_callback.py (127.0.0.1-only asyncio server, reserved-inert until armed, exact path + literal Host validation, single/duplicate param rejection, state compare_digest, iss rules per RFC 9207 advertisement, once-only delivery, fixed success/error HTML with no-store/no-referrer/CSP headers, no query logging).
- Passing command: .venv/bin/python -m pytest -q tests/test_connectors_oauth_callback.py -p no:cacheprovider → 10 passed.
### Task 5 actual grant — GREEN 2026-10-12
- Red: first run failed on test-owner mismatch (two _owner() calls) plus stray orphaned-operation noise; fixed test, green.
- Implementation: ah/connectors/oauth.py coordinator (immutable preview digest, fast start + owner task, broker tool.mcp_oauth_authorize approval, registration only after start, one code exchange, encrypted persistence before complete, 60s terminal retention); tests/support/mcp_oauth_fixture.py (uvicorn on owned loopback socket, PRM/OAuth/OIDC metadata, DCR/authorize/token/revoke, real SDK MCP behind bearer enforcement, fault switches).
- Passing command: .venv/bin/python -m pytest -q tests/test_connectors_oauth_flow.py -p no:cacheprovider → 5 passed (DCR+PKCE encrypted grant, preview freeze/ownership, preregistered/Basic/metadata-document, unsupported registration + scope widening, lost-exchange no-reuse).
### Task 6 refresh — GREEN 2026-10-12
- Red: new ensure_token paths absent.
- Implementation: OAuthCoordinator.ensure_token (60s skew, unknown-expiry use-until-challenged, asyncio + cross-process refresh locks, in_flight journal, same-grant rotation preserving grantId/generation, conservative reauth on ambiguity, generation CAS at commit).
- Passing command: .venv/bin/python -m pytest -q tests/test_connectors_oauth_refresh.py -p no:cacheprovider → 4 passed (proactive + same-loop x6 collapse, cross-process x4 spawn exactly-one, in_flight crash → reauth with zero POST, invalid_grant with zero tool dispatch).
### Task 7 cancellation/invalidation — GREEN 2026-10-12
- Red: lifecycle races unexercised.
- Implementation: coordinator.cancel/forget/invalidate_connector + service._refresh_locked invalidation hooks + runtime shutdown ordering (existing code from tasks 5-7, proven here).
- Passing command: .venv/bin/python -m pytest -q tests/test_connectors_oauth_lifecycle.py -p no:cacheprovider → 3 passed (suspended-exchange + forget → signed_out with no resurrection; refresh-after-logout refused; disconnect preserves / forget removes + revocation honest + shutdown clean).
### Task 8 gateway — GREEN 2026-10-12
- Red: 7 new methods absent from registry; existing test_features_split exact-set failed until extended.
- Implementation: ah/gateway/features/connector_oauth.py (preview/start/status/url/open/cancel/forget with strict unknown-param checks, session-owner resolution via gw.get_session, connector-owner check on forget); registry + exact-method test updates.
- Passing command: AGENT_HARNESS_TEST_DATABASE_URL=... .venv/bin/python -m pytest -q tests/test_gateway_connector_oauth.py -p no:cacheprovider → 3 passed (token required on all methods + unknown params rejected + no raw mcp.call; full RPC flow across separate calls with owner isolation + single explicit browser open + stale-generation forget refused; exact method inventory). Also fixed an IndentationError my earlier no-op edit introduced in execution.py (caught by test_gateway_connectors).
- Note: DB tests use only agentharness_test via AGENT_HARNESS_TEST_DATABASE_URL.
### Task 9 native transport/execution — GREEN 2026-10-12
- Red: httpx2 rejected a non-Auth adapter (TypeError); Starlette Mount 307 + missing SDK lifespan broke the fixture handshake (500); global fallback coordinator shadowed the managing service (fixed with authoritative manager injection); ToolError-based 401 fault redesigned to real transport-level revocation.
- Implementation: ah/connectors/oauth_auth.py (httpx2.Auth subclass, one-yield bearer, no post-response replay); mcp_client preflight + 401/403→AUTH_REQUIRED/unknown + OAuth token redaction; execution auth identity in proposals + pre-dispatch _oauth_preflight + generation binding; manager authoritative coordinator injection; fixture spliced SDK routes + combined lifespan + revoke_on_mutate/auth_failure_status faults.
- Passing command: AGENT_HARNESS_TEST_DATABASE_URL=... .venv/bin/python -m pytest -q tests/test_connectors_oauth_execution.py -p no:cacheprovider → 6 passed (reviewed connect + echo via real SDK + full-mode agent pause/resume; 401/403 transport → outcome_unknown with effect counter exactly 1 and zero exchange/refresh; timeout unknown; expired-no-refresh + post-forget pre-dispatch refusal with zero dispatch; 3 connect/disconnect cycles with no leaked owner tasks).
### Task 10 UI — GREEN 2026-10-12
- Red: 3 initial failures (add-flow pick queue, too-fast test status, ANSI-styled theme rows in assertions); fixed tests + sanitizeStatus ANSI stripping.
- Implementation: protocol.ts OAuth types; types.ts showOAuthProgress contract; components/OAuthProgress.ts (cancellable overlay, transient link viewer, provider-text sanitization, narrow-terminal bounds); app.ts real overlay via openDialog; features/mcp-oauth.ts (preview→review→start→poll→authorized→connect, 500ms single-outstanding polling, overall deadline, URL only in overlay, Cancel-default forget + revocation reporting); features/mcp.ts (Add None/OAuth choice, Configure OAuth via partial update, Authorize/Reauthorize/status/Forget, no OAuth controls for stdio); backend partial-update merge in service.configure.
- Passing: ui typecheck + eslint clean; node --test test/mcp-oauth.test.ts → 8 passed (incl. real gateway wrapper over stdio proving method boundary + INVALID_PARAMS/METHOD_NOT_FOUND without DB/browser); test/features.test.ts 106 passed (3 existing add tests extended with the None auth pick).
### Task 11 final verification/docs — GREEN 2026-10-12
- Focused suites: oauth config/store/network/callback 41 passed; flow/refresh/gateway-oauth/execution/lifecycle 21 passed; pre-existing connector client/manager/service/management/execution/config/policy 120 passed.
- Full backend: 2290 passed, 5 skipped, 10 deselected (load), 81.38% total coverage (gate 60). Baseline was 2237 passed; +53 new backend tests, zero regressions.
- UI: npm run check green (typecheck; 303 tests: 297 pass, 6 pre-existing e2e skips; eslint).
- Ruff check + format clean on all touched files; `uv lock --check --offline` clean; `git diff --check` clean.
- Bandit: 0 medium/high severity (111 lows, same idioms as baseline — asserts, try/except/pass, string comparisons flagged as B105/B106 false positives). No suppressions added.
- Distributions: hatchling wheel+sdist built to a disposable dir; check_release_artifacts verifies both; wheel contains all 8 OAuth modules; default deps include mcp==2.3.0, cryptography, keyring.
- Docs: new docs/MCP_OAUTH.md (setup, storage/recovery, limitations, evidence); docs/MCP.md + README.md deferred-OAuth statements replaced with supported-scope language; unrelated staging/production gates untouched.
- No live .env reads, no production DB (only agentharness_test), no real browser (injected fakes; webbrowser default never invoked in tests), no keyring writes, no commit/push/publish.

## Current checkpoint
Files changed: see `git status` (8 new backend modules, 1 gateway module, 1 security module, 9 new test files + fixture, UI flow/overlay/types/protocol/app, docs/MCP_OAUTH.md + MCP.md + README).
Last passing command: full backend 2290 passed / UI npm run check green (2026-10-12).
Unresolved failure and reproduction: none. (One self-inflicted IndentationError in execution.py during development was caught by test_gateway_connectors and fixed with a regression trail in the evidence above.)
Next exact action: none — implementation complete; remaining work is explicit deferred scope below.

## Acceptance matrix
- Default install includes OAuth → wheel contains ah/connectors/oauth*.py + deps (`hatchling build` + `check_release_artifacts.py` green).
- Old stdio/unauthenticated HTTP unchanged → test_connectors_client/manager/service/management/execution/config/policy 120 passed.
- Header/path/root metadata → test_header_first_then_path_then_root_discovery_order.
- OAuth and OIDC metadata → issuer_metadata_candidates order test + fixture routes all three.
- Malicious discovery fails closed → mismatch/unsafe/mixed-DNS/oversized/compressed tests.
- Multiple issuers not silently selected → candidates error path (covered in coordinator preview contract; fixture advertises one issuer).
- Public DCR → test_dcr_pkce_flow (register counter 0 at preview, 1 after start).
- Preregistered public and Basic → test_preregistered_and_basic_and_metadata_document_modes.
- Configured metadata document → same test (metadata_document mode through full login).
- S256 and exact exchange → fixture recomputes challenge from verifier; missing/plain-only fail.
- Callback state/issuer/path → test_bad_callback_does_not_consume_good_flow + parametrized rejections + concurrent duplicates.
- Browser wait bounded/cancellable → cancel/expiry/shutdown tests + UI escape test.
- Secrets not exposed → no-secret-bytes disk assertions + public-view/JSON-RPC/transcript/link-privacy tests.
- At-rest encryption → restart recovery, tamper/wrong-key/AAD, permissions tests.
- Secure master key → env-malformed + fake-backend + no-plaintext-fallback tests.
- Cross-worker refresh → 4-process spawn exactly-one test.
- Refresh crash safety → in_flight restart forces reauth with zero POST.
- Logout wins late callbacks → suspended-exchange + forget test; refresh-after-logout test.
- Approval binds auth identity → reauth/forget pre-dispatch refusal tests.
- No replay → mutate revoke + 401/403/unknown tests with effect counter exactly one.
- Pre-dispatch auth failures known → expired-no-refresh counter-zero test.
- Broker remains mandatory → full-mode agent pause/resume test.
- Teardown ownership preserved → 3-cycle connect/disconnect + no leaked owner tasks.
- UI usable → 8 mcp-oauth UI tests + real gateway wrapper.
- Provider revoke honest → forget tests (succeeded/unsupported/failed/not_requested, local always effective).
- Operational docs honest → docs/MCP_OAUTH.md supported/deferred matrix.

## Deferred and operational gates
Only list explicit scope boundaries; do not mark unbuilt planned work complete.
- Remote hosted callbacks, distributed operations, device-code, password/implicit grants, JWT assertions, DPoP/mTLS, OIDC user login: not built.
- HTTP credential profiles: still deferred (auth_required).
- Windows file-ACL hardening: not independently verified; prefer native credential backend.
- Live-vendor certification: fixtures only; real-provider compatibility is operator work.
- No production release claimed; unrelated staging/production gates untouched.
