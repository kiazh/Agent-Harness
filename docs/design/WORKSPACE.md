# Integrated Coding Workspace

The terminal app includes a code workbench beside the chat: an editable code
editor with file tabs, a right-edge file explorer, problems/results views, and
optional Python + TypeScript/JavaScript language intelligence backed by real
language servers.

## Layout

- Default at ≥ 89 columns: chat left, code workbench right, approximately 50/50.
- Chat maximized: chat uses the full width; the editor keeps every document,
  cursor and undo state.
- Editor maximized: the editor uses the full width.
- Below the split minimum the app shows one pane and restores your split
  automatically when space returns. Your saved preference is never overwritten
  by a temporary resize.
- The divider can be dragged with the mouse. The drawer (explorer) docks off
  the editor's right edge when the editor can keep its minimum width; otherwise
  it is an editor-local overlay.

## Commands

| Command | Purpose |
|---|---|
| `/workspace [split\|chat\|editor\|ratio\|side\|open <path>\|status\|close]` | Mode switching; `ratio <0.25-0.75\|reset>` sets the split ratio, `side left\|right` places the chat pane, `open <path>` opens another folder (re-mints the workspace identity), `status` shows root, epoch and server states, `close` retires the workspace connection (open documents are kept) |
| `/editor [maximize\|restore\|complete\|close]` | Editor maximize / back to split; `complete` requests completion at the cursor (fallback when the terminal or macOS intercepts `Ctrl+Space`); `close` closes the active tab |
| `/explorer [toggle\|open\|close]` | Right-edge file tree |
| `/lsp [status\|start\|stop\|restart] [profile]` | Language-server lifecycle; status prints per-profile state, generation and capabilities |
| `/problems` | Open/focus the Problems pane (Enter jumps to the location) |
| `/quickopen` | Search the workspace file index (bounded, cancellable) |
| `/attach` | Attach the current editor selection to the next message |

## Keys (when delivered by your terminal)

| Key | Action | Fallback |
|---|---|---|
| Ctrl+P | Quick Open | `/quickopen` |
| Ctrl+S | Save the active document (compare-and-swap; conflicts are shown) | command palette |
| Ctrl+Space | Completion | `/editor complete` |
| F12 / Shift+F12 | Definition / References | command palette |
| F2 | Rename preview (only where the server supports it; otherwise an honest notice) | — |
| F6 / Shift+F6 | Cycle focus: chat → editor → explorer → problems | commands |
| Ctrl+Left / Ctrl+Right | Editor-region only: `Ctrl+Right` toggles chat maximize, `Ctrl+Left` always returns to the 50/50 split (previous split restores). While the chat composer owns focus these keep word motion | `/workspace chat\|split` |
| Ctrl+T | Toggle explorer (some terminals use this for a new tab) | `/explorer` |
| Ctrl+W | Close the active editor tab (dirty → Save/Discard/Cancel). **Not** bound in the chat composer, where it remains delete-word-backward | `/editor close` |
| Esc | Dismisses the top editor popup first; a second Esc stops a running turn | — |

`Ctrl+Left`/`Ctrl+Right` are gated to the editor region (commit `6f3131c`):
the chat composer keeps its standard word motion. Editor word motion stays on
`alt+arrows`. Some terminals intercept `Ctrl+W` (close tab/window) or `Ctrl+T`
(new tab) before the app sees them; the command fallbacks above always work.

## Language servers

Two profiles are supported out of the box:

| Profile | Server | Notes |
|---|---|---|
| Python | `pyright-langserver --stdio` | configured interpreter/environment |
| TypeScript/JavaScript | `typescript-language-server --stdio` | uses the packaged TypeScript |

Resolution order: explicit configuration → `PATH` → the repo-local
`ui/node_modules/.bin/` fallback used by this development checkout. Nothing is
ever installed automatically, and a repository-supplied binary is not trusted
just because it is nearby.

- A missing server is reported honestly (`missing`) — plain editing keeps
  working, and no lookup result is ever dressed up as language intelligence.
- Servers start lazily when you open a file of that language; one process per
  workspace and profile serves the editor and the agent.
- Crash handling: pending requests fail with a structured error, the last
  diagnostics are kept but marked stale, and you can restart from `/lsp`.
  Automatic restarts are bounded (3 attempts / 5 minutes).
- A language server runs with your OS privileges. Protocol-level filtering is
  not a sandbox; this boundary is disclosed for trust decisions.

## Editing and saving

- Tabs: preview (single click) vs pinned (double click); dirty, conflict and
  read-only states are always labelled in text, not only color.
- Save is compare-and-swap: if the file changed on disk since it was read, you
  choose Reload / Overwrite (explicit) / Keep editing. Unsaved buffers are
  never silently replaced — not by reload, and not by agent writes.
- Quitting with unsaved files shows one review dialog (Save all & exit /
  Discard & exit / Cancel).
- Line endings, trailing whitespace and the final newline are preserved
  exactly; a read-only view appears for binary or oversized files.
- Unicode positions are correct for emoji, combining marks, CJK and tabs;
  diagnostics columns are never estimated from terminal cells.

## Agent integration and privacy

- The agent can discover that a workspace is open and which language servers
  are ready, plus a bounded, read-only `code_intelligence` tool
  (diagnostics / definition / references / symbols / hover).
- Code-intelligence results describe source analysis — they are not evidence
  that tests pass or the program runs correctly. Empty diagnostics mean the
  server reported none for that exact version; nothing more.
- Opening or viewing a file never sends its contents to the model. Unsaved
  content reaches the chat only through the explicit `/attach` selection flow,
  which shows the exact file, range, version and unsaved status.
- Server messages and repository comments are untrusted data, never
  instructions to widen permissions.

## Persistence and recovery

- Layout mode, split ratio, explorer state, open files and cursor positions are
  stored per workspace root (versioned, non-sensitive metadata only — never
  file contents or secrets). Corrupt or unknown state falls back to safe
  defaults and can never block startup.
- Restored tabs are re-validated: missing files are skipped, never silently
  re-pointed at same-named files.

## Troubleshooting

| Symptom | Check |
|---|---|
| No workbench appears | Terminal width < 89 columns, or `MACHINA_WORKSPACE=0` is set, or the backend predates the workspace methods (one notice is shown) |
| `/lsp status` says `missing` | The server binary was not resolved; install it explicitly and set the configured path or PATH |
| Completion does nothing | Cursor has no completion context, or the server is not `ready`; `/lsp status` shows the truth |
| TS diagnostics empty while editing | Restart the TS server (`/lsp restart typescript`); stale generation results are discarded by design |
| Explorer hides folders | Exclusions are shown in the explorer footer and are navigation policy, not permissions |
| Split disappeared | Terminal narrower than 89 columns; it restores automatically when widened |

## Rollout flag

`MACHINA_WORKSPACE=0` (or `false`/`off`) disables the workspace entirely:
startup, commands and keys behave exactly like the chat-only app.
