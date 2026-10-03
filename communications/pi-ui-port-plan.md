# Pi TUI → AgentHarness UI Port — Deep Research & Plan

**Status:** Planning only. No implementation yet (per user instruction).
**Date:** 2026-10-02
**Author:** Kiro (Opus-tier review agent), coordinating with the Hermes agent.
**Goal:** Make AgentHarness's interactive CLI look and feel 1:1 with `earendil-works/pi` (the `pi` coding agent), as faithfully as a Python/Rich implementation allows.

---

## 0. TL;DR / Decision

- **License:** pi is **MIT**. We may legally adapt its UI into AgentHarness, commercially or not, provided we keep the MIT copyright + permission notice for any substantial copied portions and attribute it. No "stealing" needed — this is a legitimate port. (We mostly reimplement, not copy, so attribution is a courtesy + covers any snippets we do port.)
- **Language:** pi-tui is **TypeScript/Node**. AgentHarness is **Python**. A literal file copy is impossible.
- **Can Python match it?** **Yes for the look and ~90% of the feel**, with caveats (below). The visual result — layout, colors, components, streaming, spinners, slash-command menu, markdown — is fully achievable in Python. The hardest-to-match items are mouse support, inline images (Kitty/iTerm2 graphics), and the exact differential-render smoothness; these are "nice to have," not what makes it *look* like pi.
- **Recommendation:** **Reimplement in Python** rather than rewrite AgentHarness in TypeScript. A TS rewrite throws away the entire Python harness (agent loop, memory, RAG, DB) for a frontend — not worth it. BUT: a *moderate internal refactor* of AgentHarness's `cli/` layer is justified (see §7) because the current `interactive_win.py` is a flat msvcrt loop, whereas pi uses a clean component/renderer architecture that we should mirror.

---

## 1. What pi's UI actually is (verified from source)

pi's terminal UI lives in the **`@earendil-works/pi-tui`** package. It is a from-scratch, dependency-light TUI framework (not Ink/React, not blessed). Key facts gathered from `packages/tui/README.md`, `packages/tui/src/` listing, and `packages/tui/src/colors.ts`:

### 1.1 Rendering model
- **Two renderers behind one `TUI` interface:**
  - `TuiMainScreen` — renders into the normal terminal buffer, preserves scrollback. Three strategies: (1) first render dumps all lines; (2) width-change / change-above-viewport → clear + full redraw; (3) normal update → move cursor to first changed line, clear to end, redraw changed lines only.
  - `TuiAltScreen` — alternate-screen fixed-height viewport with app-owned scrolling, mouse, search panel, OSC 133 prompt jumps.
- **Differential rendering:** only changed lines/rows are rewritten.
- **Synchronized output:** wraps every frame in CSI 2026 (`\x1b[?2026h` … `\x1b[?2026l`) so updates are atomic → **no flicker**. This is the single most important trick behind pi's "smoothness."
- **Component contract:** every component implements `render(width) -> string[]` (one string per line, must not exceed `width`), optional `handleInput(data)`, optional `handleMouse(event)`, and `invalidate()` (clear render cache). Components cache their rendered lines keyed by width.
- **Fake cursor:** components emit a zero-width `CURSOR_MARKER` APC escape; the TUI scans for it and positions the hidden hardware cursor there (for IME). The visible cursor is drawn with inverse video (`\x1b[7m…\x1b[27m`).

### 1.2 Component inventory (what we must reproduce)
Built-ins from pi-tui: `Text`, `TruncatedText`, `Input`, `Editor`, `Markdown`, `Loader`, `CancellableLoader`, `SelectList`, `SettingsList`, `MouseRegion`, `Spacer`, `Image`, `Box`, `Container`, `VStack`, `HStack`, `ScrollView`.

