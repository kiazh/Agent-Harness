# Final review — permissions fix lane: F1 (`:N:path` revspec), F2 (clustered `grep -d`), F4 (resume-helper view boundary)

Date: 2026-10-15 · Lane: FIX (sole writer of `machina/permissions/policy.py`,
`machina/permissions/store.py`, their affected tests, and one new evidence test
file). No commits. Read-only elsewhere.

Source findings: `docs/verification/proofs/final_review_2026-10-15/line_security_permissions.md`
(F1 `:N:path` revspec; F4 unredacted `find_pending`/`find_approved_unclaimed`)
plus the relaunched-review addendum (F2 clustered `grep -d recurse`).

## Files changed

| File | Change |
| --- | --- |
| `machina/permissions/policy.py` | F1: revspec split handles `:[<n>:]<path>`; F2: compact clusters whose `-d` ACTION is `recurse` force review |
| `machina/permissions/store.py` | F4: `find_pending` / `find_approved_unclaimed` route through `public_view` |
| `tests/test_permission_risk_policy.py` | RED-first pins for F1 (`:path`, `:N:path`, benign, `.env.example`) and F2 (clustered `-d recurse`, routine non-recurse value) |
| `tests/test_final_review_permissions_views.py` | NEW evidence suite (F4 memory + durable + broker boundary) |

No selection/count behavior changed in any helper.

---

## F1 · P2 — REV:path gate misses git's index-stage revspec (`:N:path`)

**Root cause.** `sensitive_path` (`policy.py`) recovered the file half of a
revspec with `text.split(":", 1)[1]`. That is correct for `HEAD:path` but wrong
for git's index-stage form `:[<n>:]<path>`: for `:0:credentials` the tail is
`0:credentials`, which matches no sensitive name, so `git show :0:credentials`
was classified as a routine git read (`allowed`) and read the credential.

**Fix (smallest safe change).** After the first-colon split, drop an
all-digit stage prefix when a second colon is present, then classify the path
half exactly like `HEAD:path`:

```python
rev_tail = text.split(":", 1)[1]
stage, sep, rest = rev_tail.partition(":")
if sep and stage.isdigit():
    rev_tail = rest
parts.extend(p.lower() for p in rev_tail.split("/") if p)
```

`HEAD:path`, `:path`, and `path` (no colon) are unchanged; `.env.example`
remains excluded by the existing dotenv rule; the drive-letter guard
(`text[1] == ":"`) is untouched. The strip fires only on `segment:rest` where
`segment` is all digits and `rest` is non-empty.

**RED (pre-fix):** `logs/fix_permissions_red_first.log` — 5 index-stage
credential commands failed (`:0:credentials`, `:0:.netrc`, `:0:id_rsa`,
`:2:id_rsa`, `:0:credentials.1`), all returned `allowed`.
**GREEN (post-fix):** `logs/fix_permissions_after_fix_probe.log`:

```
git show :0:credentials   -> pending (… credential_access)
git show :credentials     -> pending (… credential_access)
```

Benign forms stay routine: `HEAD:README.md`, `:README.md`, `:0:README.md`,
`:0:docs/notes.md`, `HEAD:.env.example`, `:0:.env.example`.

---

## F2 · P2 — `grep` compact cluster with non-first `d` bypasses recursive-read review

**Root cause.** `_recursive_traversal`'s grep branch detected recursion from the
exact `-d`/`--directories` forms, the attached `-dX` form, `--directories=X`,
and any cluster containing `r`/`R`. It missed a compact cluster where `d` is
non-first (`-id`, `-sd`, `-End`). `-id` matches commands.py's
`-[A-Za-z0-9]+` compact shape, so the fail-closed guard never fires and `grep`
is in `_PROJECT_COMMANDS` → `allowed`. `grep -id recurse TOKEN .` recurses
(including hidden files) with no approval.

**Fix.** Inside the compact-cluster branch, after the `r`/`R` check, locate
`d`; the ACTION value is the rest of the cluster, else the next argv (grep's
`-d ACTION` is value-taking). Force review only when that value is exactly
`recurse`, so legitimate non-recurse clusters keep working:

```python
position = cluster.find("d")
if position != -1:
    value = cluster[position + 1 :]
    if not value and index + 1 < len(args):
        value = args[index + 1]
    if value == "recurse":
        return True
```

**RED (pre-fix):** `logs/fix_permissions_red_first.log` — `grep -id recurse …`,
`grep -sd recurse …`, `grep -End recurse …` all returned `allowed`.
**GREEN (post-fix):** `logs/fix_permissions_after_fix_probe.log`:

```
grep -id recurse TOKEN .   -> pending (… opaque_execution_review)
grep -id TOKEN file        -> allowed (routine trusted project command)
```

The routine pin (`-id` whose value is a non-recurse token) is locked by
`test_grep_d_option_cluster_with_non_recurse_value_stays_routine`.

