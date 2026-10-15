# R4-022 + R4-023 — denied→denied end-to-end and Menu/Picker sanitization (proof)

Date: 2026-10-14 · Status: GREEN (RED-first captured) · Scope: `machina/core/agent.py` (streamed
tool_result outcome lines only), `ui/src/transcript.ts`, `ui/src/components/ToolTurn.ts`,
`ui/src/components/Menu.ts`, `ui/src/components/ApprovalCard.ts`, tests. No commit.

## Root causes (verified at HEAD before fixing)

1. **R4-022** — the streamed `tool_result` events carried no truthful outcome for denied
   actions: the Phase C emit (`agent.py:1159`) forwarded only `outcome_unknown | ""`, and the
   Phase D denied/paused emit (`agent.py:1220`) forwarded nothing. The broker's real denial
   goes through Phase D (`item["_denied"]` items are excluded from `runnable`), so the TUI saw
   `outcome=""` + `isError=false` and painted the green `✔` on
   "Permission denied: … The action was NOT executed." (`ui/src/transcript.ts:194`).
2. **R4-023** — `Menu` rendered title/labels/descriptions without `sanitizeDisplay` (the only
   untrusted-text path skipping it), so ESC/OSC/C1/`\r`/tabs from session titles, recall
   previews and model IDs reached the terminal; `sanitizeDisplay` preserves `\n`, so
   `ToolTurn`'s header and `ApprovalCard`'s dock title concatenated fields containing `\n`
   into a single row element (an in-band break that corrupts row math).

## Fix

- `machina/core/agent.py` — exactly two outcome lines (net +1 line; file 2055/2057 ceiling):
  - Phase C emit: `tool_outcome=_tool_outcome(finished)` (covers `completed`/`failed`/
    `paused`/`outcome_unknown` for executed tools, incl. paused delegations).
  - Phase D denied emit: added `tool_outcome=_tool_outcome(item)` (denied + headless pauses).
- `ui/src/transcript.ts` — `toolOutcomeState(outcome, isError)` centralizes the mapping (see
  below).
- `ui/src/components/ToolTurn.ts` — new `denied` state: marker `⊘` styled `muted` (never the
  success `✔`, never the error `✖`), status line `denied; not executed`; header split on `\n`.
- `ui/src/components/Menu.ts` — `sanitizeDisplay` on title/labels/descriptions, then every
  embedded `\n` becomes its own physical row (selection bar spans all rows of the selected
  entry; section headers split too). Width math unchanged (truncation stays on sanitized text).
- `ui/src/components/ApprovalCard.ts` — dock title split into one physical row per line
  (request tag on the first row); `first` spread into the dock rows.
- `ui/src/events.ts`, `machina/protocol/*` — **unchanged**: the generated contract already
  carries `outcome?: string` on `tool.complete` (added earlier for `outcome_unknown`);
  `test_protocol_conformance.py` byte-checks the generated file against
  `machina/protocol/events.json`, so hand edits would be a protocol break. The field remains
  additive.
- `machina/core/turn_engine.py:363-364` — **read-only verification** (no diff): it copies
  `event.tool_outcome` into `payload["outcome"]` unchanged; locked by a relay test below.

### Documented mapping choices

| Streamed `outcome` | Rendered state | Rationale |
|---|---|---|
| `denied` | **`denied`** — `⊘` muted + "denied; not executed" | distinct from success and from execution error |
| `paused` | `awaiting_approval` — `○` + "awaiting approval" | an unexecuted needs-approval action must not paint success; the UI already had this neutral state |
| `outcome_unknown` | `outcome_unknown` | unchanged |
| `failed` | `error` | unchanged (previously reached the UI via `isError`; now explicit) |
| `completed` / unknown / absent | legacy `isError ? error : success` | legacy events (no outcome) keep the old contract exactly |

## Proof 1 — backend (RED → GREEN)

