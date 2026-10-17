---
name: cli-tui
description: CLI + TUI development — Typer, Rich, Textual, async I/O, keybindings, markdown render
triggers:
- cli
- tui
- typer
- rich
- textual
- terminal
version: 1.0.0
schema_version: 2
skill_id: cli-tui
task_families:
- web-build
capabilities:
- cli
- layout
aliases:
- cli
- tui
- terminal-ui
- typer
- textual
stack:
  languages:
  - python
  frameworks:
  - typer
  - rich
  - textual
  independent: false
preconditions:
- A Python project that ships a command or terminal app
- A terminal to run and iterate on the interface
required_tools:
- shell
exclusions:
- Browser-based interfaces
- Library work with no user-facing command surface
sections:
- id: core-skills
  heading: Core Skills
  stage: implement
  requires_sections: []
- id: key-resources
  heading: Key Resources
  stage: inspect
  requires_sections: []
- id: practice-project
  heading: Practice Project
  stage: verify
  requires_sections:
  - core-skills
metadata_source: generated
metadata_generated_at: '2026-10-17T05:24:37.333192+00:00'
---

# CLI + TUI Development

## Core Skills

| Skill | Practice |
|---|---|
| Typer | Build machina chat, machina skills list, machina memory add |
| Rich | Syntax highlighting, progress bars, status panels |
| Textual | Reactive TUI with widgets |
| Async I/O | Stream LLM tokens without blocking UI |
| Keybindings | Ctrl+P palette, Ctrl+X session switch |
| Markdown render | Render markdown with syntax highlighting |

## Key Resources
- [Typer docs](https://typer.tiangolo.com/)
- [Rich docs](https://rich.readthedocs.io/)
- [Textual docs](https://textual.textualize.io/)

## Practice Project
Build a TUI with: main chat area (streaming markdown), context sidebar (token budget bar, loaded skills), status bar (session info, model, heartbeat countdown).
