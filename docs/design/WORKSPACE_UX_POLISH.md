# Workspace UX polish spec (closing design pass)

Authority: `docs/design/TUI_DESIGN.md` (implementation contract) + the plan's mockup
(placement) + the impeccable design laws adapted to terminal UI. Register: **product**
(tool UI; design serves the work). Audit performed against real PTY frames captured at
commits `a672776..94a2895` (`/private/tmp/opencode/caps2..caps8`).

Design laws applied to TUI: hierarchy via weight/dim + one accent; spacing rhythm (not
uniform padding); every label earns its place (no restated words); states carry text,
never color alone; borders are structure, not decoration.

## CodeRabbit pass 1 triage (b0db077..HEAD)

| Finding | Severity | Disposition |
|---|---|---|
| didOpen sent profile language, not the document's protocol id (.tsx/.js grammar family) | major | **FIXED** (service.py uses `language_id_for`; regression added to integration suite; 31/31) |
| Problems duplicates: push keyed `pyright`, pull keyed `Pyright` | major | **FIXED at root** (backend tags each row with producer; TS narrowing parses producer objects + rows; refresh groups by producer key; 2 new tests) |
| Runbook personal absolute paths | minor | **FIXED** (env vars + setup note; zero /Users/shadow paths) |
| Ctrl+Left/Right maximize before region gate | minor | **FIXED** (closing pass 6f3131c; composer keeps word motion; RED-evidenced test) |
| Flush pending edit sync before intelligence request | major | **FIXED** (closing pass; RED-evidenced order test; this unblocked live completion) |
| Ctrl+V in editor region pastes into the composer | minor | **FIXED** (closing pass; region-aware routing; RED-evidenced) |

## Live verification (final PTY evidence, 160x48, real gateway + pyright)

- **Syntax colors render**: raw stream contains 1204 truecolor fg sequences, 0 256-color;
  `def` = `38;2;122;162;247` (accent), function name = secondary, `:`/`==` = muted
  operators, `"hi"`/`"__main__"` = `38;2;158;206;106` (success), active-line number = accent.
- **Completion popup works live** (finding 8 verified): after typing `from util import `
  (pyright ready) the popup row rendered `▶ v x` — selection marker, kind glyph `v`
  (Variable), label `x`, which was the fixture's only util.py name at that moment. The
  flush-before-intelligence fix is what makes this reachable (previously the request could
  outrun its didChange and be dropped as stale).
- **Settings menu**: top level (5 categories with inline values) and the Editor submenu
  (`Syntax highlighting current: On · Soft wrap current: Off · Tab width current: 4 · ← Back`).
- **Polish surfaces**: `workspace · ws-fixture` header grouping, single empty-state hint
  `No open files · Ctrl+P to open`, explorer `h: show hidden` + shortened exclusions
  (`excluded: .cache, .git, +5`), grip `▐`/`‹`, footer
  `Ctrl+T explorer · Ctrl+←/→ panes · ? shortcuts · / commands`, status row without a
  repeated language.
- **Interaction**: Ctrl+W closes the active tab (`[main.py ✕]` → `No open files`);
  `/workspace side right|left` swaps and restores chat/editor live; explorer open by default.

Lesson recorded: the shared scratch fixture was overwritten by a concurrent live repro
(the "def main()" content and the `x = 1` util), which briefly made a healthy completion
render look like an artifact. Live-verification fixtures must be recreated per run.

## Findings and required changes

1. **Empty editor restates itself.** Strip shows `No open files`, viewport shows
   `No file open`. Fix: strip shows nothing extra; the viewport shows one dim hint
   gutter-aligned at the first content row: `No open files · Ctrl+P or Ctrl+T to open`.
2. **Breadcrumb leaks absolute paths and dominates the row.**
   `/private/tmp/opencode/ws-fixture/main.py … python` truncates on narrow panes.
   Fix: root-relative path (`main.py`, `src/config.ts`) + language right-aligned;
   full path stays in `/workspace status`. Requires the pane to learn the workspace
   root (one `setWorkspaceRoot(path)` call from the app after open).
3. **Status row repeats the language**: `… · LF · python · python ready`. Fix:
   `Ln 5, Col 1 · UTF-8 · LF · Python` on the left, LSP state right-aligned when it
   fits (`ready`, `starting…`, `missing`), never repeating the language name.
4. **Problems pane duplicates rows (real bug).** Live frame 21 showed two identical
   `main.py:3:7 ✗ Argument of type "Literal[1…` rows while the backend returns ONE
   diagnostic. Fix: dedupe display rows by (uri, range, code, message, severity) when
   merging the event feed and the refresh pull; keep distinct producer labels only when
   content actually differs. Also: root-relative paths, ellipsize messages at pane
   width, keep `✗/!/i` glyph column aligned, keep counts header `✗ 2 · ! 0 · i 0`.
5. **Explorer chrome wording.** `workspace [h] hidden off` reads as a fragment. Fix:
   left `workspace`, right dim `h: show hidden` / `h: hide ignored`-style toggle hint
   stating the CURRENT state; directories keep `▸/▾`, files drop the leading `·`
   (indent only), symlinks keep `@`; exclusion footer shortens to
   `excluded: .git, node_modules, +4` (full list remains in `/workspace status`).
6. **Dirty-exit dialog copy.** `Unsaved changes: main.py (3/3)` is confusing. Fix:
   `1 unsaved file: main.py`; pluralize; show `main.py, util.py +3` beyond 2.
7. **Header grouping.** Right-side root label alone reads detached. Fix: `workspace · ws-fixture`
   (mockup language) with the label dimmed, matching the footer's quiet metadata voice.
8. **Completion popup (verify with a fresh capture once visible).** Requirements: item
   rows `glyph  label  detail(dim, truncated)`, selection via theme.selection bar plus a
   `▶` marker (never color-only), max 8 rows with `+N more`, aligned with the caret
   column, border only where it separates panes (no boxed-card look).
9. **Rhythm pass.** One blank row between strip/breadcrumb/content already holds; ensure
   editor gutter right-padding is consistent (2 cells), Problems pane is separated from
   the viewport by a full-width dim rule, and the divider/grip use `theme.border` while
   pane chrome fills use the existing `raisedSurface`/`toolBg` (no new colors; theme
   system is frozen per TUI_DESIGN.md).
10. **Quick Open wording.** `go to file main` → `Go to file: main`; results row keeps
    `❯` selection and dimmed directory; keep `N results · excluded: …` footer.

## Execution order (single writer on app.ts / view.ts / panes)

1. Syntax wiring (UX-B output → EditorView) + editor prefs.
2. Settings menu (`/settings`, categories; bare `/config` opens it).
3. This polish pass: items 1–7, 9–10 + popup verification (8) with fresh captures.
4. Fresh PTY capture set as before/after evidence; frame-level tests for every changed
   surface (empty state, breadcrumb relativity, status dedupe, problems dedupe,
   explorer header, dialog pluralization).

## Counterexample checks

- Narrow panes (40 cols): no item may clip another pane's text; ellipsis only at the
  item's own width.
- Non-color terminals: every state above still readable (text markers preserved).
- Long paths, CJK filenames, and control-bearing names render safely (existing
  sanitize rules keep applying).
