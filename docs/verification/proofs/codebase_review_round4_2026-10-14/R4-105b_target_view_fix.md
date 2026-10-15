# R4-105b — Approval-view `target`-column leak: store-side fix

- Date: 2026-10-15 · Lane: approval-view target-column leak (follow-up to R4-G3).
- Scope: exactly two files — `machina/permissions/store.py` + marker removal in
  `tests/test_r4_105_approval_view_secrets.py`. `broker.py` read-only. No commit.

## Finding (from G3)

The approval top-level `target` column (`"|".join(req.targets)[:500]`) was
persisted raw and served unredacted: `public_view()` redacted
`proposal.targets`/`argv`/`network`/`shell`/`display`/`content_preview` but not
`target`, and `get_approval()` returned the stored row without passing through
`public_view()` at all — so a secret embedded in *targets* reached
`list_pending`/`get_approval` (gateway/REST/CLI surfaces).

## Fix (view boundary only; durable row intact)

- `public_view()` now redacts a string `target` via `_redact_public_text`,
  same helper as every other field (fail-closed to `[REDACTED]` when the
  redactor is unavailable). Covers legacy rows persisted before this boundary.
- `get_approval()` now returns `public_view(_with_completeness(...))` on both
  the durable and in-memory paths (same composition `list_pending` already
  used). Write path (`create_approval`) untouched: the durable row and the
  in-memory mirror keep the raw `target`; execution identity
  (digest/raw argv via `claim_execution`/`proposal_of`) is unchanged.
- Re-redaction is idempotent (redactor guards `[REDACTED_…]` markers), so
  `matches_approval` (`proposal_of(request) == record["proposal"]`) is
  unaffected.

## Behavior change (called out)

`get_approval()` now returns the redacted public view instead of the raw
stored row. Internal readers only use status/digest/consumed/proposal —
safe — except session-grant scope derivation (`api/routes/approvals.py:51`,
`gateway/features/mode.py:199`), which reads `rec["target"]` for
`scope_path` and will now see the redacted value on secret-bearing targets.
Fail-closed direction (grant may fail to match → re-approval), never a
widening. `find_pending`/`find_approved_unclaimed` (broker-internal resume
path, not a listing surface) still return raw rows, per the lane's
"public_view/get_approval only" scope.

## Proof

Strict-xfail flipped to hard pass by marker removal only (no assertion
weakened — `git diff` on the test file shows only the 9-line
`pytest.mark.xfail` deletion):

- `tests/test_r4_105_approval_view_secrets.py`: **3 passed** (argv end-to-end,
  target live card, target durable-record+listing — the last formerly
  strict-xfail; strict XPASS would fail, so the flip itself is the proof).
- Adjacent: `tests/test_approvals_ownership.py` (10) + 
  `tests/test_fm3_approval_store.py` (8) + the above (3) = **21 passed**
  (with `MACHINA_TEST_DATABASE_URL` exported from `.env`; ownership's
  module-level `skipif` otherwise skips — pre-existing import-time env
  artifact, unrelated to this change).
- `ruff check` + `ruff format --check`: clean on both touched files.