The ones that define the *coding-agent look*:
- **Editor** — multiline input with horizontal rule above/below, slash-command autocomplete (type `/`), file-path autocomplete (`Tab`, supports `~/ ./ ../ @`), large-paste collapsing to `[paste #1 +50 lines]`, fake cursor, rich key bindings (Ctrl+A/E/W/U/K, Alt+Backspace, word nav, Ctrl+] jump-to-char).
- **Markdown** — headings, bold/italic/strike/underline, code + fenced code blocks with border, blockquotes with border, lists, links, `<hr>`, optional syntax highlighting hook; render caching.
- **Loader** — animated spinner with colored spinner + message; `CancellableLoader` adds Escape→AbortSignal.
- **SelectList / SettingsList** — keyboard+mouse menus (used for model/theme/settings pickers).
- **ScrollView + VStack/HStack** (alt-screen only) — the transcript scroll region + the editor/footer stack with `basis/grow/shrink/minSize/maxSize` flexbox-like layout; `follow: "end"` to track streaming output, scroll-to-end indicator.
- **Overlays** — centered/anchored modal layer with full positioning (anchor, percent, absolute, margin), for dialogs/menus.

### 1.3 Color system (verified from `colors.ts`)
This is the part that makes colors look *identical*, and it maps almost 1:1 onto what AgentHarness already has:
- Color type is a union: **indexed (ANSI 0-255)**, **sRGB**, or **OKLCH**. Everything converts to sRGB so color math never fails.
- `parseColor()` accepts `#rgb`/`#rrggbb`, `oklch(L C H)`, and `okhsl(H S% L%)`.
- `mixColors(a, b, amount, space)` interpolates in OKLCH (default) or sRGB; hue takes the shortest path.
- **OKLCH → sRGB gamut mapping by chroma bisection** (reduce chroma until in gamut; achromatic fallback). **AgentHarness `cli/visual/colors.py` already implements exactly this** (`gamut_map_bisection`, `oklch_to_srgb`, `tint`, OKHSL). So our color engine is already pi-compatible in spirit.
- Terminal output: truecolor (`38;2;r;g;b`) or 256-color (`38;5;n`) via `rgbToAnsi256`. `styleText()` composes SGR with proper nested reset ordering.
- OKHSL saturation is gamut-relative so "equal saturation looks equally colorful across hues" — the trick behind pi's balanced palette.

### 1.4 `packages/tui/src/` file map (reference for what each concern lives in)
`tui.ts` (interface), `tui-main-screen.ts`, `tui-alt-screen.ts`, `layout.ts` + `layout-node.ts` (VStack/HStack/ScrollView geometry), `terminal.ts` (ProcessTerminal/VirtualTerminal), `colors.ts` + `oklab.ts` + `terminal-colors.ts` (color), `editor-component.ts`, `autocomplete.ts`, `fuzzy.ts`, `keybindings.ts` + `keys.ts` (key matching, Kitty keyboard protocol), `kill-ring.ts` + `undo-stack.ts` + `word-navigation.ts` (editor internals), `wheel-scroll.ts`, `alt-screen-search.ts`, `terminal-image.ts` (Kitty/iTerm2), `components/` (the built-in widgets), `utils.ts` (`visibleWidth`, `truncateToWidth`, `wrapTextWithAnsi`).

---

## 2. Can Python/Rich do this? Feature-by-feature verdict

