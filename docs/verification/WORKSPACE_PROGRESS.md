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

### T4b — Editor view + pane (status: complete, commit b2bbfe7)

- Files: `ui/src/editor/view.ts`, `ui/src/components/EditorPane.ts`; tests 40 (27 view + 13 pane).
- Highlights: virtualized viewport, gutter/active-line/selection/caret with `CURSOR_MARKER` gated on focus, control-char display projection that never mutates stored bytes, grapheme-safe editing, bracketed paste verbatim, mouse press-capture drag/double-click/wheel, windowed tab strip with `+N` overflow + duplicate-basename disambiguation, status row with text markers.
- Red evidence: two real bugs found by tests (caret rendered while unfocused; C1 control leak through the projection) plus test-expectation corrections recorded by the agent.
- Keybindings: ctrl+pageUp/Down paging (bare page keys alt-screen-owned per T0 ADR §4); `ctrl+shift+k` delete-line is Kitty-only (legacy collapses to ctrl+k; remap note for the app task); ctrl+c/x consumed only with a live selection.
- Deferred: tab-switch keybindings (app command task), workspace-root breadcrumb (one-line wiring later).

### T8-backend — LSP service integration (status: complete, commit 6764c71; DevTools f2e568b)

- Files: `machina/workspace/intelligence.py` (new), `service.py`/`models.py`/`documents.py` (edits); tests 30 through the fake subprocess.
- Invariants encoded: mirror-first; didOpen exactly once per (doc, generation); didChange only against the live generation; flush barrier so requests never overtake sync; save order disk → flush → didSave → fileChanged; diagnostics per-producer replace/version rules/generation discard/1000 cap; crash → stale retained + unavailable results; restart → generation bump + sorted reopen with latest snapshots; bounded per-op deadlines; never-lie states.
- Evidence: 30/30; combined batch 160 passed across 9 suites (orchestrator re-run).

### T7 follow-up fix (status: complete, commit a672776; DevTools 057df89)

- Findings from the real-server suite: (1) tsls suppresses ALL diagnostics unless the client declares `textDocument.publishDiagnostics` — capability added; (2) spurious `cleanup_pending` on stops where wrapper children reap just after the sweep — bounded `group_reap_grace` (2 s default) added while keeping the honest persistent-members path.
- Evidence: 5 new regression tests; transport+lifecycle 47 still green; real-server suite 8/8 with the fix (TS diagnostics now flow without harness help).

### T9-logic — Workspace-edit staging + jump history (status: complete, commit 1147844)

- Files: `ui/src/editor/workspaceEdits.ts`, `ui/src/editor/navigation.ts`; tests 21.
- Semantics pinned: synchronous all-or-nothing apply (no awaits → no partial multi-file mutation); one store transaction per document; same-position inserts preserve protocol array order (fixture-verified); duplicate URI groups merged; version/overlap/read-only refusals; `codeActionsFromPayload` never executes commands.
- Red evidence: module-missing red; one behavior failure (fallback hunk summary) fixed.

### §24-E — Real supported-server integration (status: complete, DevTools 2c0df50)

- Files: `tests/test_lsp_integration_real.py` (8 tests) + `tests/support/lsp_real_workspace.py`.
- Both profiles actually ran (pyright 1.1.414; tsls 6.0.2) via the repo-local pinned binaries; no skips when provisioned; content-level assertions (messages, ranges, URI slicing, severity, versions, generations).
- Proven: unsaved diagnostics (py + ts), completion on unsaved text, definition into related file, references, rename capability (service-level rename pending — T9), save revision + fileChanged, stop/restart with unsaved-text replay, missing-server honesty, no leaked processes.
- Orchestrator re-run after the T7 fix: 8/8 in 4.57 s.
- Gaps reported and dispositioned: tsls capability (fixed a672776), reap grace (fixed a672776), service rename allowlist (pending T9 wiring), didSave not server-observable (harness records forwarding honesty).

### Cross-wave verification (Wave 2, orchestrator)

- Full UI gate with W2 integrated: **691 tests, 685 pass, 6 skip, 0 fail**; typecheck + eslint clean.
- Python combined batch (9 suites incl. real servers): **160 passed** in 13.49 s.
- Commits: main `a672776`, `6764c71`, `b2bbfe7`, `1147844`; DevTools `057df89`, `f2e568b`, `2c0df50`.

### T3-core — Workspace controller + gateway adapter (status: complete, commit cf31830)

- Files: `ui/src/workspace/gateway.ts`, `controller.ts`; 26 tests.
- Highlights: adapter binds workspaceId+epoch per request; controller owns lifecycle, layout intents (mode toggles preserve previous non-chat mode), edit debounce with strictly increasing versions + resync, save handshake, conflict flows, epoch-gated event routing, supersede per (document, method), bounded persistence + reopenRecent ≤8.
- Red evidence: one real bug (editor-maximize toggle no-op) caught by test before fix.
- Full gate at agent run: 770 tests / 764 pass / 6 skip / 0 fail; orchestrator re-ran 26/26.

