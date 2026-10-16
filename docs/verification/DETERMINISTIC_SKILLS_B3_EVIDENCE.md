# B3 — Compaction/status coexistence and release evidence (spec §17-B3, §14.7, §16.9)

- **Task:** B3 — compaction/status coexistence and release evidence. Implemented and verified at the runtime/loop boundary where the acceptance criteria are behavioral, not source-inspection.
- **Date:** 2026-10-16 (America/New_York), ~17:00.
- **Baseline (moving):** Machina HEAD `0f7eb2c` ("feat(context): wire both loops through the shared request gate (B2, shadow default)"); Machina-DevTools HEAD `b109027` ("tests: B2 loop-wiring suite + artifact tool surface + patch-target update"). Worktree also carries unrelated in-flight `ui/*` edits by another agent (untouched); this task's code changes are `machina/core/request_gate.py` and `machina/tools/artifacts.py` only.
- **Method:** executable tests + one disposable PostgreSQL database (`MACHINA_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/machina_test`). No live provider calls were made anywhere in this evidence. Spec source: `/Users/shadow/Desktop/Machina_Revised_Deterministic_Skills_and_Rolling_Context_Plan.md` (not in-repo).
- **Test file:** `Machina-DevTools/tests/test_b3_coexistence.py` (13 tests: 6 provider-free, 7 DB-backed; DB tests skip without the DSN).

## 1. What changed

1. **`machina/core/request_gate.py` — bounded decision status (§14.7).** `evaluate_request` now emits exactly one `request_projection_decision` audit event per evaluation via `_audit_decision` (`request_gate.py:497`): `session_id, mode (shadow|wired), degraded, would_fit, refusal, estimated_tokens, effective_budget_tokens, request_tokens, passes, projected`. Labels, booleans and counts only — never message bodies, pins, or refusal text. Shadow/refusal/wired-success call sites: `:300`, `:417`, `:447`, `:482`; a wiring failure that degrades to the legacy path emits the same event marked `degraded=true`. Emission is best-effort and must never change the decision (covered by the degraded-path test). `_shadow_note` now returns its redacted report (`:334`) so the shadow decision event cites the same numbers as the shadow log.
2. **`machina/tools/artifacts.py` — `artifact_read` exposes the store's bound cursor (C14).** The store already rejected stale cursors (`artifacts.py:516-527`, `:728-752`); the tool surface could not request or return one. `artifact_read` now takes `cursor` (empty = legacy offset/limit, unchanged), cites `revision=<content_revision>` in its header, and appends `next_cursor=<cursor>` when more remains (`tools/artifacts.py:45-78`). This is the minimal way to make stale-binding rejection reachable end-to-end by the model; no behavior changed for callers that do not pass a cursor (the existing header-prefix pin still passes).
3. **No changes to `services.py` / `turn_engine.py`.** The coexistence reviews in C13 found no race that needs a guard: the gate is a pure read-only evaluator with no claim code path (static test `test_gate_module_has_no_claim_coordination_imports` and runtime test `test_gate_completes_under_held_mutation_and_attempts_no_claim` pin this), and `try_begin_turn`/`begin_mutation` already mutually exclude on one durable row claim (`turns.py:129-160, 302-503`). Adding a hook would be redundant state, so none was added (justified per task instructions).
4. **One legitimate pin moved:** `Machina-DevTools/tests/test_b2_loop_wiring.py::test_wired_refusal_is_structured_with_no_provider_call_in_both_loops` expected exactly one audit event per refusal gate; it now expects the legacy capacity event **plus** the new bounded `request_projection_decision` refusal record and asserts both.

## 2. Commands and observed counts

All from `/Users/shadow/Machina-DevTools`, interpreter `/Users/shadow/machina/.venv/bin/python` (pytest 9.1.1; `PYTHONDONTWRITEBYTECODE=1`, temp HOME per `run-tests.sh`):

