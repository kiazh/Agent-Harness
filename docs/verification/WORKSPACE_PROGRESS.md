# Integrated Coding Workspace — Implementation Progress Record

Authority: `Machina_Integrated_Editor_LSP_Implementation_Plan.md` (T0–T12, §26 record format).
This document keeps current evidence separate from historical baseline logs. Never write
"verified" because a test was added but not run; never describe a fake terminal as a real PTY.

## Baseline (recorded at T0 start)

- Baseline commit: `b0db077` — "Fix CLI edge cases and restore regression suite compatibility"; working tree clean at recording.
- Runtime: Node v26.10.0, Python 3.14.5 (venv at `.venv`), platform darwin (macOS).
- UI checks (before any change):
  - `npm run typecheck` → exit 0 (clean).
  - `npm test` → 507 tests, 501 pass, 0 fail, 6 skipped, 0 todo.
  - `npm run lint` → exit 0 (clean).
- Language servers on PATH: none found (`pyright-langserver`, `typescript-language-server`, `tsc` absent from PATH).
  - Local `ui/node_modules/.bin` ships `tsc` and `tsserver` (TypeScript ~5.8 devDependency) but neither is an LSP stdio server.
  - Integration provisioning is therefore an explicit setup step; missing-server behavior must be honest.
- Dirty paths: none at baseline.

## T0 — Baseline and capability spike

- Status: complete (contracts frozen; see below).
- Deliverables:
  - ADR: `docs/design/WORKSPACE_ADR.md` (decision: build one workbench editor adapter; keep toolkit; reuse layout/scroll/mouse/cursor infrastructure).
  - Source map: ADR §5 (UI + Python seams with line anchors, verified by four parallel recon agents).
  - Keyboard collision inventory: ADR §4 (alt-screen consumes pageUp/pageDown/home/end/ctrl+shift+f before the focused component; App.onKey intercepts listed; plan bindings mapped to owners).
  - Frozen contracts (code):
    - `ui/src/workspace/state.ts` — WorkspaceMode/FocusRegion/Rect/LayoutInput/LayoutResult, budgets, persisted-state shape.
    - `ui/src/workspace/layout.ts` — pure `computeLayout`, `clampSplitRatio`, `adjustSplitRatio`, `explorerColumnBudget`, `dividerRect`; `SPLIT_MINIMUM_COLUMNS=89` derived.
    - `ui/src/design/responseLayout.ts` — `DEFAULT_RESPONSE_POLICY`, `assistantFrameBudget`, `chooseResponseGeometry` (compact→medium→full, monotonic class, content-fit compact), `renderAssistantFrame`.
  - Evidence: `ui/test/workspace-layout.test.ts` 12/12 pass; typecheck + lint clean with new modules.
- Key verified facts:
  - Two-pane alt-screen layout, per-pane ScrollView, hardware cursor marker, local-origin mouse with capture, and state-preserving resize are all supported by pi-tui 1.0.0 (source-level evidence in ADR §2).
  - pi-tui `Editor` is prompt-grade only (no selection/redo, 30% height cap) → custom editor adapter required by plan §3.
  - Gateway RPC is newline-JSON; LSP framing is a separate Content-Length implementation.
  - Workspace events must not enter the chat event schema (no fabricated turn IDs): separate hand-written family.
  - Python tests run from `/Users/shadow/Machina-DevTools` (symlinked `machina/` + `ui/`).

## Wave plan (already-frozen interfaces are authoritative)

- W1: T1 adaptive layout integration/tests; T2 shell composition; T4a editor model; T5 workspace file service; T7 LSP process infrastructure.
- W2: T3 command/focus router; T4b editor view; T8 document sync + core intelligence; T6 tabs/explorer/persistence.
- W3: T9 references/symbols/workspace edits; T10 agent awareness + sharing.
- W4: T11 sweep, T12 docs (or recorded as blocked with exact reasons).

## Task records

### T1 — Adaptive response layout (status: complete, commit 064a81d)

