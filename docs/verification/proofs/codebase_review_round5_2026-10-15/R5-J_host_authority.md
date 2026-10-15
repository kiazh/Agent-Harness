# R5-J · Host authority formatting (finding 14, P2) — evidence

Lane: R5-J · Files owned: `machina/security/fetch.py` (+ new `tests/test_r5_014_host_authority.py`)
Finding: `CODEBASE_REVIEW_CURRENT_2026-10-14.md` §14 — `Host` contained only the
hostname: explicit non-default ports omitted, IPv6 literals unbracketed.
Fix-plan item: plan §R5-J (RED custom-port/IPv6 → fix → unit + live-shape wire test).

## Verdict: CONFIRMED then FIXED

## RED (reproduced, pre-fix code `Host: {hostname}` at `fetch.py:235`)

New test `tests/test_r5_014_host_authority.py` stubs DNS + `socket.socket` at the
fetch seam (network/provider boundary — allowed mock edge), captures raw request
bytes, and asserts the `Host` header (fake transport returns canned 200):

| Case | URL | Sent (pre-fix) | Expected | Result |
|---|---|---|---|---|
| custom port | `http://example.com:8080/s` | `example.com` | `example.com:8080` | FAIL (named reason) |
| IPv6 literal | `http://[2001:db8::1]/s` | `2001:db8::1` | `[2001:db8::1]` | FAIL (named reason) |
| IPv6 + port | `http://[2001:db8::1]:8080/s` | `2001:db8::1` | `[2001:db8::1]:8080` | FAIL (named reason) |
| https custom port | `https://example.com:8443/s` | `example.com` | `example.com:8443` | FAIL (named reason) |
| default ports | `http://example.com:80/`, `https://example.com:443/` | `example.com` | `example.com` | PASS (already correct) |

Pre-fix run: **4 failed, 1 passed** — exact failure lines:

- `assert 'example.com' == 'example.com:8080'`
- `assert '2001:db8::1' == '[2001:db8::1]'`
- `assert '2001:db8::1' == '[2001:db8::1]:8080'`
- `assert 'example.com' == 'example.com:8443'`

## GREEN (fix)

`machina/security/fetch.py`:

- New `_host_authority(hostname, scheme, explicit_port)` (RFC 9110 §7.2): re-brackets
  IPv6 literals (`":" in hostname` → `[…]`; `urlparse().hostname` strips brackets),
  appends the port unless absent or equal to the scheme default (:80 http / :443
  https, elided). Takes `parsed.port` (None vs explicit-default distinguished), so
  explicit `:80`/`:443` elides per spec. Userinfo excluded — callers pass
  `parsed.hostname`, unchanged.
- `pinned_fetch` computes `authority = _host_authority(hostname, parsed.scheme,
  parsed.port)` per redirect hop (redirects re-parse, so each hop's authority is
  correct) and sends `Host: {authority}`. TCP connect port, SNI
  (`server_hostname=hostname`, correctly unbracketed), DNS, and IP policy untouched.

## Verification

- `tests/test_r5_014_host_authority.py`: **5 passed** (log: `logs/R5-J_green.log`).
- Neighbors (no behavior change on normal paths): `test_handoff_regressions.py`
  pinned-fetch tests (4) + `test_deep_fetch_deadline.py` (10) — 14 passed;
  `test_audit_remediation.py` + `test_web_extract_pinned_fetch.py` +
  `test_review_web_search.py` — 58 passed.
- Scope note: the bare-`Host` path for default-port/no-port URLs is byte-identical
  to before; only custom-port and IPv6-literal requests change (from wrong to
  correct authority). SNI intentionally still uses the raw hostname.

## Files changed (uncommitted, coordinator commits)

- `machina/security/fetch.py` (+17/−1: helper + 2-line call-site change)
- `tests/test_r5_014_host_authority.py` (new, 5 tests)
