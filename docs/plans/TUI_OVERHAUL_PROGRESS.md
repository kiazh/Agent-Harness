# TUI Overhaul Progress

Spec: `/Users/shadow/Desktop/TUI_FULL_OVERHAUL_INSTRUCTIONS.md` (1051 lines, prepared 2026-10-11, HEAD 0ea164e).
This file is the durable continuation record per spec section 1.5. Update after each task.

## Screenshot correction — 2026-10-11

The user supplied a new visual authority after the OpenCode overhaul: plain
chat, neutral framed tools, transcript action evidence and vertical bottom
approval controls, one footer, quiet blue codex palette, and slower small
activity. This overrides older borderless-tool and two-row-footer contracts.
Implementation and regressions are in the checkout, uncommitted. No backend
approval policy was weakened to achieve the reference layout.

- Single-row header/footer; tools indented/capped at 104 columns; reading measure
  bounded for assistant messages; thin filled composer and visible small-window
  placeholder. Saved themes remain respected.
- Activity uses one cell/200 ms. The corner square pulses in brightness while
  working and for an 800 ms learning acknowledgment, coalesced at 30 s;
  acknowledgments render immediately while idle. Cleanup is static;
  ownership remains held until terminal retirement.
- Approval evidence belongs to the active dock request. Queued requests activate
  in order; overflowed operation/target is fully pageable. Late forms/pickers
  retain draft and wait behind approval. External resolution, terminal completion,
  cancellation, switching and exit cannot silently approve a request.
- Fresh bound continuation IDs are accepted before dispatch filtering and retired
  origin events remain inert. This is presentation compatibility; TUI HTTP transport
  and OAuth were not added. Watchdog replacement/exit clears resources.
- `/mcp`, help and autocomplete call read-only `mcp.list` / `mcp.status`. A lazy
  worker-owned ConnectorService supplies true inventory/state; no SDK, database,
  connect or discovery is required to inspect. Real SDK fixtures verify connected,
  disconnected and owned/borrowed cleanup. Inventory setup/persistence and connect
  UI remain deferred, so an empty fresh runtime reports no registered servers.

See `docs/verification/TUI_OVERHAUL_REVIEW.md` for final commands and counts;
old task records below are historical and their visual contracts/counts are not
current. The native capture fixture is reproducible with
`node ui/test/support/capture-reference.ts /tmp/ah-reference-review`.

## Output frame and motion follow-up — 2026-10-11

User correction: output rectangles need visible margins and a different fill;
activity felt too slow and the corner square was not pulsating. The local launcher
was checked read-only and points at this checkout. Native frames existed, but
contrast was insufficient. Codex now has #606069 borders and #17171c surfaces;
actual emitted truecolor and indexed colors pass rectangle/fill contrast tests.

Activity cadence is 200 ms. The square keeps its one-cell geometry while brightness
rises/falls. Working footer demand uses the existing shared coordinator even after
text deltas hide thinking. Approval, ready, stopping, cleanup, disconnect, switching
and exit retire working demand. Learning uses an 800 ms pulse, a 30 s cooldown and
an immediate first render; reduced motion stays static without a recurring timer.
Seven assertions were observed failing before fixes. Node motion/panel checks,
full UI check and isolated real-gateway wrapper pass; see current verification doc.
The full backend run above remains the prior verified run (UI/docs-only follow-up).
Independent bounded review found no Critical/Important issue. Changes uncommitted.

## T0 — Reconnaissance and baseline — COMPLETE 2026-10-11

Working directory: `/Users/shadow/agent-harness1` (spec says `/Users/shadow/Agent-Harness1`; macOS case-insensitive, same checkout).
HEAD: `0ea164e animatuin changes with growth`
Log -3:
- 0ea164e animatuin changes with growth
- 592fc67 rsi implementation and mcp servers
- dc77017 profile
Branch: `main`
Status: clean (`git status --short` empty at T0 start).
Node: `v26.10.0` (requires >=22.19.0 — OK)
Python: `.venv/bin/python 3.14.5` (use `.venv/bin/python -m ...`; pip shebang stale per spec).
AGENTS.md: none found at repo root (glob AGENTS.md = no files). Follow spec section 1.2 authorization.

Runtime: TypeScript ESM, `@earendil-works/pi-tui 1.0.0` pinned, TS ~5.8, ESLint 8, native node:test.
Test DB authorized: `postgresql://postgres:postgres@localhost:5432/agentharness_test` via `AGENT_HARNESS_TEST_DATABASE_URL`. DB sequential only.

