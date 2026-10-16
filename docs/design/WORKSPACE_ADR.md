# ADR: Integrated Editor Workspace (W-000)

Status: accepted (T0).
Context: `Machina_Integrated_Editor_LSP_Implementation_Plan.md` §3 requires a capability
spike before implementing a full editor: prove the pinned toolkit
(`@earendil-works/pi-tui@1.0.0`) can render two independently constrained panes with
focus, cursor, mouse and resize support, then decide between adapting the existing
`Editor` widget or building one bounded workbench editor adapter.

## 1. Recorded baseline (b0db077)

- Clean tree, Node v26.10.0, Python 3.14.5, macOS (darwin).
- `ui`: typecheck clean; 507 tests (501 pass / 6 skip / 0 fail); eslint clean.
- No language servers on PATH; `ui/node_modules/.bin` ships `tsc`/`tsserver` only.
- Python tests execute from the sibling `Machina-DevTools` checkout whose `machina/`
  and `ui/` are symlinks back to this repository.

## 2. Spike findings (pi-tui 1.0.0, evidence: shipped `dist`, full TS source embedded in sourcemaps)

| Capability | Result | Evidence |
|---|---|---|
| Two independently constrained panes | Supported | `TuiAltScreen.setLayoutRoot(new HStack([...]))`; `StackEntryOptions {basis, grow, shrink, minSize, maxSize}` allocate column counts; nested `ScrollView` per pane keeps independent scroll state (`dist/layout.js:200-246`) |
| Hardware cursor in right-hand component | Supported | `CURSOR_MARKER` (`\x1b_pi:c\x07`) emitted in a line; `extractCursorPosition` maps through composited layout; components must implement `Focusable` and only emit the marker when focused (`dist/tui.js`) |
| Mouse coordinates to local origin | Supported | `dispatchMouseToLayout` hit-tests retained frame geometry; handlers receive component-local `x/y` plus `screenX/screenY`, `width/height`; `press → {capture:true}` streams drag/release to the owner; synthesized `click` with `clickCount` (`dist/tui-alt-screen.js`) |
| Resize without state loss | Supported | Components are never recreated on resize; `ScrollView.updateLayout` clamps and preserves `followingEnd`; leaf components see a new width in `render()`; no resize callback is delivered (components observe widths) |
| Composer placement across modes | Composition decision | The composer is a full-width row beneath the panes in split/editor modes (beneath the transcript in chat-only), never inside a pane; the approval dock is a full-width row as well (`AppShell`/`WorkspaceShell` header comments) |
| Line numbers + editable content without ANSI/cursor leakage | Supported via custom component | Leaf/scroll-node contract: `render(width): string[]`, every line ≤ width; cursor via marker; gutter is our own column |
| Selection, undo/redo, bracketed paste | Must be built | Toolkit `Editor` has no selection model, undo-only (no redo), expands tabs, caps height at 30% of terminal, adds its own border; paste arrives as one bracketed string and needs no upstream change |

## 3. Decision

Build **one workbench editor adapter** (`ui/src/editor/view.ts`) owning its document
model and rendering. Do **not** reuse the pi-tui `Editor` widget as the code editor; do
**not** replace the toolkit or add a browser engine. Reuse the toolkit's infrastructure
as-is: alt-screen, layout nodes, `ScrollView`, `CURSOR_MARKER`, mouse dispatch,
`matchesKey`/Kitty protocol, bracketed-paste buffering contract, `visibleWidth`/`sliceByColumn`.

Reused primitive patterns (borrowed concepts, not code forks):
- grapheme-aware motion via `Intl.Segmenter` (same approach as toolkit `Editor`);
- windowed rendering honoring `render(width)` line budgets;
- `[LAYOUT_NODE]() → scroll` ownership is **not** required for v1: the editor pane is
  wrapped in a `ScrollView`; the editor keeps its own logical scroll offset for cursor
  following and translates wheel/scroll deltas through its own hit area. If measurements
  show the wrapper fights cursor-follow, the fallback is a custom scroll node
  (`ScrollLayoutState`), a bounded change recorded against this ADR.

## 4. Keyboard collision inventory (verified against shipped sources)

Listener order is fixed: `TuiAltScreen` viewport listener (registered in its constructor)
→ `App.onKey` (registered in `App.start`) → focused component (composer `Editor` or
the workspace components).

### Full matrix (post UX-batch bindings)

| Sequence | Toolkit owner | App/region policy | Verdict |
|---|---|---|---|
| `ctrl+w` | **composer Editor: delete-word-backward** | close-tab is gated to "not the chat composer" (fires while the editor, explorer or problems region owns focus); composer keeps delete-word | ok — gate implemented |
| `ctrl+left`/`ctrl+right` | **composer Editor: word motion** (`tui.editor.cursorWordLeft/Right`) | panes toggle is EDITOR-REGION-GATED: the handler returns before the branch unless the editor region owns focus | ok — gate implemented |
| `ctrl+t` | free in toolkit | explorer toggle global (composer does not use it) | ok |
| `ctrl+p` | free in toolkit | Quick Open global | ok |
| `ctrl+s` | free in toolkit (raw mode disables XOFF) | save (editor-gated today) | ok |
| `ctrl+space` | NUL legacy; not in TUI_KEYBINDINGS | completion (editor-gated); **macOS input-source switch may eat it** | ok + document; `/editor complete` fallback implemented (`features/workspace.ts`) |
| `f12`, `shift+f12`, `f2` | free in toolkit | navigation/rename (editor-gated) | ok |
| `f6`, `shift+f6` | free in toolkit (shift+F-keys need the legacy sequence decode — already handled) | focus cycle (global) | ok |
| `ctrl+shift+a` | free in toolkit; Kitty-only CSI-u | attach selection (editor-gated); legacy ctrl+a stays select-all | ok |
| `pageUp/pageDown/home/end`, `ctrl+up/down`, `ctrl+shift+f`, `shift+enter`, `ctrl+b/f/u/k/y/-`, `alt+d/b/f`, `alt+backspace`, `ctrl+j` | alt-screen viewport + composer Editor | NOT reused by the workspace | untouched |
| `ctrl+c`/`ctrl+d`/`ctrl+l`/`ctrl+shift+left/right`/`f1`/`esc` | existing App bindings | unchanged; popup-Esc precedence documented | untouched |

