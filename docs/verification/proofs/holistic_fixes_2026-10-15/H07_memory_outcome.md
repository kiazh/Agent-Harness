# H07 — approval-gated memory writes returned the wrong kind of ID as stored success

Lane: H07 (subagent). Repo: /Users/shadow/machina. Source: `docs/verification/CODEBASE_REVIEW_HOLISTIC_2026-10-15.md`
finding H07 (P2). Tests run from the DevTools clone
(`/private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode/devtools_seed_2026-10-15/harness-clone`)
against this harness via `MACHINA_REPO`; a dedicated database (`machina_test_h07`, not the shared
`machina_test`) kept concurrent lanes out of the run. Nothing committed or pushed.

## Summary

Reproduced: yes. `MemoryStore.add()` returned a fabricated `MemoryEntry` whose `id` belonged to
`pending_memories` whenever the approval gate was enabled; the `remember` tool printed
"Memory stored", the gateway/API echoed a stored-shaped memory object, and the returned id could
never resolve to the durable memory that approval later created (approval allocates a different id).
Fix: `MemoryStore.add()` now returns the discriminated union `MemoryEntry | PendingMemory` — the
pending arm is the gate's own `PendingMemory` (`id` = pending row; `memory_id` = the approved
durable id once applicable; `status`/`redactions` preserved) and the durable arm is unchanged. The
tool/gateway/API surfaces, the consolidator, and the TUI were adapted to branch on the type.

## Timeline

- [2026-10-15] Recon: read H07, `machina/memory/store.py` (`add`), `machina/memory/approval.py`
  (`submit`/`approve`), `machina/tools/memory.py` (`remember`), `machina/gateway/features/memory.py`,
  `machina/api/routes/memory.py`, `machina/memory/consolidator.py`, `machina/memory/shared_bus.py`,
  and the UI consumer `ui/src/features/memory.ts`. All six production call sites of
  `memory_store.add` enumerated; the pending branch fabricated a `MemoryEntry` and discarded the
  gate's `PendingMemory`.
- [2026-10-15] RED written: `tests/test_holistic_h07_memory_outcome.py` in the DevTools clone
  (store contract, stored-arm guard, tool message, gateway wire shape, API wire shape, consolidator
  entries). Pre-fix run: **4 failed, 1 passed** — `logs/H07_red_run.log` (the passing pin is the
  stored-arm guard, which must pass before and after).