### T6b — Quick Open (status: complete, commit c2395cb; DevTools 8a0b57a)

- Backend `quickopen.py` (single-flight bounded walk; 20k cap, 10 s deadline, per-dir 5k disclosure, TTL cache, epoch-stale discard) + service methods + handlers (validated) + `workspace.indexProgress` events; UI `quickOpen.ts` overlay (debounced, request-seq stale-drop, honest indexing/truncated states).
- Evidence (orchestrator re-run): 115 combined backend pass; 14/14 UI; ruff clean.

### T9 UI — Editor intelligence (status: complete, commit 2d28a4d)

- Files: `ui/src/editor/intelligence.ts`, `ui/src/components/{IntelligencePopups,ProblemsPane}.ts`, `EditorPane.ts` extension; 39 tests.
- Highlights: CompletionSession (supersede/stale-drop/filter/one-transaction accept/snippet rejection), HoverSession (sanitized 100-line cap), LocationPicker, RenameSession (honest unavailable), ProblemsModel (per-producer latest-wins, 1000 cap with per-entry omission accounting, severity ordering), bounded popups, EditorPane `handleIntelligenceKey` (Enter/Tab consume only with popup; Esc closes popup first).
- Red evidence: per-entry cap-omission counter bug found and pinned.
- Orchestrator re-run: 79/79 across intelligence+problems+pane+view suites.

### Wave-3 in-flight note

- `W3-A2` (app.ts integration: startup + flag, commands, key routing, dirty-quit review, event subscription) is running; consumed controller API and the IntelligenceRequest adapter bridging `IntelligenceOutcome` → `WorkspaceIntelligenceResult` are documented in its task brief.
- `T10` (agent awareness + selection attachment) queued behind A2 (self-knowledge token pins require the TUI commands to exist).

### T3/T6 wiring — App integration (status: complete, commit 4242571)