### Terminal / OS interception risks (documented, with fallbacks)

- **macOS Mission Control** defaults to Ctrl+←/→ ("Move a space"): on stock macOS these
  keys may never reach the terminal. Fallbacks: `/workspace split|chat|editor`, F6 focus
  cycle, and the footer workspace hints.
- **macOS input-source switch** defaults to Ctrl+Space: completion fallback is the
  command/palette path (`/editor complete`).
- Some terminals map Ctrl+T (new tab) or Ctrl+Shift+W/Ctrl+W (close tab); `/explorer`
  and the notch grip remain the fallbacks. Ctrl+W is never bound in the chat composer:
  the close-tab binding fires only while another region owns focus, so the composer's
  delete-word stays intact.
- Shift+F-keys and Ctrl+Shift+A only arrive fully under the Kitty keyboard protocol;
  legacy terminals keep Ctrl+A (select all) and get the attach command instead.

## 5. Source map (integration seams, verified)

UI:
- `ui/src/main.ts` — per-launch token, `TuiAltScreen` (hardware cursor off by default), `GatewayClient`, `App`.
- `ui/src/app.ts` — `App`: `onKey` (global funnel; workspace keys routed by `handleWorkspaceKey`), `submit`/`submitTurn`, `onEvent` (turn router; `onGatewayEvent`/`featureEventHandlers` is the workspace-event seam), `switchTo` (session switch must not discard workspace docs), `openDialog` (overlay system), approval dock via `shell.setDock`.
- `ui/src/components/AppShell.ts` — retained `transcriptScroll`, `viewportRoot` `[LAYOUT_NODE]` wrapper, `build()`/`densityFor`; the workspace composition rebuilds the root around the same component instances (panes row, then full-width dock/composer/footer).
- `ui/src/components/AssistantTurn.ts` + `ToolPanel.ts` — assistant sizing moved to `design/responseLayout.ts` (`responseGeometry`, T1); `assistantPanelContentWidth` is now a delegating legacy seam; `renderToolPanel`/`panelContentWidth`/`TOOL_PANEL_MAX_WIDTH` remain the tool-frame seam.
- `ui/src/transcript.ts` — `Transcript.apply`; workspace events do NOT enter this reducer.
- `ui/src/design/text.ts` — `sanitizeDisplay`/`displayWidth`/`wrapDisplay`/`countRenderedLines`; `spacing.ts` — `transcriptInset`/`panelInset`; `theme.ts` — semantic slots + `repairFill`.
- `ui/src/gateway.ts` — `JsonRpcChannel`/`GatewayClient` (token attached per request); `protocol.ts` hand-written result types; `events.ts` generated from `machina/protocol/events.json` (do not hand-edit).
- `ui/src/clipboard.ts` — real clipboard (`pbcopy`/`Set-Clipboard`/`wl-copy`/`xclip`).
- Commands: `features/index.ts` `COMMANDS`/`SLASH_COMMANDS`, `commandDispatch.ts` conflict table, `commands.ts` parsing/help.

Python:
- `machina/gateway/server.py` — newline-JSON-RPC, `add_method`, `handle_line` dispatch, auth token gate, event push pattern (`features/auth_chatgpt.py`), `Gateway.close` ordering.
- `machina/gateway/features/__init__.py` — `METHODS` + `register()`; add `workspace.py`.
- `machina/protocol/events.json` — chat event schema only; workspace events are a separate hand-written family (`machina/workspace/models.py` ↔ `ui/src/workspace/protocol.ts`) that never carry turn IDs.
- `machina/core/runtime.py` — `RuntimeServices` (lazy service getter idiom + `shutdown`) — WorkspaceService/LspManager live here.
- `machina/core/child_process.py` — `sanitized_env`, `group_members`, `kill_tree`, `run_bounded` — reuse for LSP spawn/kill.
- `machina/tools/file.py` — containment primitives (`resolve_path`, `_open_secure_read`, `_open_parent_dir_fd`, `_atomic_write_file_at`); extract a shared leaf for the workspace service.
- `machina/core/self_knowledge.py` — `FEATURES` + `configuration()` + `harness_status` (tokens pinned by DevTools tests).
- `machina/permissions/` — broker/user vs agent authorization (workspace methods answer to the gateway token; agent tool requests flow through the broker).

Tests:
- UI: `ui/test/*.test.ts` (flat; support files excluded by glob), harness patterns per `test/support/reference.ts`, `test/layout.test.ts` boot, `test/ui.test.ts` widget render swarms.
- Python: the sibling `Machina-DevTools` checkout's `tests` via `run-tests.sh` (`MACHINA_REPO`); ruff/bandit gates in main-repo CI.

## 6. Consequences

- The workspace editor is a first-class component with tests at model, view and PTY level.
- No parallel editing stack exists; `EditorStore` is the single unsaved-text authority in the UI;
  Python mirrors are transport state only.
- The toolkit is not forked; any deep import used must be pinned by a test
  (parity guard) so a future toolkit bump cannot silently break internals.