---

## F4 · P3 — `find_pending` / `find_approved_unclaimed` bypass the redacted view boundary

**Root cause.** Both helpers returned `_with_completeness(dict(row))` with no
`public_view`, unlike `get_approval` / `list_pending`. The top-level `target`
column is persisted raw, and the broker surfaces these rows verbatim through
`NeedsApproval`, so the same approval could be shown raw or redacted depending
on which helper ran.

**Fix.** Wrap each return in `public_view`, mirroring `get_approval`:

- durable `find_pending`: `public_view(_with_completeness(dict(row)))`
- memory `find_pending`: `public_view(dict(rec))`
- durable `find_approved_unclaimed`: `public_view(_with_completeness(candidate))`
  (the `candidate.get("proposal")` selection check still runs on the raw row)
- memory `find_approved_unclaimed`: `public_view(_with_completeness(rec))`

Selection SQL/conditions, ordering, limits, and the returned
`request_id`/`session_id`/`digest`/`status`/`proposal` fields are unchanged;
only `target` (and proposal/display, as already done by `public_view`) is
redacted.

**Consumers verified before/after.**
- `broker._guard_headless` / `_guard_interactive` (`broker.py:199,204,213,253`)
  surface the dict via `NeedsApproval` (redaction is the fix) and read only
  `approved["request_id"]` for the claim (unchanged).
- `protocol.approval_summary`/`approval_fields` render `card["target"]` verbatim
  → now redacted.
- `controller.wiring.record_approval_pause` reads `approval["target"]` for an
  observe-only span → now redacted.
- `gateway/features/mode.py:185` derives grant scope from `get_approval`
  (already redacted before this change; unaffected).
- `core/approval_resume.resumed_request` receives records from `get_approval`,
  not these helpers; unaffected.

**RED (pre-fix):** `logs/fix_permissions_red_first.log` — 3 failures, first with
`"raw secret leaked through view"` (`target` carried the raw secret).
**GREEN (post-fix):** new suite (6 passed) covers memory path, durable-FakeDB
path, field/selection preservation, and the broker `NeedsApproval` and
headless-resume paths.

---

## Verification

Focused suites (`logs/fix_permissions_focused_suites.log`): **390 passed** —
`test_permission_risk_policy`, `test_final_review_permissions_views`,
`test_permissions_store_regressions`, the four `test_approval*` suites,
`test_r4_approvals_deadline`, all five `test_r4_105*`,
`test_delegate_scope`/`test_delegate_validator`, the four remaining
`test_delegation*`/`test_review_delegation` files, `test_fm3_approval_store`,
`test_fm3_policy_scope`, `test_fm8_s5_broker`, `test_http_approval_resume`,
`test_memory_approval`.

Adjacent consumers (`logs/fix_permissions_adjacent_suites.log`): **108 passed, 1 skipped** —
`test_debug_permissions_hardening`, `test_failed_delegation_usage`,
`test_fm3_hardening`, `test_fm3_audit_verdicts`, `test_fm3_digest_display`,
`test_integration_permissions`, `test_learning_approval_recovery`,
`test_r4_063_064_observability`.

`test_audit_regressions` (`logs/fix_permissions_audit_regressions.log`): **13
passed**.

### Sibling-lane interference (NOT this lane)

While this lane was running, a concurrent fix lane edited
`machina/core/progress.py` (removed `should_write_first`). That broke
`tests/test_delegation_contracts.py`, whose tests monkeypatch
`progress.should_write_first`; the 7 failures are `AttributeError: module
'machina.core.progress' has no attribute 'should_write_first'`
(`logs/fix_permissions_delegation_contracts_sibling.log`). It is unrelated to
permissions: this lane changed only `machina/permissions/{policy,store}.py` and
its tests, and does not import or touch `progress`/orchestrator state. An
earlier focused run on the pre-edit tree (same `test_delegation_contracts`
included) passed 403; it is superseded by the 390-pass post-edit run above,
which excludes only that sibling-broken file.

Lint (`logs/fix_permissions_ruff_check.log`): `ruff check` — All checks passed
(exit 0).
Format (`logs/fix_permissions_ruff_format.log`): `ruff format --check` — 4 files
already formatted (exit 0).

Direct probes: `logs/fix_permissions_after_fix_probe.log` (sensitive_path /
recursive-traversal / decide verdicts for every pinned spelling).

## Logs

- `logs/fix_permissions_red_first.log`
- `logs/fix_permissions_after_fix_probe.log`
- `logs/fix_permissions_focused_suites.log`
- `logs/fix_permissions_adjacent_suites.log`
- `logs/fix_permissions_audit_regressions.log`
- `logs/fix_permissions_delegation_contracts_sibling.log`
- `logs/fix_permissions_ruff_check.log`
- `logs/fix_permissions_ruff_format.log`
