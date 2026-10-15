# Fix lane — final review research metrics (2026-10-15)

- **Date:** 2026-10-15
- **Lane:** FIX (research metrics). Sole writer of `machina/research/locomo.py`,
  `machina/research/head_to_head.py`, `machina/research/identity_eval.py`, the
  affected tests, and `tests/test_final_review_fix_research_metrics.py`.
- **Findings:** F11 (P2) from
  [`docs/verification/CODEBASE_REVIEW_FOLLOWUP_2026-10-15.md`](../../CODEBASE_REVIEW_FOLLOWUP_2026-10-15.md);
  P2-1 and P3-3 from
  [`line_rag_tools_skills.md`](line_rag_tools_skills.md).
- **Method:** RED-first, fixture-driven. Each finding has a failing test on the
  pre-fix sources and a passing test after the smallest safe change. No metric is
  faked: the title baseline is now a real, comparable measurement, and an
  undefined rate is reported as absent rather than as `0.0`.
- **No commits** were made.

## Files changed

| File | Change |
| --- | --- |
| `machina/research/head_to_head.py` | `success` now also requires no recorded harness error and `harness_exit_code == 0`; design-note docstring updated. |
| `machina/research/identity_eval.py` | `DriftMetrics.attack_rejection_rate`/`benign_acceptance_rate` are `float \| None` and `None` when their denominator is zero. |
| `machina/research/locomo.py` | Title-only baseline builds a comparable session→evidence ranking (title hits carry `chunk_id=None`); content-derived session titles for the title baseline. |
| `tests/test_research_phase2.py` | Replaced the test that pinned the structural-zero contract with one that pins the corrected behavior. |
| `tests/test_final_review_fix_research_metrics.py` | **New** RED-first evidence suite (6 tests). |

## Finding 1 — F11 (P2): title-only retrieval evaluation forced to score zero

**Root cause.** `evaluate_session_recall(..., title_only=True)` filtered hits to
`source == "title"` and then built the ranking from
`identifiers[hit.chunk_id]`. `RecallHit` for a title hit always has
`chunk_id=None` (`machina/core/session_recall.py`), so the ranking was always
empty; recall/MRR were a hard `0.0` even when a title matched. Every temporary
session also carried the single constant title `"LoCoMo transcript"`, so no title
could ever match a question.

**Fix (`locomo.py`).**
1. Titles are content-derived for the title baseline via
   `SessionManager.generate_title(window[0].text)` (production parity). The
   transcript (`session-recall`) baseline keeps the neutral placeholder: its
   ranking ignores title hits, but a content-derived title would collide with the
   dialogue chunk in `discover`'s own content-hash dedup and evict it — that would
   distort an unrelated, already-correct measurement.
2. The title-only ranking is a comparable session→evidence ranking: each `title`
   hit contributes its session's dialogue IDs in turn order (`session_evidence`),
   trimmed to `k`. This measures whether a title-only search can locate the
   session that holds the evidence — the honest question the old code skipped.

**Honesty guard.** A title that does not mention the evidence still scores `0`
(`test_title_only_does_not_credit_an_unmatched_title`): the fix attributes a
*matched session* to its evidence, it does not fabricate hits.

**RED → GREEN.** `test_title_only_scores_evidence_when_title_matches` and the
rewritten `test_session_recall_title_only_backend_scores_matched_session` fail on
pre-fix sources (`hit_rate 0.0`), pass after (`hit_rate 1` for a matching title).

## Finding 2 — P2: head-to-head success ignores harness exit status/error

**Root cause.** `run_one` derived `success` only from the graded pytest run and
the fixture-integrity diff (`grade()` runs unconditionally at `:736`, even after
the harness errored). A crashed harness that never ran still scored
`success=True` on a green-suite task.

**Fix (`head_to_head.py`).** The verdict now conjunctively requires
`not result.get("error")` and `result.get("harness_exit_code") == 0`, alongside
the existing graded-pass/integrity/timed-out checks. The module design note was
updated to state the rule.

**RED → GREEN.** `test_harness_nonzero_exit_cannot_score_success` (stub
`opencode` exits `2` after a green edit) and
`test_harness_error_cannot_score_success_on_green_suite` (patched `run_machina`
raises, `result["error"]` set) both scored `success=True` pre-fix and
`success=False` after. Existing controls (genuine fixes, symlink containment)
still pass.

## Finding 3 — P3: identity_eval undefined rejection rate reported as `0.0`

**Root cause.** `attack_rejection_rate=rejected_attacks / attacks if attacks else 0.0`
made a scenario set with no injected transitions print `0.0`, indistinguishable
from "every attack was accepted".

**Fix (`identity_eval.py`).** `attack_rejection_rate` is now `float | None` and
`None` when `attacks == 0`; the same treatment is applied to the sibling
`benign_acceptance_rate` (`None` when `benign == 0`), which had the identical
defect. `injected_transitions`/`transitions` remain as the explicit counters, and
`to_dict()` serializes the absent rate as JSON `null`.

**RED → GREEN.** `test_identity_no_injected_transitions_reports_undefined_rate`
reads `0.0` pre-fix and `None` after (plus `to_dict()` shows `None`).
`test_identity_with_injected_transitions_still_reports_rate` and the existing
`test_labeled_identity_sequence_evaluation_cleans_up` confirm the defined rate is
unchanged.

## Verification

Logs live in [`logs/`](logs/).

| Evidence | Command | Result |
| --- | --- | --- |
| RED (pre-fix sources) | `pytest tests/test_final_review_fix_research_metrics.py tests/test_research_phase2.py` | **5 failed, 23 passed** (`logs/RED_pre_fix.txt`) |
| GREEN (focused suites) | `pytest tests/test_head_to_head_grader_adversarial.py tests/test_head_to_head_symlink_containment.py tests/test_final_review_fix_research_metrics.py tests/test_research_phase2.py tests/test_research_integration.py tests/test_locomo_answers.py tests/test_fm6_locomo.py tests/test_session_recall_extended.py tests/test_session_recall.py tests/test_recall_filters.py tests/test_identity_gate.py tests/test_r5_007_scheduler_identity.py tests/test_session_web_research.py` | **173 passed** (`logs/GREEN_focused.txt`) |
| GREEN (per finding) | `pytest tests/test_final_review_fix_research_metrics.py -v` | **6 passed** (`logs/GREEN_per_finding.txt`) |
| Lint/format | `ruff check` + `ruff format --check` on the 5 touched files | **clean** (`logs/ruff.txt`) |

The RED log was captured by stashing only the three source files
(`git stash push -- machina/research/{locomo,head_to_head,identity_eval}.py`),
running the new suite against the resulting pre-fix sources, then
`git stash pop`. The shared worktree contains concurrent lanes; only these three
files were stashed, so no other lane was disturbed.

**Per-finding status: F11 fixed · P2 fixed · P3 fixed.**

### Orthogonal note (not this lane)

`tests/test_benchmark_tool.py::test_core_sections_run_and_write_json` fails in
the shared worktree with
`ImportError: cannot import name 'should_write_first' from 'machina.core.progress'`.
`machina/core/progress.py` is mid-edit by a concurrent lane (it is modified in
the worktree and no longer defines `should_write_first`, which
`machina/research/benchmark.py` imports). None of this lane's files import
`progress`, and the failure reproduces independently of them
(`logs/orthogonal_benchmark_import.txt`). It is excluded from the GREEN log for
that reason and is left untouched.