- Baseline commit / dirty paths: dd6e681 → work landed on top of d4ebfab; files below.
- Requirement and invariant: content-aware width, natural height, monotonic stream growth, full usable chat width in chat mode, never overwide rows (plan §6).
- Exact files changed: `ui/src/components/AssistantTurn.ts`, `ui/src/components/ToolPanel.ts`, `ui/src/design/responseLayout.ts`, `ui/test/response-layout.test.ts`.
- Public interfaces: `AssistantTurn.responseGeometry(paneWidth)`; `contentPrefersWide(source)`; `assistantPanelContentWidth` retained as a delegating legacy seam (no fixed cap).
- Why reused/extended: policy lives at the shared design seam the plan names; the pi-tui Markdown renderer is the only measurement source; tool/approval frame budgets untouched.
- Tests added and what they assert: 13 tests — compact content-fit, full-width long replies, width sweep 0..240 + near-threshold widths, streaming monotonicity + exact final text, pinned-class guard, fence/table preference, 240-pane no stale cap, 160↔80 resize, cache identity, rows-not-bytes, legacy seam, re-skin, hostile ANSI.
- Red evidence: first run 1 pass / 9 fail (contract violations: missing responseGeometry; oscillation case).
- Green: `npm run check` exit 0 — 536 tests, 530 pass, 6 skipped, 0 fail (isolated worktree at d4ebfab + T1 files); shared-tree failures at that moment were other in-flight tasks' unfinished files.
- Counterexamples probed: OSC-8 hyperlink + `ESC[2J` inert; 220-char unbroken token wraps, not clamped; empty content at frameless width emits only the block gap.
- Unverified: policy defaults (56/0.72/4/12) untuned by captures — tuning remains a config concern.
- Security/privacy: no change to sanitization order (assembled-content sanitize retained for measurement and render).
- Next: T4/T2 integration (pane width now flows from computed geometry).

### T2 — Pure workspace geometry + shell composition (status: complete, commit a7e23a5)

- Baseline/dirty: on top of 064a81d; files below.
- Requirement/invariant: single geometry source (`computeLayout`); split ≈50/50 at 160 cols; narrow fallback preserves request and restores; retained component identity; footer shared; divider drag clamps; no conflicting independent split (plan §7, §2).
- Files: `ui/src/components/WorkspaceShell.ts` (new: shell + divider + geometry compare), `ui/src/components/AppShell.ts` (+114/−5), `ui/test/workspace-shell.test.ts` (new).
- Interfaces: `AppShell.setWorkspaceState/setWorkspaceEditor/setWorkspaceExplorer`, `onWorkspaceRatioChange`; `WorkspaceShell.entries(result)` fixed-basis columns; `sameLayoutGeometry`.
- Reuse rationale: existing `viewportRoot` `[LAYOUT_NODE]` wrapper + rebuild flow retained; chat-only tree byte-identical when unconfigured; ScrollView/composer/dock instances move, never recreate.
- Tests: 12 new — 160×48 split geometry, 88→160 restore with editor identity counter, ScrollView identity across modes/densities, divider drag mapping + clamps, docked explorer allocation, degenerate terminals.
- Green: isolated worktree at 064a81d + T2 files → `npm run check` exit 0, 548 tests / 542 pass / 6 skip / 0 fail; combined shell+layout+geometry suites 37/37.
- Counterexamples: drag x=5000 clamps to 0.75 and minima hold (chat 111/editor 48); drag x=0 → chat 40; drag without press/right-button ignored; 0×0..20×6 terminals no throw; no render per move (coalesced).
- Known nuance: frozen geometry rounds `0.5×159 → 80` chat columns (editor 79); tests derive positions from computeLayout rather than hardcoding 79/80. Divider ratio formula uses `screenX/(total−1)` and relies on geometry minima when an explorer is docked (acceptable; revisit if drag feels off with docked explorer).
- Unverified: no PTY capture yet (deferred to T11 native sweep); keyboard divider resize explicitly deferred to T3/T4.
- Security/privacy: none new; divider claims only left-press with capture; no focus steal.

### T4a — Editor document model (status: complete, commit 021d671)

- Files: `ui/src/editor/{model,positions,tabs}.ts`; tests `ui/test/editor-{model,positions,tabs}.test.ts` (54 tests).
- Interfaces: frozen `DocumentRecord`/`EditorViewState`; `beginSave`/`acknowledgeSave` (dirty clears only on version+hash match; out-of-order acks guarded); global undo budget with oldest-whole-group trim; total open-text budget at open only; `serializeForSave` byte-exact.
- Red evidence: module-missing red for all three suites; then 7 behavior failures caught (URI dedup, byte accounting, newline label bug) before fixes.
- Green: 54/54; full UI gate at that point 602 tests (596/6/0).
- Ambiguities resolved and documented in code: undo budget global across docs (UTF-16 units), overlap rules at range boundaries, `positionToOffset` CRLF clamping, dedup key = exact URI (canonicalization delegated to T5).
- Unverified: no view-layer integration yet (T4b).

### T5 — Workspace file service (status: complete, commit 00763f3; DevTools e02f686)