| Command (suite arguments) | Result |
| --- | --- |
| `… ./run-tests.sh tests/test_b3_coexistence.py -q -rs -p no:cacheprovider` (with DSN) | **13 passed** (0 failed) |
| same, **without** `MACHINA_TEST_DATABASE_URL` | **6 passed, 7 skipped** (all 7 = DSN-gated; clean skip path) |
| Targeted bundle: `test_b3_coexistence test_b2_loop_wiring test_request_projection test_te_artifacts test_te_views_integration test_te_reducers test_te_review_fixes test_compaction_checkpoint test_compression test_turns_ownership test_claim_failure_boundary test_turn_engine test_holistic_h04_compaction_prompt` (with DSN) | **190 passed** (0 failed, 3.71 s) |
| Regression bundle: `test_architecture_layers test_leaf_independence test_observability_payload_boundary test_audit_remediation test_fm3_audit_verdicts test_r5_018_audit_redaction test_history_condensation test_model_context_limits test_turn_completion_lifecycle test_s5_turns_counters test_a5_turn_flow test_b0_contract` (with DSN) | **197 passed** (0 failed, 4.39 s) |
| Pre-change baseline of the B2/B1/artifacts/compaction files | 62 passed — all still green above |

Lint: `machina/.venv/bin/python -m ruff check machina/core/request_gate.py machina/tools/artifacts.py` → clean (repo config, 100 cols). `ruff check tests/test_b3_coexistence.py` → clean.

## 3. C13 — coexistence and ownership (DB-backed)

The gate evaluates **under the active turn's authority without acquiring a competing post-turn mutation claim** (§14.7):

- **(a) Gate completes while a compaction mutation is held, no claim attempted.** `test_gate_completes_under_held_mutation_and_attempts_no_claim` acquires a real `begin_mutation` claim, runs the wired gate to a projected decision, and asserts: the traced claim calls made during the gate are exactly `[]`; the DB row still carries the mutation owner token and `claim_kind='mutation'`; `try_begin_turn` is refused while the mutation is held; a second `maybe_auto_compact` attempt is refused with no `compress_session` call; release restores a free row. The static companion test pins that `request_gate` contains no claim-coordination import or call at all.
- **(b) Auto-compaction defers on a live turn (existing busy path), then runs once.** `test_auto_compaction_defers_on_live_turn_and_never_double_accounts` holds a real turn claim: `maybe_auto_compact` returns `None` **without** claiming or compressing (deferral at acquisition), turn claim untouched. After `end_turn`, the same call compacts exactly once, releases its mutation claim, and records `last_auto_compact_tokens` once. An immediate re-run returns `None` via the no-progress guard with zero additional `compress_session` calls and no claim left behind — no double-accounting and no double-compaction.
- **(c) Wired turn + real `schedule_auto_compact` ≡ flag-off in ownership terms.** `test_wired_turn_then_scheduled_compaction_matches_flag_off_ownership` runs the full loop twice (flag on/off) under a real claim row, releases the turn like the transports do, then executes the **actual** `TurnEngine.schedule_auto_compact` (`turn_engine.py:699`) and awaits its tracked task. Both runs produce the identical ownership trace `[begin_turn, end_turn, begin_mutation, end_mutation]`, exactly one compaction attempt per scheduling call, a free claim row afterwards, and no `last_auto_compact_tokens` write when compression returned None.

## 4. C14 — restart/reconnect durability (DB-backed)

- **Fresh connection:** `test_recovered_artifact_cites_revision_across_reconnect_and_fresh_process` stores a record with `source_version="machina:b3-test"`, then reads it back through a **new `Database` pool**; `read` and `describe` both cite the stored `content_revision` and `source_version`, with byte-exact content.
- **Fresh OS process:** the same test spawns an isolated Python process against the real DB (real `Database` connect, no inherited pool) and asserts the recovered page cites the same stored revision/source version with byte-exact content. This is a process-level recovery read, not a full service restart (see §8).
- **Stale bindings rejected end-to-end through the tool surface, never wrong data:**
  - `artifact_read` page 1 returns `revision=…` and `next_cursor=…`; resume succeeds while the binding matches; after simulating revision drift (`UPDATE … content_revision`), resuming with the cursor raises `ToolError("cursor revision no longer matches the retained artifact; issue a new read")` with zero content bytes in the error, and the store-level error code is the stable `stale_cursor`. A plain new read (no cursor) is the explicit way to accept the new basis and states the drifted revision.
  - `artifact_search` for a one-match page returns a `next_cursor`; after the head artifact's revision drifts, resuming raises `ToolError("search cursor evidence basis changed; issue a new search")`, again with no content in the error; a fresh search finds the content on the new basis.
- **Cross-session recovery stays denied:** `test_cross_session_recovery_stays_denied_through_tools` reads/search/describes another session's artifact through the tool surface and gets `ToolError(... not accessible)` for all three — this closes the runtime acceptance gap B0 listed as unexecuted (§6.1 of the B0 audit).

## 5. C11 — effective-budget change