| pi-tui feature | Python feasibility | How |
|---|---|---|
| Differential rendering | ✅ Full | Rich `Live` already does diff-based cell updates; or hand-roll a line-diff writer. |
| Synchronized output (no flicker) | ✅ Full | Emit CSI 2026 ourselves around frames, or rely on Rich `Live` (screen=False) which batches. |
| Component `render(width)->lines` model | ✅ Full | Define a Python `Component` protocol; wrap Rich renderables or emit raw ANSI lines. |
| OKLCH/OKHSL + gamut mapping colors | ✅ Already done | `cli/visual/colors.py` already has the math. Port pi's exact token values. |
| Editor (multiline, autocomplete, kill-ring, word nav) | ✅ High | Reimplement; we already have an msvcrt input loop + autocomplete dropdown to build on. prompt_toolkit `Buffer` could accelerate this. |
| Markdown with themed code blocks | ✅ Full | Rich `Markdown` + custom theme, or port pi's renderer for exact styling. |
| Spinners / loaders | ✅ Full | We already have a spinner/loader system (once the current bug is fixed). Match pi's frames + colors. |
| SelectList / SettingsList menus | ✅ Full | We have an autocomplete dropdown + interactive help nav already; generalize into these. |
| Slash-command + file autocomplete | ✅ Full | Already partially built in `interactive_win.py`. |
| Alt-screen viewport + app-owned scroll | ✅ High | Rich `Live(screen=True)` + a `Layout`, or raw alt-screen (`\x1b[?1049h`). More work but doable. |
| VStack/HStack/ScrollView flex layout | ✅ Medium | Rich `Layout` gives split regions; `basis/grow/shrink` needs a small solver (we can port pi's). |
| Bracketed paste (>10 lines → marker) | ✅ Full | Enable `\x1b[?2004h`, parse paste markers. |
| Mouse (click/drag/wheel/hit-test) | ⚠️ Partial | Possible via SGR mouse (`\x1b[?1006h`) + manual hit-testing, but it's a lot. Likely **defer** — keyboard-first like our current REPL. pi's MainScreen also skips mouse. |
| Inline images (Kitty/iTerm2) | ⚠️ Optional | Kitty/iTerm2 protocols are emittable from Python, but niche. **Defer.** |
| OSC 133 prompt markers / OSC 52 clipboard / OSC 8 links | ✅ Easy wins | Just escape sequences; add selectively. |
| Kitty keyboard protocol key parsing | ⚠️ Medium | We can parse the common subset; full protocol is a lot. Match what we need. |

**Bottom line:** The *visual identity* (layout, palette, components, streaming, markdown, menus, spinners) is **fully reproducible in Python**. The gaps (mouse, inline images, full Kitty kbd) are exactly the parts pi itself treats as alt-screen extras and that don't define the signature look. So we can get a faithful 1:1 **appearance** and a very close **feel**, keyboard-first.

The two things we must copy precisely to *look* identical:
1. **The exact color tokens / theme values** (OKLCH/hex). Our color engine already matches; we just need pi's numbers.
2. **The exact layout + glyphs** (editor rules, prompt symbols, footer/status format, spinner frames, box-drawing chars, spacing/padding).

---

## 3. Target screen anatomy (what "1:1" means concretely)

pi's interactive coding-agent screen (alt-screen viewport) composes, top→bottom:

1. **Transcript / scroll region** (`ScrollView`, `follow: "end"`, `grow: 1`): streamed assistant markdown, user messages, tool-call cards, spacers between messages, loader while responding.
2. **Editor block** (`basis: auto`, `shrink: 1`): horizontal rule, multiline input with fake cursor, autocomplete dropdown overlay when typing `/` or `Tab`.
3. **Status/footer line** (`TruncatedText`): compact single line (session/model/token/mode indicators).

Everything wrapped in synchronized output; transcript auto-follows while at bottom, holds position when scrolled up, shows a clickable "scroll to end" indicator.

To hit 1:1 we must capture, from a running `pi` (or screenshots/docs), the exact: prompt symbol(s), footer text format, color of each element, rule characters, spacing, spinner frames, and message framing. **Action item:** capture reference screenshots of real `pi` for pixel comparison (user or a sandbox install).

---

## 4. Proposed Python architecture (mirror pi-tui, thin)

Create a new sub-package `ah/cli/tui/` that mirrors pi-tui's structure, implemented over Rich where convenient:

```
ah/cli/tui/
  __init__.py          # public exports
  terminal.py          # Terminal protocol: start/stop/write/size/clear + CSI 2026 sync + alt-screen + bracketed paste + mouse enable
  renderer.py          # TUIMainScreen + TUIAltScreen: component tree, focus, overlays, diff render loop, request_render()
  component.py         # Component protocol: render(width)->list[str], handle_input, invalidate; CURSOR_MARKER cursor model
  layout.py            # VStack/HStack/ScrollView + flexbox solver (basis/grow/shrink/minSize/maxSize), follow:end
  components/
    text.py            # Text, TruncatedText, Spacer, Box, Container
    editor.py          # Editor (multiline, kill-ring, word-nav, autocomplete, paste markers, fake cursor)
    markdown.py        # Markdown (themed; wrap Rich Markdown or port pi's renderer)
    loader.py          # Loader, CancellableLoader (spinner frames + colors matched to pi)
    select_list.py     # SelectList, SettingsList
  theme.py             # ColorScheme tokens ported from pi (reuse cli/visual/colors.py math)
  keys.py              # matches_key(), Key.* helpers (subset of pi's keybindings/keys)
  utils.py             # visible_width, truncate_to_width, wrap_text_with_ansi (ANSI-aware)
  LICENSE.pi           # pi's MIT license text (attribution for any ported logic)
```

Then the AgentHarness REPL (`interactive_win.py` or a new `interactive_tui.py`) composes these exactly like pi's coding-agent: `ScrollView(transcript) / Editor / status` inside a `VStack`.

**Reuse what exists:** `cli/visual/colors.py` (OKLCH engine — already pi-equivalent), the slash-command registry + autocomplete dropdown + interactive help nav already in `interactive_win.py`, and the (soon-fixed) spinner/animation classes.

---

## 5. Attribution / license handling (do this when we implement)

- Add `ah/cli/tui/LICENSE.pi` containing pi's MIT license + copyright line.
- In any module that ports a non-trivial algorithm from pi (e.g., the OKLCH gamut bisection, the flex layout solver, the ANSI width/truncate utils, keybinding tables), add a header comment: `# Adapted from earendil-works/pi (MIT). See LICENSE.pi.`
- Note it in README under a "Credits / Third-party" section.
- This keeps the portfolio project clean and legally correct for Waterloo/Nous review.

---

## 6. Phased implementation plan (for later — not now)

- **Phase A — Foundations:** `terminal.py` (sync output + alt-screen + bracketed paste), `component.py`, `renderer.py` (main-screen diff first), `utils.py`, `theme.py` with pi's tokens. Deliver: a static themed frame renders flicker-free.
- **Phase B — Core widgets:** `Text/TruncatedText/Spacer/Box`, `Loader`, `Markdown`. Deliver: a transcript of themed markdown + spinner matching pi.
- **Phase C — Editor + autocomplete:** port `Editor` behavior and slash/file autocomplete; wire key bindings. Deliver: pi-identical input block.
- **Phase D — Layout + alt-screen scroll:** `VStack/HStack/ScrollView` + flex solver + follow-end. Deliver: full pi screen anatomy (§3).
- **Phase E — Menus + overlays:** `SelectList/SettingsList`, overlay layer for model/theme/settings pickers.
- **Phase F — Polish/parity:** exact token/glyph matching against reference screenshots; optional OSC 8/52/133; optional (defer) mouse + inline images.
- **Phase G — Tests:** component render tests (golden ANSI line snapshots), width/truncation, layout solver, key matching. (Our test suite currently has zero CLI/visual coverage — this closes that gap.)

---

## 7. Refactor question (answered)

A **TypeScript rewrite is NOT recommended** — it discards the Python harness. A **targeted refactor of `ah/cli/` IS recommended**: replace the flat msvcrt loop with the component/renderer model above. This is additive (new `ah/cli/tui/` package) and can coexist with the existing REPL until parity is reached, then swap `ah repl`/`ah chat -i` to use it.

**Collision warning:** The Hermes agent is currently editing `ah/cli/visual/` and `ah/cli/animations/`. Our port touches the same `cli/` area. **Do not start implementation until Hermes finishes its current fix batch and the animations package is confirmed working.** This doc is coordination-safe (planning only).

---

## 7a. DEEPER PASS — source-verified (2026-10-02, second review)

The pi-tui README is only half the story. The **actual coding-agent screen** is built in `packages/coding-agent/src/`, verified from its `index.ts` and `docs/themes.md`. This is what we must match for a true 1:1.

### 7a.1 Real UI components live in the coding-agent, layered on pi-tui
`packages/coding-agent/src/modes/interactive/components/` exports the concrete widgets that make the pi screen (not in pi-tui):
- **Messages:** `UserMessageComponent`, `AssistantMessageComponent`, `CustomMessageComponent`, `BranchSummaryMessageComponent`, `CompactionSummaryMessageComponent`, `SkillInvocationMessageComponent`, `ArminComponent`.
- **Tools:** `ToolExecutionComponent`, `BashExecutionComponent`, `renderDiff` (+ `RenderDiffOptions`), `truncateToVisualLines`.
- **Chrome:** `FooterComponent` (status/footer line), `DynamicBorder`, `BorderedLoader`, `keyHint`/`keyText`/`rawKeyHint` (the `Ctrl+X hint` affordances).
- **Dialogs/selectors (overlays):** `ModelSelectorComponent`, `ThemeSelectorComponent`, `SettingsSelectorComponent`, `SessionSelectorComponent`, `TreeSelectorComponent`, `ThinkingSelectorComponent`, `OAuthSelectorComponent`, `LoginDialogComponent`, `ShowImagesSelectorComponent`, `UserMessageSelectorComponent`.
- **Editors:** `CustomEditor`, `ExtensionEditorComponent`, `ExtensionInputComponent`, `ExtensionSelectorComponent`.

**Implication for us:** our `ah/cli/tui/` port needs an **`ah/cli/tui/widgets/`** layer (coding-agent-equivalent) on top of the primitive components — message cards, tool-execution card, footer, selectors. The pi-tui primitives alone won't look like pi; these widgets are the look.

### 7a.2 The screen entry point
`modes/interactive/` exports `InteractiveMode` (+ `InteractiveModeOptions`). There are also `runPrintMode` (print/JSON) and `runRpcMode` (RPC). So pi has three run modes; we care about `InteractiveMode`. It composes the transcript (message/tool components) + editor + `FooterComponent` inside the alt-screen `VStack`/`ScrollView` layout from §3.

### 7a.3 Theme system — this is the exact palette source
`packages/coding-agent/src/modes/interactive/theme/` holds:
- `theme.ts` — the `Theme` class + helpers: `initTheme`, `getMarkdownTheme`, `getSelectListTheme`, `getSettingsListTheme`, `highlightCode`, `getLanguageFromPath`, and types `ThemeAppearance/ThemeBg/ThemeColor/ThemeStyle/ThemeToken`.
- `theme-schema.json` — the authoritative list of color roles.
- Built-in themes: **`system`** (default), **`dark`**, **`light`** — written in **OKHSL** with a `vars` block (reusable values, can reference each other) + a `colors` block (role → color).

**Color roles (from `docs/themes.md`, authoritative token list to port):**
- General: `accent`, `border*`, `text`, `muted`, `dim`, `success`, `error`, `warning`
- Selection/fullscreen: `selectedBg`, `searchMatch*`, `scrollbar*` (`scrollbarTrack`→`muted`, `scrollbarThumb`→`text` fallbacks)
- Messages: `userMessage*`, `customMessage*`, `thinkingText`
- Tool execution: `toolPendingBg`, `toolSuccessBg`, `toolErrorBg`, `toolTitle`, `toolOutput`
- Markdown: `md*`
- Tool diffs: `toolDiff*`
- Syntax highlighting: `syntax*`
- Editor modes: `thinking*`, `bashMode`
- HTML export: `export.pageBg`, `export.cardBg`, `export.infoBg`
- Color forms accepted: `#rgb`/`#rrggbb`, `oklch(…)`, `okhsl(…)`, 256-index int, var reference, `""` = terminal default.
- Optional roles inherit: `scrollbarTrack←muted`, `scrollbarThumb←text`, `searchMatchBg←selectedBg`, `searchMatchText←text`, `thinkingMax←thinkingXhigh`; `export.*` derives from `userMessageBg`.

**The `system` theme (default) is a signature behavior:** it queries the terminal's default fg/bg + 16 ANSI colors (OSC escapes), takes each role's hue from an ANSI color (errors←red, links←blue), and sets lightness for a minimum contrast (body text ≥ 4.5:1 WCAG on bg and every panel). Re-queries on light/dark switch. Waits ≤100ms at startup for the terminal to answer, else falls back to ANSI indices.

**Port action:** copy the `dark`, `light`, and `system` theme JSON values verbatim into `ah/cli/tui/themes/` (they're data, OKHSL — our `cli/visual/colors.py` already does OKHSL→sRGB + gamut map). Implement the `system` theme's terminal-query + contrast-placement logic (we have `relative_luminance`/`contrast_ratio` in `colors.py` already). This is the single highest-leverage step for "colors look identical."

### 7a.4 Markdown + syntax highlighting
pi exports `getMarkdownTheme` and `highlightCode` + `getLanguageFromPath`. The `Markdown` component takes a `MarkdownTheme` (heading/link/code/codeBlock/quote/hr/listBullet/bold/italic/strikethrough/underline + optional `highlightCode`). For 1:1 markdown we must map these theme functions to our Rich Markdown styling (or port pi's renderer). Syntax highlighting in pi is its own `highlightCode(code, lang)`; in Python we'd use Pygments/Rich syntax with a theme matched to pi's `syntax*` tokens.

### 7a.5 Keybindings are configurable + conflict-checked
pi exports a full `KeybindingsManager` with `TUI_KEYBINDINGS`, `getKeybindings/setKeybindings`, `KeybindingConflict`. Keys use `matchesKey`/`parseKey`/`Key.*` and support the **Kitty keyboard protocol** (`decodeKittyPrintable`, `isKeyRelease`, `isKeyRepeat`, `isKittyProtocolActive`). For v1 we match the default `TUI_KEYBINDINGS` table and the common key subset; full Kitty protocol + user-remap is a later nicety.

### 7a.6 Footer data
`FooterComponent` is fed by a `FooterDataProvider` (git branch + extension statuses). To match the footer 1:1 we need: git branch, model, token/context usage, mode indicators. AgentHarness already tracks model/provider/tokens in the REPL; we'd add git-branch detection.

### 7a.7 Revised scope statement
A faithful port is **two layers**, not one:
1. **Primitive layer** (`ah/cli/tui/`) ≈ pi-tui: terminal/renderer/layout/components/colors/keys/utils. (§4)
2. **Widget layer** (`ah/cli/tui/widgets/` + themes) ≈ coding-agent `modes/interactive/`: message cards, tool card, footer, selectors, the 3 themes, markdown theme, `InteractiveMode` composition.

Everything here is Python-feasible. Nothing in the deeper pass changes the "yes, Python can do it" verdict — but it **increases the surface area**: the look is defined as much by the coding-agent widget + theme layer as by pi-tui. Budget accordingly.

### 7a.8 Files to copy verbatim vs reimplement (MIT, with attribution)
- **Copy as data (low risk, high value):** the `dark`/`light`/`system` theme JSON token values; the spinner frame sets; keybinding default table; markdown theme role mapping. (Data/tables, trivially portable.)
- **Reimplement in Python (logic):** renderer diff loop, layout flex solver, editor, autocomplete/fuzzy, ANSI width/truncate utils, color engine (already have it). Add `# Adapted from earendil-works/pi (MIT)` headers.

## 8. Open items / inputs needed
1. Reference screenshots or a running `pi` to extract exact tokens/glyphs/footer format (docs don't give pixel values).
2. Confirm keyboard-first (defer mouse + inline images) is acceptable for v1 — recommended.
3. Decide alt-screen vs main-screen default (pi uses alt-screen for the coding agent; main-screen preserves scrollback). Recommend alt-screen for the full look, main-screen as a fallback.
4. Pull `packages/coding-agent` theme + screen-composition source (couldn't retrieve the exact file via web yet) to copy precise token values before Phase F.