Test: `tests/test_r4_022_denied_outcome.py` (5 tests). Drives the real agent tool loop
(`BaseReActAgent._execute_tool_calls_stream`) with a real broker denial (restricted parent
authority → `ApprovalDenied`), the real `TurnEngine` relay, and the real protocol serializer.
No mocks on the fixed path; a pending-chunk buffer avoids DB writes.

- RED (unfixed agent.py): `R4-022_023_red_pytest.txt` — 4 failed / 1 passed; failures are the
  named reason: streamed `tool_outcome=''` (expected `"denied"`/`"paused"`/`"completed"`) and
  relay payload `KeyError: 'outcome'`.
- GREEN: `R4-022_023_green_pytest.txt` — 5 passed.

```
uv run pytest tests/test_r4_022_denied_outcome.py -q -p no:cacheprovider
```

## Proof 2 — UI fake-terminal (RED → GREEN)

Test: `ui/test/r4-honesty.test.ts` (7 tests). Renders through the real `Transcript` and through
the real `TuiAltScreen` on a no-op terminal; payload corpus: OSC title, OSC-52 clipboard
(ESC and C1 forms), clear-screen CSI, `\r`, tabs, U+2028, bidi override, newline-laden
labels. Oracle: strip renderer-owned SGR and the alt-screen's own empty OSC-8 close, then
require zero remaining control bytes in every row (including `\n` inside a row) and pin exact
row counts after newline splitting.

- RED (unfixed UI): `R4-022_023_red_ui.log` — 0 pass / 7 fail: denied card rendered
  `✔ terminal` (success check on "The action was NOT executed"); Menu/Picker rows carried
  `\x1b]0;evil title\x07`; ToolTurn/ApprovalCard rows contained raw `\n`.
- GREEN: `R4-022_023_green_ui.log` — 7 passed.
- Full gate: `cd ui && npm run check` → typecheck ✓, 426 passed / 6 skipped / 0 failed,
  eslint ✓.

```
cd ui && node --test --test-reporter=spec test/r4-honesty.test.ts
cd ui && npm run check
```

## Round-gate checks

- `uv run pytest tests/test_architecture_layers.py tests/test_r4_022_denied_outcome.py \
  tests/test_turn_engine.py tests/test_protocol_conformance.py tests/test_fm8_wiring_honesty.py`
  → 71 passed (agent.py 2055 lines ≤ 2057 ceiling; ratchet untouched).
- Wider regression batch (all green): `test_connectors_execution.py`,
  `test_parallel_tool_execution.py`, `test_s5_guard_coverage.py`, `test_gateway.py`,
  `test_a5_turn_flow.py`, `test_audit_regressions.py`, `test_wiring_parity.py`,
  `test_observability_payload_boundary.py`, `test_integration_permissions.py`,
  `test_rpc_gateway.py`, `test_fm8_s5_broker.py`, `test_connectors_oauth_execution.py`,
  `test_review_round2_fixes.py`, `test_godfile_extraction.py`, `test_trace_hierarchy.py`,
  `test_sse_stream_adversarial.py`.
- `uv run ruff check` + `ruff format --check` on `machina/core/agent.py` and the new test: clean.

## Residual risks / notes

- `paused` rendering intentionally changed from the previous false `✔` to `○ awaiting
  approval` (same honesty bug class); the footer already uses the same vocabulary.
- Menu's item budget still counts items, not physical rows: a label with many embedded
  newlines renders additional rows within the composited screen (no in-band line break, no
  unsanitized bytes). No width-math was changed.
- Unknown/forward `outcome` strings fall back to the legacy `isError` mapping by design.
- The gateway emit is a pass-through (`gateway/server.py:998`); covered by reading plus the
  protocol wire test, not edited (out of scope).
- Evidence toggles: the unfixed runs were captured by temporarily reverting only the two
  `agent.py` outcome expressions and the four UI files (restored immediately; no commit).