- Files: `machina/workspace/{__init__,models,files,documents,service}.py`, `machina/gateway/features/workspace.py`, additive `features/__init__.py`, `communications/ui-gateway-protocol.md`, TS refusal fields in `ui/src/workspace/protocol.ts`.
- Security evidence: actor spoofing ignored; server-minted ids/epochs; traversal + symlink escape (file and parent) refused; TOCTOU conflict under per-path lock; hardlink/read-only refusals; atomic writes preserve original on failure; fallback path for no-`dir_fd` platforms explicitly weaker (documented).
- Green: 54 DevTools tests (38 service + 16 gateway) + supporting suites; ruff clean. Independently re-run by orchestrator: 109 total with LSP suites.
- Unverified: Windows behavior (monkeypatched seams only); cursor stability under concurrent directory mutation documented.

### T7 — LSP process infrastructure (status: complete, commit 9427928; DevTools e02f686)

- Files: `machina/workspace/lsp.py`; `tests/support/fake_lsp_server.py`, `tests/test_lsp_transport.py` (22), `tests/test_lsp_lifecycle.py` (24).
- Guarantees: ready only after successful initialize + flushed initialized; ordered write queue (requests cannot overtake document sync); cancelled-request late responses dropped without leaks; server-initiated requests always answered structurally; bounded header/body/pending/stderr; process-group teardown with honest `cleanup_pending`.
- Real smoke (manual, recorded): repo-local `pyright-langserver` handshake → ready (`textDocumentSync: 2`, `hoverProvider`), stop rc 0. Full real-server matrix remains W2-E (§24E).
- Unverified: Windows kill path; `setsid`-escaping descendants are outside group-kill scope (disclosed).

### Cross-wave verification (orchestrator, independent of agent claims)

- UI committed state @ 9427928 re-run in an isolated worktree (`/private/tmp/opencode/machina-verify`, node_modules symlinked): `npm run check` exit 0 → 602 tests, 596 pass, 6 skip, 0 fail; eslint clean.
- Python committed state: `run-tests.sh tests/test_workspace_service.py tests/test_workspace_gateway.py tests/test_lsp_transport.py tests/test_lsp_lifecycle.py tests/test_features_split.py` → 109 passed in 5.03s (includes pre-existing `test_gateway.py` DB-dependent failures reproduced at baseline and excluded here).
- Note: DevTools' `machina` symlink shadows `PYTHONPATH`, so Python runs always exercise the live checkout — mid-wave runs are only meaningful when the relevant agents are idle.
- CI-gate observation (pre-existing, NOT ours): `ruff format --check machina/ tools/` reports 13 unformatted files at baseline (`api/routes/streaming.py`, `core/provider/ollama.py`, `core/turn_engine.py`, `gateway/__main__.py`, `gateway/features/secrets.py`, `memory/store.py`, `permissions/{broker,policy,store}.py`, `rag/pipeline.py`, `security/{env_file,providers}.py`, `tools/file.py`). None are touched by this feature; all files we added/modified are format-clean. Left untouched deliberately (no unrelated audit backlog smuggling); flagged for the maintainers.
- Bandit sweep on new modules: 0 medium/high issues (CI uses `-ll`).

### T6a — Lazy explorer + persistence (status: complete, commit 5c97c9e; DevTools ee7ac3a)

- Files: `ui/src/editor/explorer.ts` (ExplorerModel + ExplorerPane), `ui/src/workspace/persistence.ts`, `machina/core/config.py` (additive `workspace_ui_state` default, 64 KiB bound in `validate_value` — single refusal shared by all coercion paths per AH-AUDIT-042), tests: 28 UI + 7 DevTools.
- Evidence (orchestrator re-run): explorer+persistence 28/28; backend 7/7; agent's isolated worktree full gate 630 tests / 624 pass / 6 skip / 0 fail.
- Red evidence (agent): 3 explorer failures fixed (collapse-vs-parent, selection reconcile during reload, test regex); backend red run against baseline config (6 failed) → green with the change.
- Honest states: `…`/`(empty)`/`(excluded)`/`(denied)`/`(error)` distinct; `+N more` (exact loaded-row budget) vs backend `load more` never confuse an invented count.
- Deferred: Quick Open recursive backend service; tab auto-restore; controller wiring (all tracked for W3).

## Task record template (from plan §26)

```text
Task ID and status:
Baseline commit / dirty paths:
Requirement and invariant:
Exact files changed:
Public interfaces or schema changes:
Why existing abstractions were reused or extended:
Tests added and what they assert:
Red evidence (where applicable):
Green command / runtime / exit code / pass-fail-skip counts:
Native or real-server evidence path:
False-positive/counterexample checks:
Unverified assumptions and environment limitations:
Security/privacy implications:
Next task and first action:
```