- [2026-10-15] FIX (GREEN): `MemoryStore.add` returns `pending` (the gate's `PendingMemory`) instead
  of a fabricated `MemoryEntry`; return annotation is `MemoryEntry | PendingMemory` (TYPE_CHECKING
  import only — the runtime store↔approval seam is unchanged). Surfaces adapted; fail-loud guards
  added to the two `bypass_approval=True` privileged completions and the shared-bus delivery.
- [2026-10-15] Consolidator pin added and shown RED against the pre-fix file
  (`git stash push -- machina/memory/consolidator.py` → 1 failed; restore → GREEN):
  `logs/H07_consolidator_red.log`.
- [2026-10-15] GREEN: all six pins pass (`logs/H07_green_run.log`); focused memory/approval/tool/
  gateway-memory suites: **314 passed, 1 skipped** (skip = `test_integration_permissions.py:588`
  "Windows path semantics", unrelated) — `logs/H07_focused_suites.log`; architecture + adjacent
  suites: **192 passed** — `logs/H07_arch_and_adjacent.log`.
- [2026-10-15] UI adaptation verified: `typecheck` clean, **501 passed / 6 pre-existing skips**,
  eslint clean — `logs/H07_ui_green.log`. Ruff check + format clean (`machina/`, `tools/`).
- [2026-10-15] Architecture ratchet: zero source delta (deferred-import census 721/721, unchanged by
  hoisting the two `PendingMemory` surface imports to module level; no god-file/provider growth).

## Reproduced (RED, pre-fix)

`logs/H07_red_run.log` — 4 failed, 1 passed (1.02s):

```
E  AssertionError: assert False
E   +  where False = isinstance(MemoryEntry(id=UUID('4e05266e-...'), ...),
E        <class 'machina.memory.approval.PendingMemory'>)          # store pending arm lies
E  AssertionError: assert 'Memory stored' not in 'Memory stor... every night'
E    'Memory stored' is contained here:
E      Memory stored: [7aca476c-d5f7-4026-8a3e-e2fb87f0ff28] (fact, importance=0.50) The staging database resets every night
E  KeyError: 'status'                                             # gateway memory.add (stored shape)
E  KeyError: 'status'                                             # POST /api/v1/memory (stored shape)
FAILED tests/test_holistic_h07_memory_outcome.py::test_store_add_returns_pending_outcome_under_approval
FAILED tests/test_holistic_h07_memory_outcome.py::test_remember_tool_reports_pending_approval
FAILED tests/test_holistic_h07_memory_outcome.py::test_gateway_memory_add_reports_pending
FAILED tests/test_holistic_h07_memory_outcome.py::test_api_memory_create_reports_pending
4 failed, 1 passed in 1.02s
```

The RED pin also asserts the whole identity chain: the returned id names the `pending_memories`
row, `store.get(returned_id) is None` before *and after* approval, approval allocates a different
durable id, and the pending record's `memory_id` exposes the approved id afterwards.

## Fix

One authoritative implementation: the store's write router owns the outcome, every surface branches
on the outcome type.

- `machina/memory/store.py` — `add()` non-bypass + gate-enabled path returns the gate's
  `PendingMemory` (previously a fabricated `MemoryEntry` with the pending id). Return annotation
  `MemoryEntry | PendingMemory` (TYPE_CHECKING-only import; runtime seam unchanged). Docstring now
  states the two arms and that the ids are distinct.
- `machina/tools/memory.py` — `remember` branches: pending prints
  `Memory pending approval: [<pending id>] (...) — not stored or recallable until approved; review
  with `machina memory-approve <pending id>`.`; the audit `memory_remember` event carries
  `pending_id` instead of `memory_id` on that arm.
- `machina/gateway/features/memory.py` — `memory.add` returns
  `{"status": "pending", "pending": {...}}` (gateway `_pending` shape) or
  `{"status": "stored", "memory": {...}}` (previously always `{"memory": {...}}`).
- `machina/api/routes/memory.py` — `POST /api/v1/memory` returns
  `{"status": "pending", "pendingId": ..., "content": ..., "category": ...}` or
  `{"status": "stored", "memory": {...}}` (previously always `{"memory": {...}}`).
- `machina/memory/consolidator.py` — both write loops discriminate: queued writes are counted
  (`pending_approval`, now on the `memory_consolidate` audit event and the log line) and excluded
  from `ConsolidationResult.entries` / the `consolidate_from_text` return; checkpoint semantics
  unchanged (a queued submission is a completed write; re-running would duplicate queued rows).
- `machina/memory/approval.py` + `machina/memory/shared_bus.py` — the `bypass_approval=True`
  completion paths fail loudly (`RuntimeError`) if a `PendingMemory` ever reaches them, instead of
  silently binding a pending id as the completed/approved `memory_id` or caching a bogus receipt.
- `ui/src/features/memory.ts` — `/memory add` prints a warning notice
  ("Pending approval (<category>, <short id>). Review with /memory pending, then /memory approve
  <short id>.") instead of "Remembered (...)" for the pending arm.
- `ui/test/features.test.ts` — new pin for the pending notice (stored-path test unchanged).

## Callouts (user-visible / API change)

This is a deliberate result-shape change **for the remember/approval path only**:

1. Agent tool output under approval: "Memory stored: [id]" → "Memory pending approval: [pending id]
   … not stored or recallable until approved" (the pending id is explicitly *not* a memory id).
2. Gateway RPC `memory.add` under approval: `{"memory": {...}}` with a fake stored object →
   `{"status": "pending", "pending": {...}}`. Direct writes keep the `memory` payload (plus an
   additive `"status": "stored"`).
3. HTTP `POST /api/v1/memory` under approval: `{"memory": {...}}` → `{"status": "pending",
   "pendingId": ...}`. Direct writes keep the `memory` payload (plus additive `"status": "stored"`).
4. TUI `/memory add` under approval: "Remembered (…)" → pending warning with the approve hint.
5. Consolidation under approval: queued writes are no longer reported as durable `entries`;
   `memory_consolidate` audit gains `pending_approval`.
6. `MemoryStore.add()` public contract is now `MemoryEntry | PendingMemory`; consumers must branch
   on the type before using `.id` as a memory id.

Consumers that never enable `memory_approval_enabled` (the default) are unaffected apart from the
additive `status` fields (2)/(3).

## Files changed

Harness (`/Users/shadow/machina`): `machina/memory/store.py`, `machina/memory/approval.py`,
`machina/memory/consolidator.py`, `machina/memory/shared_bus.py`, `machina/tools/memory.py`,
`machina/gateway/features/memory.py`, `machina/api/routes/memory.py`,
`ui/src/features/memory.ts`, `ui/test/features.test.ts`.

DevTools clone (tests only): `tests/test_holistic_h07_memory_outcome.py` (new, 6 pins).

## Tests / verification

Pins (`logs/H07_green_run.log`, 6 passed):

| pin | covers |
| --- | --- |
| `test_store_add_returns_pending_outcome_under_approval` | pending arm is `PendingMemory`; pending id ≠ approved id; `memory_id` linked after approval |
| `test_store_add_stored_arms_stay_memory_entries` | gate OFF and `bypass_approval=True` stay `MemoryEntry` (guard pin) |
| `test_remember_tool_reports_pending_approval` | tool never says "Memory stored" under approval; pending row exists |
| `test_gateway_memory_add_reports_pending` | RPC pending discriminant, no `memory` key |
| `test_api_memory_create_reports_pending` | HTTP pending discriminant via real ASGI app + test DB |
| `test_consolidator_excludes_approval_queued_writes` | queued writes not reported as durable entries |

Commands (final state):

```
# backend focused sets — cwd = DevTools clone
MACHINA_REPO=/Users/shadow/machina \
MACHINA_TEST_DATABASE_URL='postgresql://postgres:postgres@localhost:5432/machina_test_h07' \
PYTHON=/Users/shadow/machina/.venv/bin/python \
./run-tests.sh tests/test_holistic_h07_memory_outcome.py -v -p no:cacheprovider
# → 6 passed

MACHINA_REPO=... MACHINA_TEST_DATABASE_URL=... PYTHON=... ./run-tests.sh \
  tests/test_holistic_h07_memory_outcome.py tests/test_memory_approval.py tests/test_memory.py \
  tests/test_memory_bug_fixes.py tests/test_memory_embedding_space.py tests/test_shared_memory_bus.py \
  tests/test_gateway_features.py tests/test_gateway_memory_usage.py tests/test_holistic_l2r_memory_fixes.py \
  tests/test_final_review_2026_10_15_batch3_gateway_obs_memory.py tests/test_security.py \
  tests/test_agent_factory_memory.py tests/test_integration_permissions.py tests/test_phase5_completion.py \
  -q -p no:cacheprovider
# → 314 passed, 1 skipped (Windows path semantics, unrelated)

MACHINA_REPO=... MACHINA_TEST_DATABASE_URL=... PYTHON=... ./run-tests.sh \
  tests/test_architecture_layers.py tests/test_architecture_pairs.py tests/test_config_import_cycle.py \
  tests/test_code_quality.py tests/test_r5_f1_consolidation_cursor.py tests/test_fm5_consistency.py \
  tests/test_audit_remediation.py tests/test_bug9_bug15_fixes.py tests/test_phase6.py tests/test_api.py \
  tests/test_features_split.py tests/test_api_route_table.py -q -p no:cacheprovider
# → 192 passed

# lint/format — cwd = /Users/shadow/machina
.venv/bin/ruff check --no-cache machina/ tools/           # All checks passed!
.venv/bin/ruff format --check --no-cache machina/ tools/  # 237 files already formatted

# UI — cwd = /Users/shadow/machina/ui
npx tsc --noEmit -p tsconfig.json                          # clean
node --test --test-reporter=spec test/*.test.ts            # 501 passed, 6 skipped, 0 failed
npm run lint                                               # clean
```

## Architecture ratchet

Zero source-level delta. The deferred (function-level) import census stays **721 <= cap 721** and
`test_deferred_import_count_ratchet` is green; module-level `machina.core` ceilings unchanged; no
god-file/provider totals touched. One iteration note: the first draft imported `PendingMemory`
function-locally in the gateway and API handlers (census 723 > 721, `logs` not kept); the final
shape hoists those two *type* imports to module level — consistent with
`gateway/features/growth.py` and `api/routes/sessions.py`, which already module-import memory
modules — so the ratchet needs no cap change and `tests/test_architecture_layers.py` was not
edited.

## Evidence

- `logs/H07_red_run.log` — pre-fix RED (4 failed, 1 passed).
- `logs/H07_consolidator_red.log` — consolidator pin RED against the pre-fix file.
- `logs/H07_green_run.log` — 6/6 pins verbose GREEN.
- `logs/H07_focused_suites.log` — 314 passed, 1 skipped.
- `logs/H07_arch_and_adjacent.log` — 192 passed (includes architecture ratchets).
- `logs/H07_ui_green.log` — TUI typecheck/tests/lint.

Not run: full backend suite (not requested for this lane; the shared test DB is used by concurrent
lanes). The skipped focus test is an unrelated Windows-path-semantics case.