Inventory (from source, T0):
- Protocol: `ah/protocol/events.json` version 2. `ui/src/events.ts` generated, do not hand-edit. TUI initialize sends `protocolVersion: 2` (ui/src/app.ts:147). Canonical envelope requires protocolVersion/type/sessionId/turnId. v1 aliases `permission.required/resolved` accepted at compatibility boundary.
- Commands (37 unique from ui/src/features/*.ts + index.ts): agents, approvals, clear, compress, config, context, default-model, delegate, delete, effort, exit, export, fork, goal, growth, help, init, jobs, keys, memory, mode, models, new, persona, profile, profiles, provider, recall, rename, resume, review, search, sessions, skills, status, theme, usage. Aliases (ui/src/commands.ts): quit/q->exit, h/?->help, ls->sessions, mem->memory, skill->skills, settings/set->config, compact->compress, model->default-model. Groups in HELP_GROUPS: Session/Task/Memory/Context/Automation/Setup/Info.
- Theme IDs actual (ui/src/theme.ts SKINS keys, 24): default, tokyonight, catppuccin, dracula, gruvbox, rosepine, nord, everforest, matrix, codex, catppuccin-frappe, catppuccin-macchiato, cobalt2, ayu, nightowl, one-dark, flexoki, mono, onedark, monokai, github, solarized, kanagawa, palenight. Spec section 6.1 lists 21 and says add opencode+light; actual has 24 (extras: catppuccin-frappe, catppuccin-macchiato, one-dark + onedark duplicate). T8 must verify against SKIN_NAMES, keep all saved IDs, add opencode+light, keep explicit default, fresh default -> codex (currently DEFAULTS['theme']='default', Config.theme='default').
- UI files: ui/src/{app,transcript,components,theme,commands,autocomplete,protocol,events,gateway,format,main}.ts + features/{agents,codex,context,growth,index,jobs,keys,memory,mode,models,sessions,settings,skills,theme,types}.ts. Tests: app, ui, dialogs, layout, features, autocomplete, channel, gateway.e2e.
- Config default theme: `ah/core/config.py` DEFAULTS['theme']='default', Config.theme='default'. T8 owns change to codex (both Python defaults + UI fallback) without overwriting persisted user values.

Baseline checks (T0, no production changes):
- `npm --prefix ui run check` (typecheck + native tests + lint): PASS. Tests 152 total, 147 pass / 0 fail / 5 skipped. The 5 skips are environment-gated real-gateway tests (AH_TEST_PYTHON / AGENT_HARNESS_TEST_DATABASE_URL not set in standalone run): persona menu persists default, personality survives restart, talks to real gateway, readline close, slash commands vs real gateway+DB.
- `git diff --check`: clean exit 0.
- Isolated real gateway: `AGENT_HARNESS_TEST_DATABASE_URL=... .venv/bin/python -m pytest -q tests/test_personality_growth.py::test_ui_checks_with_real_gateway_restart --tb=short -p no:cacheprovider` => 1 passed in 9.14s. Wrapper runs Node with skipped 0 inside (to be confirmed per-run output in later tasks).
- BASE_HEAD for section 9.3 comparisons: `0ea164e`.

Recon read order (spec 3.1) started: package.json, tsconfig, main.ts, theme.ts, events.json, app.ts (1-150), transcript.ts (1-120), commands.ts, features/index.ts, config.py (1-80), ui-gateway-protocol.md (1-60), HANDOFF.md (1-60). Remaining to read before T1-T3 edits: full app.ts/transcript.ts/components.ts, features/types+settings/growth/keys/theme/mode, autocomplete/protocol/events/gateway, all ui tests, gateway server.py + features/mode.py + features/config.py, tests/test_personality_growth.py, PERSONALITY_GROWTH/MCP progress docs, pi-tui .d.ts capabilities.

Known risk paths (spec 3.3, targets for red tests): approval missing sessionId + pre-filter picker; complete marks unresolved tools success; 300s watchdog clears guard without ownership release + weak timer test; session-only routing needs stale turn rejection; tool.complete.truncated not preserved; replay infers failure from "Error:" prefix; /keys prints instructions, no secret field/masking; Picker/SelectList filter confusion (prefix-only, no real search field); theme save always claims success; single overlay row budget insufficient on resize.

## Task checkboxes

- [x] T0 Reconnaissance and baseline
- [x] T1 Extract semantic design system without breaking callers
- [x] T2 One motion coordinator and quiet learning migration
- [x] T3 Honest session/turn-aware event model
- [x] T4 Compact transcript, previews and retained output inspection
- [x] T5 Responsive shell, composer and footer
- [x] T6 Searchable menus, durable forms and masked keys
- [x] T7 Exact approval review and cross-session safety
- [x] T8 Complete theme switching, preview and default persistence
- [x] T9 Integrated regression sweep and documentation
- [x] T10 Bounded visual/native verification and finish

## Continuation record (section 1.5 — update after each task)

### T10 complete 2026-10-11 — OVERHAUL COMPLETE

Task ID and status: T10 complete. All required boxes satisfied (see evidence below).
Behavior delivered: Final fixes from the visual pass (pluralized preview counts, assistant marker column, tiered footer priority) + verification review doc.
Files changed: ui/src/components/{ToolTurn,DiffBlock,AssistantTurn,Footer}.ts (fix batch); ui/test/{ui,motion}.test.ts (3 regressions); docs/verification/TUI_OVERHAUL_REVIEW.md (new).
Verification:
- Matrix script (temporary, removed after pass): 3 themes x 3 sizes x 7 states = 63 native-renderer screens; invariants (width, no control forgery, no secret leak) all clean after the fix batch; 36 stripped captures inspected once in /tmp/tui-matrix (uncommitted).
- After last production edit: focused suites green, full `npm run check` exit 0 (233/228/5/0 + lint), isolated gateway wrapper 1 passed. Backend suite green from T9 (2093/5 on rerun); rendering-only T10 edits do not change backend risk.
- No real PTY review; labeled honestly. No commits/pushes; diff whitespace-clean (30 changed/new paths).
Completion checklist (10.1): normal TUI entry unchanged in shape (main.ts -> App -> shell); single design/theme/coordinator systems with facades only; codex fresh default + all saved IDs valid; compact borderless transcript with Markdown/whitespace fidelity; one coordinator timer, fixed-width markers, reduced motion, quiet square; honest tool/final/usage states; docked chrome at 40x9/80x24/120x40 with safe degradation; live resize with state; ?/F1/editing/paste/completion preserved; searchable menus; persona/growth/profile/settings/session/model on real APIs; masked keys with no leaks; once-only saves with draft preservation; exact Deny-default session-bound approvals; /output bounded inspection; all themes readable with preview honesty; offline/cleanup/timeout guards honest; checks recorded; docs delivered.

### T9 record (historical)

Task ID and status: T9 complete. T10 in progress.
Behavior delivered: Full backend suite green on rerun (2093/5; first run had 2 load-timing delegation flakes, green in isolation + rerun), README/design/handoff docs updated.
Files changed: README.md (TUI section, keys flow, 38 commands, 26 skins, counts); docs/design/TUI_DESIGN.md (new contract); docs/HANDOFF.md (dated entry); tests/test_configuration_precedence.py (default test).
Verification:
- `npm --prefix ui run check` => 233 tests / 228 pass / 5 env-skipped / 0 fail, lint clean (before T10 matrix).
- Isolated gateway wrapper => 1 passed; direct e2e (absolute python) => 5/0 skipped incl. /theme round-trip.
- Full backend sequential ×2: (a) 2091 pass + 2 failed (delegation TimeoutError under load) + 5 skipped; (b) 2093 pass + 5 skipped. Isolated file run: 15 pass. Labeled timing flakes with evidence; no worktree comparison needed (theme-default literals cannot affect delegation; rerun green).
- Ruff check/format clean on ah/core/config.py + precedence test. `git diff --check` clean. No commits.
Next task and first action: T10 — batched native-renderer screen matrix (40x9/80x24/120x40 x codex/mono/light x all major states) via a temp script; inspect once; batch fixes; docs/verification/TUI_OVERHAUL_REVIEW.md; final report.

### T8 record (historical)

Task ID and status: T8 complete. T9 next.
Behavior delivered: Live reversible theme preview, truthful once-only persistence, codex fresh default across Python/UI, readability contrasts.
Files changed:
- Modified: ui/src/components/Menu.ts (initialValue + onSelectionChange notifications).
- Modified: ui/src/app.ts (pickTheme with live preview/restore/commit); ui/src/features/theme.ts (pickTheme flow, persist-once, temporary-on-failure warning).
- Modified: ui/src/features/types.ts (pickTheme); ui/src/design/theme.ts (initial codex).
- Modified: ah/core/config.py (DEFAULTS + Config theme -> codex; explicit saved values untouched).
- Tests: features.test.ts (H02 x3 + FakeHost.pickTheme), design.test.ts (H01 readability), dialogs.test.ts (H02 preview/cancel, H03 mid-stream), gateway.e2e.test.ts (/theme persist round-trip), tests/test_configuration_precedence.py (fresh-default test).
Public interfaces added/changed: FeatureHost.pickTheme(); MenuOptions.initialValue; Menu.onSelectionChange.
Tests added: H01 semantic/readability contrasts, H02 commit/cancel/save-failure, H03 mid-stream preservation.
Red command + failure that proved the defect: none needed beyond new-contract tests (old direct-apply path kept working; new preview/persistence paths failed before implementation).
Green command + result:
- `npm --prefix ui run check` => typecheck pass, 233 tests / 228 pass / 5 env-skipped / 0 fail, lint pass.
- Direct e2e with absolute AH_TEST_PYTHON => 5 pass / 0 skipped (incl. /theme dracula persist + reread).
- Isolated wrapper => 1 passed. Python precedence + atomicity suites => 12 passed. Ruff check/format clean on changed Python files.
- `git diff --check` => clean.
Unresolved failures and reproduction: none. Direct e2e with a relative AH_TEST_PYTHON fails to spawn (environmental; wrapper/absolute path is the supported route).
Notes: theme switches need no animation-subscription rebuild (lazy resolution; proven by stable timerCount in H03). No backend changes beyond the two default literals; no test pins the old default.
Next task and first action: T9 — full standalone UI check, isolated gateway wrapper, full backend suite sequentially on the test DB, README + docs/design/TUI_DESIGN.md + HANDOFF entry, ruff on changed Python.

### T7 record (historical)

Task ID and status: T7 complete. T8 next.
Behavior delivered: Dedicated bordered ApprovalCard with exact sanitized evidence, Deny-first keyboard flow, immutable binding capture with pre-dispatch revalidation, sessionId included in approvals.resolve, invalidation without RPC on switch/disconnect/exit.
Files changed:
- New: ui/src/components/ApprovalCard.ts (bounded bordered review, evidence window with pgup/pgdn, allow-block on clipped terminals, resolving/error states); ui/test/approval.test.ts (7 card tests).
- Modified: ui/src/app.ts (approvalDialogs map, card-based promptApproval, resolveApproval with binding revalidation + single dispatch, invalidateApprovals wired to switch/update/disconnect); components.ts (ApprovalCard re-export).
- Tests: state.test.ts (A03 x3, A04 x3); dialogs.test.ts (I03 approval-over-form).
Public interfaces added/changed:
- ApprovalDetails/ApprovalDecision/ApprovalCardOptions/ApprovalCard; components.ts re-exports.
- approvals.resolve now sends {requestId, sessionId, verdict, grant} (sessionId validated by gateway mode.py).
Tests added: A01 evidence/boundaries/timeout/scroll, A02 default-deny/deliberate-allow/dup-Enter/clipped, A05 control-sequence safety, A03 switch/late/disconnect variants, A04 bound resolve/failure-retry/resolved-distinction, I03 nested deny + draft restore.
Red command + failure that proved the defect:
- Card tests initially failed on windowed evidence expectations and clipped-hint truncation (calibrated: scroll assertions + shortened block notice).
- I03 dialog test exposed a real race: entry.close stored after map insertion never ran (create() looks the entry up synchronously) -> fixed by pre-inserting the entry.
Green command + result:
- `npm --prefix ui run check` => typecheck pass, 227 tests / 222 pass / 5 env-skipped / 0 fail, lint pass.
- `git diff --check` => clean.
Unresolved failures and reproduction: none.
Notes: no gateway changes (existing sessionId validation reused; backend approval suites run in T9). Returning to a switched-away session does not auto-reopen the card (no gateway re-emission source); the pending request stays server-side until turn timeout — documented limitation. approval.resumed is HTTP-only today; the rebind path stays defensive.
Next task and first action: T8 — pickTheme() live preview via Menu callbacks, truthful config.set persistence, lazy-widget verification, codex fresh defaults in Python + UI fallback, H01-H03 tests.

### T6 record (historical)

Task ID and status: T6 complete. T7 next.
Behavior delivered: Real filter-owning Menu (Picker delegates to it), TextForm migrated with once-only saves, masked SecretInput/SecretForm behind AskTextOptions.secret, /keys rebuilt as Set/Replace/Clear/Back on existing secrets.* RPCs with redaction, live menu budgets, Enter-while-locked draft protection.
Files changed:
- New: ui/src/components/{Menu,Form}.ts; SecretInput/SecretForm inside Form.ts.
- Modified: ui/src/components.ts (Picker over Menu; TextForm block moved out; Form/Menu re-exports).
- Modified: ui/src/app.ts (askText secret branch; pick computes live budget + passes tui; shortcuts picker passes tui).
- Modified: ui/src/features/{types,keys}.ts (secret flag; menu flow with redactSecret; expert forms preserved).
- Tests: features.test.ts (4 keys flows incl. sentinel absence + redaction); dialogs.test.ts (I05 x2, I06 mask/save/cancel, I07 lock/unlock, V07 live resize); state.test.ts (I06 history exclusion, draft restore kept).
Public interfaces added/changed:
- Menu(title, items, {hint, visibleRows, tui?}) with filter/selection/budget ownership; Picker gains trailing tui?.
- AskTextOptions.secret; SecretFormOptions; SecretInput (masked Input subclass).
- keys.ts exports redactSecret.
Tests added: I03 covered by pre-existing interrupting-picker test (unchanged behavior); I04 preserved by persona suites; I05 filter/backspace/arrows/empty/Esc/budget/markers; I06 mask/edit/paste/save-once/blank/cancel + history exclusion + notice/menu absence; I07 double-save lock + error unlock; V07 resized pick + form.
Red command + failure that proved the defect:
- Old /keys instruction test failed against the real flow (replaced with Set/Clear/Back/redaction tests).
- Probe found Editor submit-and-clear wiping drafts on Enter-while-locked -> TextForm swallows Enter while locked.
- Overlay-clipped picker hint at 9 rows -> live Menu budgets via tui + App.pick budget.
Green command + result:
- `npm --prefix ui run check` => typecheck pass, 213 tests / 208 pass / 5 env-skipped / 0 fail, lint pass.
- `git diff --check` => clean. No SelectList filtering remains in src (theme only).
Unresolved failures and reproduction: none.
Notes: SelectList untouched for editor autocomplete; mouse on menus not implemented (keyboard-first per 6.9); undo stack may hold secret copies (documented, no erasure claimed); expert /keys clear stays immediate (no established confirm); approval-flavored nesting test belongs to T7.
Next task and first action: T7 — ApprovalCard widget with exact argv/digest/evidence + Deny-default keyboard flow; immutable binding capture; approvals.resolve WITH sessionId; ui/test/approval.test.ts (A01-A05 + S01/I03 variants).

### T5 record (historical)

Task ID and status: T5 complete. T6 next.
Behavior delivered: Production AppShell with retained ScrollView and density budgets (full/reduced/compact/degraded), cursor-safe bounded composer, responsive footer (compact row, working dots, small-width priority), scroll-anchor follow semantics, draft restore on submit failure.
Files changed:
- New: ui/src/components/AppShell.ts (retained transcriptScroll, densityFor, sync/rebuild preserving scroll identity, composer/footer budgets).
- Modified: ui/src/components.ts (ComposerBox.setMaxRows/setDegradedMode, windowAroundCursor, placeholder budgets, final-width clamps; Picker width clamp; createLayout kept as static compat).
- Modified: ui/src/components/Footer.ts (compact flag, working activity dots, tiered small-width priority status/model/tokens before cwd/branch, square docked per mode).
- Modified: ui/src/app.ts (shell owns layout root; sync on start/input/events; submit restores draft with pre-acceptance vs unknown-dispatch notices).
- Modified: ui/src/components/{UserTurn,AssistantTurn,ToolTurn,Notice,OutputViewer}.ts (final-width truncation for V05 incl. 0/1/2).
- Tests: layout.test.ts (AppShell boot, V04/V05 loop, V06 sizes, live resize, V08 anchor, degraded, composer cap); state.test.ts (draft restore).
Public interfaces added/changed:
- AppShell(densityFor/Density/ComposerBudget/root/transcriptScroll/currentDensity/sync/dispose); ComposerBox budget setters; Footer.compact; windowAroundCursor exported.
Tests added: V04/V05 widths loop, V06 docked sizes, density live-resize, V08 follow-freeze, degraded safety, composer cap/cursor, draft restore. I01/I02 preserved by existing dialogs/autocomplete tests (all passing).
Red command + failure that proved the defect:
- Composer grew to 14 rows on a 20-line paste (unbounded) -> now capped with cursor window.
- V04/V05 loop exposed width-0 overflows in Notice/OutputViewer/Picker (min-1 clamps) -> final-width truncation everywhere.
Green command + result:
- `npm --prefix ui run check` => typecheck pass, 204 tests / 199 pass / 5 env-skipped / 0 fail, lint pass.
- `git diff --check` => clean.
Unresolved failures and reproduction: none.
Notes: overlays never trigger shell sync without interaction (toolkit has no resize subscription); dialogs read live rows themselves. Footer working dots animate off existing turn activity (no extra subscription). createLayout remains as a static-compat export.
Next task and first action: T6 — Menu/Form widgets with real filter ownership + AskTextOptions.secret masked editor; rebuild /keys Set/Replace/Clear/Back on existing secrets.* RPCs; I03-I07 + V07 menu/form resize tests.

### T4 record (historical)

Task ID and status: T4 complete. T5 next.
Behavior delivered: Borderless compact transcript (dim `>` user, `●` Markdown assistant, marker + name tool turns with 4-row previews), honest outcomes in the 5.3 vocabulary, bounded retained-output store (8 x 128 KiB / 1 MiB) with codepoint-safe caps, scrollable OutputViewer overlay, working /output command + inspectOutput on real App dialogs.
Files changed:
- New: ui/src/components/{UserTurn,AssistantTurn,ToolTurn,DiffBlock,Notice,OutputViewer}.ts; ui/src/features/output.ts; ui/test/output.test.ts (7 O01 tests).
- Rewritten: ui/src/components.ts transcript section -> facade re-exports (new widgets + diffStyle/formatArgs aliases); ui/src/transcript.ts (new widgets, coordinator-fed live tool subs, settle-based outcomes).
- Modified: ui/src/app.ts (OutputStore, tool.complete retention for bound turns incl. non-visible, inspectOutput + viewer dialog, coordinator into Transcript); features/{types,index}.ts (inspectOutput, outputCommand); commands.ts (Info group).
- Tests: ui.test.ts (new visuals, V01-V03 + plain-user test), state.test.ts (marker updates), dialogs.test.ts (3 real-App viewer tests), features/gateway.e2e doubles.
Public interfaces added/changed:
- ToolTurn(name, args, activity) with settle(result: ToolResultView, outcome: ToolOutcome)/setOutcome/getState per 5.3; ToolOutcome incl. awaiting_approval (for T7) and historical.
- DiffBlock(lines, maxLines) + styleDiffLine (diffStyle alias kept).
- OutputStore(put/list/get/resolve/size) + OutputViewer(record, {tui?, onClose?}) with Esc/arrows/pgup/pgdn/home/end.
- FeatureHost.inspectOutput(toolId?): Promise<void>; /output command registered with completion/help.
- Removed: UserMessage/AssistantMessage/ToolCard/ToolState (callers migrated; no second implementation remains).
Tests added: V01 (plain user + immediate markdown), V02 (4-row previews, wrap counting, backend vs local truncation, empty), V03 (diff classification/whitespace/bounds), O01 (bounds, caps, eviction, resolve errors, viewer render/overlay scroll/Esc, command routing), 3 real-App dialog tests (draft survival, unknown id, quiet bare form).
Red command + failure that proved the defect:
- New O01 overlay scroll test failed: nested ScrollView keeps viewportHeight 0 inside overlays (verified even for a direct ScrollView overlay) because the installed toolkit only propagates viewports through layout roots. Fixed by owning the ScrollView as scroll-state truth + manual windowing mirrored on App's 70% sheet (TextForm precedent); deviation recorded.
- V03 fixture exceeded the 4-row default preview (working-as-specified); test now covers classification at maxLines 20 plus default bounds separately.
Green command + result:
- `npm --prefix ui run check` => typecheck pass, 197 tests / 192 pass / 5 env-skipped / 0 fail, lint pass.
- `git diff --check` => clean. rg for removed names/patterns: no matches.
Unresolved failures and reproduction: none.
Changed legacy expectations (visual spec intentionally changes these): boxed assistant -> borderless (test rewritten); Ran/Failed titles -> compact markers (all marker assertions updated); 6-line filled tool cards -> 4-line borderless previews; `>` user prefix (was accent ›).
Cache bounds: 8 most recent, 128 KiB UTF-8 each (codepoint-safe prefix), 1 MiB total, oldest-first eviction; evicted reads as unavailable; backend truncation reported without invented counts.
Next task and first action: T5 — AppShell/Composer widgets with 6.3 density budgets + cursor-safe composer; extend layout/dialogs/autocomplete tests (V04-V08, I01-I02) on real TuiAltScreen at 40x9/80x24/120x40 with live resize.

### T3 record (historical)

Task ID and status: T3 complete. T4 next.
Behavior delivered: Turn-bound presentation records, provisional pre-RPC binding confirmed against submit's turnId, honest tool outcomes (unknown/interrupted/historical, truncated preserved), canonical final-text reconciliation with duplicate guard, cumulative usage accounted once incl. non-visible sessions, watchdog warns once and retains the guard, no off-visible approval dialogs.
Files changed:
- New: ui/test/state.test.ts (15 tests: S01-S04, S06, S10 App-level; S07-S09 transcript-level; replay neutrality; truncation).
- Modified: ui/src/app.ts (SessionTurn record + turns map; submitTurn provisional/confirm/retained-on-unknown catch; honest watchdog; turn-routed onEvent with started/retired guards; approvalMatches via originTurnId; accountCompletion once incl. non-visible; switchTo restores in-flight usage; approval dialogs visible-only; resumed rebinds).
- Modified: ui/src/transcript.ts (markInterrupted/markUnknown; neutral replay via restoreHistory; incompatible-final setText reconcile; lastCompletedTurn guard; spinner hidden on stopping/cleanup; suspend() kept).
- Modified: ui/src/components.ts (AssistantMessage.setText; ToolState +interrupted/unknown/historical; ToolCard.complete(result,isError,truncated); markInterrupted/markUnknown/markHistorical/restoreHistory; honest headers and truncated line).
- Tests rewritten: app.test.ts watchdog (fake-time production 300s callback, guard retained, single warning); ui.test.ts cancel/interrupted + normal/unknown expectations.
Public interfaces added/changed:
- app: SessionTurn (private); Transcript.suspend() (kept); ToolCard.complete gains truncated?; new mark*/restoreHistory/setText.
- No FeatureHost changes; no any-weakening.
Tests added: 15 state tests listed above.
Red command + failure that proved the defect:
- `node --test test/state.test.ts` before implementation: 12 fail / 3 pass. The 3 passes (S03-provisional, S06, S10) locked already-correct behavior.
- During green: found real routing gap (submit-confirmed turnId vs later start needed a started flag) + 2 test bugs (ANSI-strip, case-sensitive match), all fixed.
Green command + result:
- `node --test test/state.test.ts` => 15 pass / 0 fail.
- `npm --prefix ui run check` => typecheck pass, 183 tests / 178 pass / 5 env-skipped / 0 fail, lint pass.
- `git diff --check` => clean.
- Real gateway wrapper `test_ui_checks_with_real_gateway_restart` => 1 passed (14.3s).
- rg for `You can submit again|startsWith("Error:")|complete("", false)` in src: no matches.
Unresolved failures and reproduction: none.
Changed legacy expectations (with reason): watchdog test (old test swapped a manual timer and asserted guard-clearing; production now retains the guard — replaced with fake-time production-callback assertions); cancel test (✗ Failed -> ○ Interrupted, unresolved outcomes are interruption facts); normal-complete test (added unknown assertion; no-interrupted/no-Stopped assertions unchanged); replay (neutral recorded output; old Error:-prefix inference removed).
Intentional notes: provisional flag recorded for T7's pending-request reopen; approval.resolved routes on stored turn_id (= origin, verified in mode.py); approval.resumed is HTTP-only today (no gateway emission found) so the rebind path is defensive; watchdog notice only for the visible session (background timeouts stay silent until their completion accounts).
Next task and first action: T4 — build UserTurn/AssistantTurn/ToolTurn/DiffBlock/Notice/OutputViewer widgets + output.ts feature (/output command, inspectOutput); migrate components.ts facade + transcript.ts; extend FeatureHost and all test doubles.

### T2 record (historical)

Task ID and status: T2 complete. T3 next.
Behavior delivered: One AnimationCoordinator per App owns at most one live timer for thinking/tool/approval/footer-growth activity. Removed pi-tui Loader ticker, transcript elapsed-thinking interval, and Footer private timer. ThinkingIndicator derives dots + elapsed from timestamps at render; Footer growth keeps 800ms/30s contract with static reduced-motion path. Disconnect/exit/switch retire activity.
Files changed:
- New: ui/src/design/motion.ts (AnimationCoordinator/ActivitySource/AnimationSubscription per section 5.3 + size/timerCount/isReducedMotion/isDisposed getters; generation-keyed subscriptions; single interval or one-shot; unref; dispose forbids callbacks).
- New: ui/src/components/Footer.ts (coordinator-owned growth ack, current-session filtering stays in App, idle square, right edge, 2 rows).
- Modified: ui/src/components.ts (re-export Footer; new ThinkingIndicator with spec dot frames + elapsed; diffStyle/ICONS wiring kept).
- Modified: ui/src/transcript.ts (removed spinnerTimer + elapsed interval; added suspend()).
- Modified: ui/src/app.ts (owns coordinator from AH_UI_REDUCED_MOTION; ThinkingIndicator replaces Loader; pulseGrowth() argless; suspend/dispose on disconnect/exit).
- Tests: ui/test/motion.test.ts (8 new); ui/test/ui.test.ts + layout.test.ts migrated to Footer(coordinator).
Public interfaces added/changed:
- design/motion: AnimationSubscription/ActivitySource/AnimationCoordinator(subscribe/frame/setReducedMotion/dispose + getters).
- components/Footer: constructor(activity), pulseGrowth(_requestRender?) (arg ignored, kept optional so old call sites compile), reducedMotion getter/setter delegating to coordinator, dispose.
- components: ThinkingIndicator(start/stop/setMessage/render/invalidate).
- transcript: suspend() (public; hideSpinner stays private behavior).
Tests added: many_activity_sources_use_one_timer, reused_key_old_handle_cannot_stop_new_subscription, dispose_prevents_future_renders, reduced_motion_has_no_frame_timer, growth_pulse_expires_and_cooldown_coalesces (20-update burst, 200ms frames, 800ms expiry, 30s boundary), growth_dispose_during_pulse_stops_renders, growth_pulse_reduced_motion_is_static_then_idle, footer_keeps_right_edge_and_two_rows.
Red command + failure that proved the defect:
- `node --test test/motion.test.ts` before implementation: suite failed to load (Cannot find module design/motion.ts, components/Footer.ts). Behavioral evidence: old code ran 3 concurrent timers per turn (Loader 80ms ticker + transcript 1000ms elapsed interval + Footer 200ms growth interval/setTimeout), violating the one-timer contract.
Green command + result:
- `node --test test/motion.test.ts` => 8 pass / 0 fail.
- `npm --prefix ui run check` => typecheck pass, 168 tests / 163 pass / 5 env-skipped / 0 fail, lint pass.
- `git diff --check` => clean.
- Scoped rg for setInterval/setTimeout/new Loader/spinnerTimer/growthTimer: only coordinator timer + gateway request timeouts + 300s in-flight watchdog (legitimate, not animation). Editor autocomplete debounce untouched.
Unresolved failures and reproduction: none.
Lifecycle ownership: App coordinator disposed on exit; footer.dispose + transcript.suspend on disconnect/exit/session-change; stale key handles cannot kill newer subs; reduced motion has no recurring ticker (finite ack uses single one-shot).
Next task and first action: T3 — read app.ts event routing + transcript.apply fully + gateway mode.py approvals; add state.test.ts red regressions S01–S10 (stale turn, pre-RPC events, watchdog with fake time, unknown outcomes, final-text reconcile, usage once).

### T3 recon notes (collected 2026-10-11, no production changes yet)

- `prompt.submit` returns `{turnId}` (ah/gateway/server.py:550); UI discards it (app.ts:461). submitTurn must confirm provisional binding against it.
- Routing is session-only (`inFlight: Set<sessionId>`); no turnId checks anywhere. Stale-turn, duplicate-start/complete, and pre-RPC races are unhandled.
- `message.complete` non-cancelled marks all unresolved tools success (transcript.ts:157). Must become outcome_unknown; cancelled becomes interrupted (keeps completed outcomes, which are already out of the map).
- Non-visible completion retires without token accounting (app.ts:567-577). Must account per-session via sessionTokensById.
- Watchdog clears the guard + says "You can submit again" (app.ts:470-480). Must retain guard, warn once ("Taking longer than expected; waiting for completion"), stay silent for non-visible sessions.
- Replay infers failure from "Error:" prefix (transcript.ts:68). Must become neutral historical.
- `tool.complete.truncated` dropped by ToolCard.complete(result, isError). Must preserve + show "server output truncated".
- Incompatible canonical final is dropped (transcript.ts:144-152 keeps streamed text). Must reconcile to canonical (needs AssistantMessage.setText in components.ts).
- promptApproval opens a picker for non-visible sessions (app.ts:536-542 runs before the visibility check at 567). Must not open dialogs off-visible (T7 owns the card/queue).
- Usage display is footer-only per event; switchTo drops in-flight usage. Preserve per-record usageLatest and restore on switch-back.
- `message.stopping` / `turn.cleanup_pending` leave thinking dots running; ownership_lost already hides. Hide on all three (static honest status).
- Existing timeout test (app.test.ts:452) swaps in a manual timer; replace with fake-time assertion against the production 300s callback.
- Legacy expectations that will change with reason: "cancelling marks unfinished tools" (/✗ Failed/ -> interrupted ○, S08); "a normal completion…" gains unknown assertion (existing no-interrupted/no-Stopped assertions still hold); replay test gains neutrality (no Error: inference).
- state.test.ts will carry its own minimal FakeTUI/DeferredFakeClient (app.test.ts helpers aren't importable without double-running that suite); S05 stays in app.test.ts rewritten in place.

### T1 record (historical)

Task ID and status: T1 complete. T2 next.
Behavior delivered: One lazy theme registry (design/theme.ts, 26 IDs incl. new opencode+light), semantic/palette/spacing/bounds tokens, centralized icons, safe text/ANSI/preview helpers. Old theme.ts is a facade; components use classifyDiffLine + ICONS growth frames (identical rendering).
Files changed:
- New: ui/src/design/tokens.ts (SemanticPalette/SemanticSlotName, LegacyBase, Spacing, LIMITS, VIEWPORT, CODEX_SEMANTIC_START).
- New: ui/src/design/icons.ts (ICONS, ASCII_FALLBACKS, TOOL_MARKERS).
- New: ui/src/design/text.ts (sanitizeDisplay, hasNoTerminalControls, displayWidth/truncate/wrap, countRenderedLines, classifyDiffLine, shapePreview, truncateUtf8ByBytes).
- Moved: ui/src/design/theme.ts (full SKINS registry + opencode/light + REQUIRED_THEME_IDS/isValidTheme/semanticPalette/LEGACY_SLOT_TO_SEMANTIC + lazy styles/selection/repair).
- Facade: ui/src/theme.ts (re-exports only, no second registry).
- Wired: ui/src/components.ts (diffStyle via classifyDiffLine; Footer growth frames/square via ICONS).
- Tests: ui/test/design.test.ts (8 tests).
Public interfaces added/changed:
- design/tokens: SemanticSlotName/SemanticPalette/LegacyBase/LegacyPaletteName/Spacing/SPACING_NORMAL/SPACING_COMPACT/LIMITS/VIEWPORT/CODEX_SEMANTIC_START.
- design/icons: ICONS/ASCII_FALLBACKS/iconFor/TOOL_MARKERS/ToolMarkerKind.
- design/text: sanitizeDisplay/hasNoTerminalControls/displayWidth/truncateDisplay/wrapDisplay/countRenderedLines/DiffKind/classifyDiffLine/PreviewShape/shapePreview/truncateUtf8ByBytes.
- design/theme: REQUIRED_THEME_IDS/isValidTheme/semanticPalette/LEGACY_SLOT_TO_SEMANTIC (+ all legacy exports preserved: SKIN_NAMES/setSkin/getSkin/skinDescription/skinBase/skinArt/theme/markdownTheme/selectListTheme/editorTheme/repairFill/isSelectionRow/selectionBar/PaletteName/SemanticPalette).
- theme.ts facade preserves every legacy import path; no caller changed imports.
Tests added: theme_registry_preserves_all_saved_ids, legacy facade resolves, skin switching round-trip, safe_text_never_emits_terminal_controls, unicode_rendering_stays_inside_width, diff classification, preview shaping/utf8, tokens/icons centralization.
Red command + failure that proved the defect:
- `node --test test/design.test.ts` (before text.ts fixes): safe_text failed (safe SGR stripped: C0_RE covered ESC 0x1B + final lone-ESC strip removed safe color); unicode wrap failed at width 0/1 (pi-tui emits unbreakable CJK width-2 rows). Both were real helper defects, not missing-file failures.
- Fix: C0_RE excludes ESC (\\x0e-\\x1a\\x1c-\\x1f), final ESC strip keeps safe SGR via negative lookahead, hasNo checks stripped text; wrap bound applies at width>=2 with no-throw at 0/1.
Green command + result:
- `node --test test/design.test.ts` => 8 pass / 0 fail.
- `npm --prefix ui run check` => typecheck pass, 160 tests / 155 pass / 5 env-skipped / 0 fail, lint pass.
- `git diff --check` => clean.
- Focused: `node --test test/design.test.ts test/ui.test.ts test/dialogs.test.ts test/layout.test.ts` => pass (existing Footer/diff/selection behavior unchanged).
Unresolved failures and reproduction: none. 5 skips are the same intentional gateway/DB gates as T0.
Intentional deviations (recorded before adapting callers):
- Semantic codex hex from spec 6.1 stored as CODEX_SEMANTIC_START + opencode skin uses those accent roles; legacy codex base hex left intact so current rendering is unchanged. Full semantic restyle deferred to T4/T5/T8.
- Light palette is new (neutral #f4f4f5, text #1a1a1e, distinct roles); genuinely light, not inverted RGB.
- Default startup remains `default`; T8 owns fresh-default -> codex + persistence truthfulness.
- Icons wired minimally (diff classify + Footer growth/square); full marker/ASCII-fallback switch deferred to widget rebuilds.
- No duplicated registries remain: SKINS only in design/theme.ts; growth frames only in icons.ts; diff regexes only in text.ts (rg verified).
Next task and first action: T2 — read Footer growth + App Loader/spinner integration + pi-tui Loader declarations; add motion.test.ts fake-time tests (one timer, key reuse, dispose, reduced motion, growth 800ms/30s); implement design/motion.ts coordinator and migrate Footer.

### T0 record (historical)

Task ID and status: T0 complete, T1 next.
Behavior delivered: Baseline recorded, no code changed, working tree still clean except this new progress file (untracked until written).
Files changed: docs/plans/TUI_OVERHAUL_PROGRESS.md (new).
Public interfaces added/changed: none.
Tests added: none (baseline only).
Red command + failure that proved the defect: N/A (T0 recon; no red cycle required).
Green command + result:
- `npm --prefix ui run check` => typecheck pass, 147 pass / 5 env-skipped, lint pass.
- `AGENT_HARNESS_TEST_DATABASE_URL=... .venv/bin/python -m pytest -q tests/test_personality_growth.py::test_ui_checks_with_real_gateway_restart` => 1 passed.
- `git diff --check` => clean.
Unresolved failures and reproduction: none at baseline; 5 standalone skips are intentional (missing AH_TEST_PYTHON/DSN), proven executed (skipped 0) in isolated wrapper.
Next task and first action: T1 — read ui/src/theme.ts fully + components.ts + pi-tui color/visibleWidth declarations; add characterization tests for existing IDs/skin switching; then create design/{tokens,icons,theme,text}.ts behind compatibility facade.
