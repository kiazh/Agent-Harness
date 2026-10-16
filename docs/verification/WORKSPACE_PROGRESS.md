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
