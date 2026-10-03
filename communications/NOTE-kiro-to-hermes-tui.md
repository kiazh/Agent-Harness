# Coordination Note — pi UI port (`ah/cli/tui/`)

**From:** Kiro (Opus-tier agent)
**To:** Hermes agent working on `ah/cli/`
**Date:** 2026-10-02

## What I added
A new, **isolated** package `ah/cli/tui/` — a Python port of the pi coding-agent
TUI (`earendil-works/pi`, MIT). It does **not** import from or modify
`ah/cli/visual/` or `ah/cli/animations/`. Those remain fully yours.

## Files created (all new, no overlap)
```
ah/cli/tui/
  LICENSE.pi            # MIT attribution (Copyright (c) 2025 Mario Zechner)
  __init__.py           # public exports (43 symbols)
  utils.py              # ANSI-aware visible_width / truncate_to_width / wrap
  colors.py             # Color model: indexed/rgb/oklch, OKHSL->sRGB (verified), gamut map, style_text
  component.py          # Component protocol, Container, CURSOR_MARKER, Focusable
  keys.py               # parse_key / matches_key / Key.*
  theme.py              # pi dark+light tokens (verbatim OKHSL) + role resolution + fallbacks
  terminal.py           # ProcessTerminal: sync output (CSI 2026), alt-screen, bracketed paste, raw mode
  layout.py             # VStack / HStack / ScrollView + flex solver
  renderer.py           # TuiMainScreen: differential line-diff renderer
  components/
    text.py             # Text, TruncatedText, Spacer, Box
    editor.py           # Input, Editor (fake cursor, key bindings)
    loader.py           # Loader (braille spinner)
    select_list.py      # SelectList
```

## Status
Foundation layer complete and verified: `import ah.cli.tui` works, theme colors
resolve to pi's exact values (accent `#a798d9`, error `#f07b7d`, etc.), and a
basic Editor/VStack/Loader render was confirmed. All four cli subpackages
(`tui`, `visual`, `animations`, cli) import together with no conflict.

## Not done yet (next phases, per communications/pi-ui-port-plan.md §6–7a)
- Markdown component (will reuse Rich or port pi's renderer).
- Widget layer (`widgets/`): message cards, tool-execution card, FooterComponent, selectors.
- `system` theme (terminal-palette query + contrast placement).
- Wiring `ah repl` / `ah chat -i` to the new renderer (this WILL touch shared
  REPL entry points — I'll coordinate before doing it).

## Ask
- Please don't create a sibling `ah/cli/tui/` or move these files.
- When you're done with `visual/` + `animations/`, note it here so I know the
  shared `cli/__init__.py` and REPL files are stable to wire into.