- Files: `ui/src/app.ts` (+1187), `features/workspace.ts` (new commands), `features/index.ts`, `features/types.ts` (optional `WorkspaceHost`), `commands.ts` (help group), small additive focus callbacks in `editor/view.ts`/`explorer.ts`/`ProblemsPane.ts`; tests: 18 integration tests.
- Delivered: flag-gated background startup on `init.cwd`; persistence via `workspace_ui_state`; shell/editor/explorer/problems wiring; workspace-event subscription; commands `/workspace /editor /explorer /lsp /problems /quickopen`; keys Ctrl+P/S/Space/T/left/right, F12/Shift+F12/F2/F6; popup-owns-Enter/Tab/Esc precedence before the turn-cancel path; save conflict dialog; dirty-exit review (Save all/Discard/Cancel); dispose on exit; graceful fail-closed disable on METHOD_NOT_FOUND or invalid identity.
- Controller fix (orchestrator): open-time persist now MERGES layout intent with stored documents (regression test added; previously a default write could clobber a prior session's open files before `restore()`).
- Red evidence: completion popup mounted without model (fixed); reference fixture half-open RPC guard (fixed); controller clobber identified by probe (fixed with merge).
- Evidence (orchestrator re-run): full gate 788 tests / 782 pass / 6 skip / 0 fail; controller 27/27 after fix.
- Deferred (documented): hover popup mounting, inline diagnostics in the view, divider keyboard resize, keyboard tab switching, editor Ctrl+C selection copy (pre-existing ADR note).

### T12 (partial) — user documentation

- `docs/design/WORKSPACE.md`: layout, commands, keys + fallbacks, language-server setup/trust, save/conflict semantics, agent privacy contract, persistence, troubleshooting, rollout flag.

### T11 evidence — real PTY sessions (plan §24F)

- Harness: `ui/test/support/pty-driver.py` (real pty, controlled winsize, pyte screen snapshots, JSON action scripts) + local PostgreSQL 16 (`machina_test`) providing a live gateway database; runs use a scratch HOME/`MACHINA_HOME`.
- Scenario (160×48, fixture workspace `main.py`/`util.py`/`src/config.ts`), captured frames:
  - `01-boot`: split layout — chat left with composer, divider, editor pane `No open files`; `workspace open: ws-fixture` notice; shared footer.
  - `02-quickopen`: Ctrl+P overlay — query `main`, result `❯ main.py`, footer `1 results · excluded: .git, node_modules, …`.
  - `03-file-open`: Enter opened `main.py` — tab `[main.py]`, breadcrumb, gutter lines 1–4, status `Ln 1, Col 1 · UTF-8 · LF · python`.
  - `04-chat-max`: Ctrl+Right — chat uses the full 160 columns.
  - `05-restored`: Ctrl+Left — split restored with the same tab open (state preserved).
  - `06-explorer`: Ctrl+T — docked right-edge tree (`▸ src`, `main.py`, `README.md`, `util.py`), `workspace [h] hidden off` header, `excluded: .cache · .git ·…` disclosure.
- **Bug found by this evidence (fixed)**: the app fed `initialize.cwd` (a redacted basename) into `workspace.open`, and the backend resolved it relative to its own cwd (`.../ws-fixture/ws-fixture` → refused). Fix: `initialize` now discloses `cwdFull` (absolute) only to token-authenticated handshakes; the UI prefers it, falls back to `init.cwd` only when absolute, and otherwise stays chat-only (older backends). Tests: DevTools `test_initialize_cwd_redaction.py` 4/4 (with live DB); UI integration tests for preference + basename-only fallback (20/20 suite).
- Also verified live in the same run: quick open ranking/disclosure, editor open + status, chat maximize/restore with retained tab, explorer docking with exclusion disclosure.

### Addendum — post-integration batch (commits a672776..9aec38d + DevTools)

- `a672776` T7 fix: tsls `publishDiagnostics` capability + bounded group-reap grace (5 regression tests; real-server suite 8/8 proven after).
- `6764c71` + DevTools `f2e568b` T8 backend integration (30 tests; combined 160 across 9 suites).
- `b2bbfe7` T4b editor view/pane; `1147844` T9 edit staging; `2d28a4d` T9 intelligence UI; `c2395cb`/`8a0b57a` Quick Open; `cf31830` controller; `4242571` app integration + dirty-exit review; `3d4d3a7`/`2ff0f9e` evidence records.
- `c303ef4` user guide (T12 partial).
- Live PTY evidence: split boot, quick open, file open, edit+save to disk, explorer, chat-maximize/restore, dirty-exit review; **found the workspace-root bug** (initialize basename → backend cwd join).
- Root fix: `initialize.cwdFull` disclosed only to token-authenticated handshakes; UI prefers it, basename-only stays chat-only. Tests: DevTools `test_initialize_cwd_redaction.py` (4, live DB); UI preference/fallback tests.
- **LSP composition gap found by PTY completion verification**: production never attached an `LspManager`; `workspace.open` now attaches one (untrusted detaches; reopen replaces bounded). Tests: `test_workspace_lsp_composition.py` (5); architecture ratchet kept at 721 by hoisting the import.
- `820f943` syntax tokenizer (30 tests); `9aec38d` syntax theme seam (5 tests, width-preserving).
- `2241c60` + DevTools `42d036e` `ui_preferences` config key (16 KiB bound) + stale METHODS pin refresh (quickOpen pair).
- `cdb1475` footer workspace-hints slot (4 tests) + ADR §4 full shortcut conflict matrix (composer ctrl+w/ctrl+arrows, macOS Mission Control/input-source, terminal risks with command fallbacks).
- `f050bef` UX polish spec from real-frame audit, incl. the duplicate-Problems render bug (backend returns 1 diagnostic, UI rendered 2).
- CodeRabbit pass 1 launched on the committed range `b0db077..HEAD` (background; findings fold into the closing pass; a second pass covers T10 + fixes + UX batch).

### Closing pass (status: complete, commit 6f3131c)

- Delivered: CodeRabbit leftover fixes (flush-before-intelligence RED→GREEN; Ctrl+←/→ region gating; editor Ctrl+V routing), syntax highlighting wired into EditorView (state-cache strategy documented; caret>selection>syntax), `/settings` 5-category menu + bare `/config` opens it + `/config list` dump, `ui_preferences` store, all polish items 1–7/9/10, footer hints wired, `/editor complete` fallback, 3 new test files.
- Evidence: full gate 892 tests / 886 pass / 6 skip / 0 fail (orchestrator re-run); live PTY final captures recorded in WORKSPACE_UX_POLISH.md (truecolor syntax, settings menu rows, polish surfaces, completion popup, tab close, side swap).
- CodeRabbit pass 2 launched on the final committed range; findings to be triaged into the record.

### CodeRabbit pass 2 (main, full final range) + pass 3 (DevTools)

| Finding | Severity | Disposition |
|---|---|---|
| Syntax cache off-by-one: `>`-only rewind kept stale spans when the edited line equals the highest scanned line | minor | **FIXED** (>= rewind + spans>=line delete; RED-proven: reverting fails the new through==line test) |
| Ctrl+W fired in the chat composer, stealing delete-word-backward | major | **FIXED** (composer-gated; Ctrl+T verified toolkit-free and stays global) |
| Previous manager `stop_all()` could block a reopen indefinitely | minor | **FIXED** (bounded with asyncio.wait_for(MANAGER_STOP_TIMEOUT); old shutdown continues independently) |
| Redaction tests missing an explicit `db` marker | minor | **FIXED** (pytestmark list; correct `-m db` selection) |
| Integration test asserted on a pre-retry record log | minor | **FIXED** (records re-read after the retried document_open) |

- Evidence after fixes: full UI gate 893 tests / 887 pass / 6 skip / 0 fail; DevTools 56 passed across the four affected suites; ruff clean. Commits: main `2c4fb73`; DevTools `a8b4542`.

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
