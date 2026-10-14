# External round-3 auth re-verification (findings 1-3)

Fix verification for the round-3 review findings against the fixed tree
(`machina/security/chatgpt_auth.py`, `machina/permissions/policy.py`,
`machina/security/secrets.py`, plus the gateway logout flow). The original
review scripts in `../codebase_review_round3_2026-10-14/` are acceptance
evidence and were run **unmodified**; this directory only contains a runner
that reads them.

Run from the repository root with the repository virtualenv:

```
PYTHONPATH=. .venv/bin/python docs/verification/proofs/external_round3_auth_2026-10-14/run_proof.py
```

`run.log` records a successful run (2026-10-14, synthetic data only; no real
credentials, no real network, no production database). The two original
scripts were also run standalone; their raw outputs are kept as
`auth-security.log` and `auth-lifecycle.log`.

* `auth-security.log` exits 1 **by design**: the script's first section calls
  the production broker without an approval handler, and the read now
  requires credential approval (`READ_DECISION pending` →
  `NeedsApproval`), so the script stops before its later sections. Those
  sections' fixed behavior is shown by `run_proof.py` (see below).
* `auth-lifecycle.log` exits 0 and shows the cancelled-pending-flow behavior.

## What the fixed behavior is

**Finding 1 (P1) — resolved token-store path + redaction.** The permission
predicate now resolves each Machina credential store through the store's own
`auth_path()` (default `<machina_home>/auth/chatgpt.json`, `MACHINA_CHATGPT_AUTH_FILE`
override, and the claude/copilot/gemini stores), so a workspace that contains
the store gates a read exactly like `credentials`. Token values are registered
with the process-wide redaction registry at the store's read/write boundary,
so they are redactable even outside named-secret resolution.

* Original `auth-security.py`: `READ_DECISION pending` (was `allowed`), and
  `permission_broker.guard` raises `NeedsApproval` — the file tool never runs
  and the refresh token is never returned.
* Isolated section (same verbatim source): `RAW_STORE_CONTAINS_REFRESH True`
  vs `REDACTION_CONTAINS_REFRESH False` — even a raw copy is redactable.

**Finding 2 (P2) — logout invalidates pending authentication work.**

* Gateway logout clears the pending flow before removing the store; the flow
  still answers a redirecting callback but refuses to exchange and emits
  `auth.chatgpt.failed` (`reason: cancelled`) instead of `completed`.
* The store's authentication generation is bumped under its store lock before
  the unlink (durably, next to the store, so a CLI logout fences a login in
  another process). `exchange_code` and `refresh` refuse to publish when the
  generation they captured no longer matches, and `exchange_code` also
  refuses when the raw store changed under it (a raw unlink case).
* Original `auth-lifecycle.py`: `pending_after_logout False`,
  `credential_recreated_after_logout False`, events `["auth.chatgpt.failed"]`.
* Original `auth-security.py` section `resurrection_after_logout`:
  `STORE_RESURRECTED False` — a refresh response that lands after the store
  was removed is returned in memory but never published.

**Finding 3 (P2) — rotating refresh exchanges serialized by store.**
`refresh` takes the repository's flock idiom (`<store>.lock`) across the whole
reread -> exchange -> publish section, so the second caller waits, rereads the
store, adopts the rotated pair, and never submits the consumed token.

* The original section's `threading.Barrier(2)` fixture can only be satisfied
  by the pre-fix race (two submissions). A correct single-exchange fix sends
  exactly one request, so the barrier's missing second party raises
  `BrokenBarrierError` (recorded as `ORIGINAL_FIXTURE_RAISED`). That is a
  fixture artifact, not a defect.
* The race is therefore also run with the handler responding immediately
  (barrier neutralized; production `refresh`, real store, real lock, two real
  threads unchanged): `REFRESH_POST_COUNT 1` with both callers receiving the
  rotated pair and no `invalid_grant`.

## Catalog invalidation on logout

The catalog module's only in-process invalidation hook is
`clear_catalog_cache()` (all live/CLI-cache state). Gateway logout calls it:
the process serves a single ChatGPT account, so the signed-out account's live
entries cannot survive a logout and answer account-less reads. The gateway
test `test_logout_clears_in_process_catalog_state` pins it. No catalog module
change was needed, so its god-file ceiling did not move.