- **Seam:** `test_mid_session_route_change_recomputes_the_budget_at_the_seam` — the same session object moves from a tagged 6 000-token route to a tagged 64-token route; the next `evaluate_request` recomputes `effective_budget` (6000 → 64) and refuses with the structured `pins_exceed_capacity` instead of sending.
- **Loop via the fake provider:** `test_loop_refits_next_iteration_after_a_model_window_change` — the scripted provider mutates the session's model/window pin after call 1; iteration 2's gate uses the new budget and refuses before any second provider call (`len(provider.calls) == 1`, `outcome="failed"`, pins message). The bounded decision events show `would_fit [True, False]` with `effective_budget_tokens` shrinking — the status surface and the decision agree.

## 6. Provider-native continuation (C05)

`test_signed_provider_blocks_survive_byte_for_byte_into_next_request`: a scripted provider returns an assistant turn carrying signed `provider_content` blocks (`thinking` with signature + `redacted_thinking`), a bulky tool result forces real projection on the wired second request, and the second outgoing payload is asserted to (1) fit the reduced budget, (2) contain a `[machina view:` verified view (projection actually engaged), and (3) carry the signed blocks **byte-for-byte** (canonical JSON equality against the probe's unprojected payload, deep-copied, never aliased) with tool-call ids/args identical. This exercises the real B2 seam and real loop, not just `project_request` in isolation.

## 7. §14.7 — honest status (and what is still unavailable)

- **Added (small, default-safe):** the bounded `request_projection_decision` audit event described in §1.1, one per evaluation, in both flag modes plus refusals and degraded wiring. Tests: `test_request_projection_decision_is_emitted_bounded_in_both_modes` (shadow and wired fields; asserts no goal/constraint/message bodies in the serialized events) and `test_degraded_wiring_is_recorded_not_silent` (forced `_build_input` failure → legacy request + hard guard still apply, event `degraded=true`).
- **Compaction scheduling status — met (2026-10-16 follow-up).** The B3 baseline recorded this as an unmet gap; a follow-up closed it **without changing scheduling behavior**. `maybe_auto_compact` (`services.py`) and `TurnEngine.schedule_auto_compact` (`turn_engine.py:714`) now emit bounded, honest counters through the existing metrics collector and are surfaced by the existing snapshot accessors — `metrics.get_counter` / `metrics.get_all_metrics` (`machina/core/metrics.py`) and `prometheus_text()` at `/metrics` (`machina/observability/metrics.py`). Counter names:
  - `compaction.scheduled` — emitted at schedule time, before any claim is attempted (`turn_engine.py`), so a scheduled-but-never-started task stays distinguishable.
  - `compaction.started` — emitted only once the MUTATION claim is acquired (`services.py`).
  - `compaction.completed` — emitted when compression actually committed (`services.py`); a bounded `compaction_status` audit event carries `tokens_before`/`tokens_after` (size before/after when available).
  - `compaction.failed` — emitted with the error **class** on the bounded `compaction_status` audit event (never the exception message).
  - `compaction.skipped.disabled` (config off), `compaction.skipped.busy` (live claim / no claim acquired), `compaction.skipped.below_threshold`, `compaction.skipped.no_progress`, `compaction.skipped.session_missing`, `compaction.skipped.no_change` — exactly one skip counter per decision; a deferred/busy attempt emits no `started`.
  This satisfies the spec §21 item "Configuration and status distinguish scheduled, skipped, complete, and failed" and §14.7's scheduled / skipped/busy / started / completed / failed / size before-after. Test evidence: `Machina-DevTools/tests/test_compaction_status.py` (10 tests: disabled/busy/below-threshold/no-progress/no-change/session-missing skips, a real DB-backed compaction (started+completed with before/after), an induced failure (failed+class), scheduled-then-busy (one skip, zero started), and the Prometheus snapshot; DB-backed cases gated on `MACHINA_TEST_DATABASE_URL`). No source content, prompt, credential, or raw exception message enters any counter or event.

## 8. Release statement (spec §21, Track B) — honest status

What this task's evidence supports at runtime: **durable recovery + revision binding + stale-cursor refusal** (B0/B3 C14), **one shared projector called by both loops with scripted providers** (B2, re-verified here), **safe continuation where projection fits and structured refusal where it cannot** (B1/B2/B3), **separate in-turn vs post-turn ownership rules under real DB claims** (B3 C13), and **pinned/provider-native content survival through the real seam** (C05).

What remains open, and therefore **not checked/labelled done**:

- live-provider replay (a real Anthropic/OpenAI-shaped transport, including real signature validation) — no live calls were made;
- full production-process restart with in-flight state (this evidence restarts a connection and an OS process for recovery reads, not the service);
- concurrency/load: all DB-backed proofs are sequential single-operation; multi-transport parallelism relies on the atomic claim predicate (`turns.py:129-145`) by construction, not by stress test;
- compaction scheduling status metrics (see §7) — **closed in the 2026-10-16 follow-up** (counters met; no longer open);
- eviction-after-compaction (`eviction_max_tokens`) and CI/UI surfacing of the new event are untested here.

**Rollout decision: `request_projection_enabled` stays `False` in `DEFAULTS` (`machina/core/config.py:290`).** Justification: the wired path has now passed real loop, real DB-claim, and real recovery tests — but only against scripted providers and the local token counter. No evidence exists that a real provider transport accepts the re-rendered payload, and the §17-B3 acceptance explicitly keeps "wired, runtime unverified" until the actual loops **and** the provider-facing recovery path pass the contract. The default shadow path remains byte-identical to pre-B2 behavior plus the bounded decision event, so the release carries the observability without moving request payloads.

## 9. Handoff block (spec §20)

```text
Task ID/status: B3 — COMPLETE for loop/ownership/durability evidence; provider-native live replay Unexecuted (flag stays off)
Baseline: machina 0f7eb2c (B2), DevTools b109027; this task changed machina/core/request_gate.py, machina/tools/artifacts.py, DevTools tests/test_b3_coexistence.py (new, 13 tests), tests/test_b2_loop_wiring.py (one legitimate audit-count pin)
Requirement IDs implemented: C13 (gate makes no claim; turn/mutation mutual exclusion pinned; wired≡flag-off ownership trace), C14 (fresh-connection + fresh-process recovery cites revision/source_version; stale read/search cursors rejected through tools; cross-session denied), C11 (mid-session route change refits/refuses), C05 (signed blocks byte-for-byte through wired gate), §14.7 (bounded decision audit; compaction scheduling status counters met per the 2026-10-16 follow-up below)
Exact source/API/schema changes: request_gate.py +_audit_decision +shadow-report return (audit only); tools/artifacts.py artifact_read +cursor param +revision in header +next_cursor line; no schema/DB changes; no services/turn_engine changes (none needed)
Red→green: test_b2_loop_wiring refusal audit count moved 1→2 (legitimately, decision event added); everything else green pre/post
Green commands: DB DSN runs per §2 — B3 13 passed; targeted 190 passed; regression 197 passed; ruff clean; without DSN 6 passed/7 skipped
Coexistence proofs: gate claim trace == [] under held mutation (DB); compaction deferred while turn holds session then compacts exactly once; no-progress re-run skips; real TurnEngine.schedule_auto_compact = 1 claim attempt/run; ownership track identical flag-on vs flag-off
DB-backed evidence: claims rows on sessions (begin/end turn+mutation), artifact revision/source_version, stale_cursor rejection (read+search), cross-session denial, last_auto_compact_tokens written once
Provider-native survival: scripted Anthropic-shaped provider; signed provider_content blocks + tool-call ids/args byte-for-byte in the next wired request; no live provider calls
Unexecuted/limitations: live provider replay incl. real signature validation; full service restart; concurrent multi-transport stress; eviction path; UI surfacing (status counters were previously listed here and are now met — see the follow-up line)
Follow-up (2026-10-16, post-B3): compaction scheduling status counters added — services.py `_record_compaction_status` + calls in `maybe_auto_compact`, turn_engine.py `schedule_auto_compact`; counters compaction.{scheduled,started,completed,failed} + compaction.skipped.{disabled,busy,below_threshold,no_progress,session_missing,no_change}; existing snapshot accessors (metrics.get_counter/get_all_metrics, prometheus_text /metrics) surface them; DevTools tests/test_compaction_status.py 10 passed (DB DSN), 3 passed/7 skipped without DSN; B3 13 passed, targeted bundle 239 passed with the unrelated skills/runtime.py `import re` worktree breakage neutralized in a throwaway plugin (that file is another agent's in-flight edit, untouched here); ruff clean
Rollout: request_projection_enabled remains False (shadow default); decision and justification in §8
Next task and first action: B3-followup — live-provider replay (sandbox account) of the wired payload incl. signed-block round trip, then a concurrency run, before any flag change
```
