# AgentHarness UX/CLI Audit — Brutal Final Verdict

**Date:** 2026-10-01  
**Scope:** Entire CLI/UX surface — visual design, animations, autocomplete, help, errors, streaming, sessions, config, accessibility, consistency  
**Verdict:** A well-architected engine with a half-built cockpit. The backend is production-grade; the frontend is a developer prototype that was never finished. Every serious UX feature was started but not shipped.

---

## 1. Visual Design — 4/10

### What exists
- **Theme system** (`ah/cli/theme.py`): 4 themes (dark, light, claude, hermes) with full `ColorPalette` — 30+ semantic colors each. Well-structured.
- **Visual layer** (`ah/cli/visual.py`): `ColorScheme`, `PanelStyles`, `TableStyles`, `PromptStyles`, `StatusIndicators`, `VisualContext` — a proper design system.
- **Prompt styling**: `PROMPT_STYLE` dict for prompt_toolkit with completion menu, scrollbar, path styling.

### What's broken
- **Default theme is `"default"`** — this theme does NOT exist in `ThemeRegistry`. The registry only has `"dark"`, `"light"`, `"claude"`, `"hermes"`. On first launch, `ThemeRegistry.get("default")` falls back to the dark theme, but the config file says `"default"`. This is a silent bug that will confuse users who try to change themes.
- **Two visual languages in the same app**: `ah/cli/__init__.py` (Typer commands) uses raw `console.print("[red]...[/red]")` with ad-hoc styling. `ah/cli/interactive.py` (REPL) uses `VisualContext` with panels and tables. The `ah chat` command and `ah repl` command look like different applications.
- **No consistent header/footer**: The REPL prints a welcome panel, but `ah chat` prints raw text. No consistent branding.
- **No terminal width adaptation**: Panels and tables don't adapt to narrow terminals. A `Panel` with long content will wrap badly on an 80-column terminal.
- **No colorblind-safe palette**: Status is conveyed only by color (green dot = active, yellow = idle). No text labels or patterns for colorblind users.

### What's missing
- No light/dark auto-detection at startup (the `detect_terminal_theme()` method exists but is never called).
- No user-defined theme support (the `create_custom_theme()` function exists but is never exposed in the CLI).
- No consistent spacing/padding rules — some commands print extra blank lines, others don't.

---

## 2. Animations — 2/10

### What exists
- **Massive animation library** (`ah/cli/animations.py`, 930 lines): `Spinner` (18 styles), `ProgressBar`, `LoadingDots`, `SquareLoader`, `TypingEffect`, `FadeTransition`, `FrameAnimation`, `ThinkingAnimation`, `ToolExecutionAnimation`, `StreamingAnimation`, `ErrorAnimation`, `SuccessAnimation`, `AnimationRunner`.
- ASCII art logos (`ASCII_LOGO`, `ASCII_LOGO_SMALL`, `ASCII_THINKING`, `ASCII_TOOL`, `ASCII_ERROR`, `ASCII_SUCCESS`, `ASCII_STREAMING`).

### What's broken
- **Almost none of this is used in the actual CLI.** The interactive REPL uses a bare `Live` with `Text(response_text, style="green")` — no spinner, no thinking animation, no tool execution animation, no streaming cursor.
- The `Spinner` class has 18 styles but the REPL never shows a spinner while waiting for the LLM.
- The `ThinkingAnimation` with its cute ASCII robot faces is never displayed.
- The `ErrorAnimation` and `SuccessAnimation` are never used — errors are printed as `[red]text[/red]`.
- The `SquareLoader` (Claude-style) is never used.
- The `AnimationRunner` is never used.
- The ASCII logos are never printed at startup.

### Verdict
This is a **930-line dead code library**. Someone built an animation framework and then forgot to wire it into the REPL. The user stares at a static `Live` text update while the LLM thinks — no spinner, no progress indication, no visual feedback that anything is happening.

---

## 3. Autocomplete — 3/10

### What exists
- `SlashCommandCompleter` — prompt_toolkit `Completer` that filters slash commands as you type `/`.
- `AutoSuggestFromHistory` — suggests previous commands from history.
- `FileHistory` — persists command history to `~/.agent-harness/repl_history`.

### What's broken
- **Only completes slash commands.** No autocomplete for:
  - `/switch <session_id>` — user must type a full UUID with no suggestions
  - `/model <model_name>` — no model name completion
  - `/provider <name>` — no provider completion
  - File paths in tool arguments
  - Tool names
- **No fuzzy matching** — must type the exact prefix. `/h` doesn't match `/help` if there's a `/history` command (there isn't, but the point stands).
- **No completion for command arguments** — `/budget` should suggest token amounts, `/switch` should suggest recent session IDs.
- **No multi-line input** — the prompt is single-line only. No way to paste a multi-line prompt or code block.
- **No paste handling** — pasting a large block of text into the prompt will trigger autocomplete on every line.

---

## 4. Help Menu — 4/10

### What exists
- Interactive help menu using prompt_toolkit `Application` + `TextArea` with scrollbar.
- Commands grouped by category (general, session, config).
- Arrow key navigation, Enter to select, Esc/Ctrl+C/q to close.
- Shows command name, description, and usage.

### What's broken
- **The help text is built with raw string formatting** — not using the visual system. No colors, no panels, no tables. It's plain monospace text in a `TextArea`.
- **`_build_help_text()` is called twice** — once for the `TextArea` widget and once for the `FormattedTextControl`. Wasteful and inconsistent.
- **No search/filter** — with 13 commands it's manageable, but the architecture doesn't scale. No way to filter by typing.
- **No aliases shown** — `/exit` has alias `/quit` but the help doesn't show it.
- **No examples** — no usage examples for any command.
- **No keyboard shortcut reference** — the help says "↑/↓ navigate • Enter select • Esc close" but doesn't document Ctrl+C, Ctrl+D, or other shortcuts.
- **Selecting a command just prints it** — `run_interactive_help()` prints `/command` to stdout, but the REPL doesn't execute it. The user must press Enter, see the command printed, then type it again. This is broken UX.
- **No context-sensitive help** — `/help model` doesn't show model-specific help.

---

## 5. Error Display — 2/10

### What exists
- Try/except blocks around agent runs and provider calls.
- `logger.exception()` for logging.
- `audit_log()` for security events.

### What's broken
- **Errors are printed as `[red]Agent error:[/red] {e}`** — raw exception string, no formatting, no panel, no animation, no suggestion.
- **No error recovery** — when the LLM provider fails, the error is printed and the REPL continues. No suggestion to check API key, retry, or switch provider.
- **No error categorization** — network errors, auth errors, rate limit errors, and validation errors all look the same.
- **No stack trace in verbose mode** — `logger.exception()` logs to stderr but the user never sees it unless they redirect output.
- **No error panel** — the `PanelStyles.error()` method exists but is never used for error display.
- **No error animation** — the `ErrorAnimation` class exists but is never used.
- **Silent failures** — memory consolidation failures are logged but never surfaced to the user. RAG retrieval failures are silently swallowed.
- **No Ctrl+C handling during agent run** — pressing Ctrl+C during a streaming response will crash the REPL or leave it in an inconsistent state.

---

## 6. Streaming — 5/10

### What exists
- `run_stream()` async generator yields `StreamEvent` objects.
- `Live` display with `refresh_per_second=10` for real-time updates.
- Text accumulation and display during streaming.
- Tool call and result events in verbose mode.
- Final output rendered as `Markdown` in a `Panel`.

### What's broken
- **No streaming cursor** — the `StreamingAnimation` class has a cursor (`▌`) but it's never used. The user can't tell if the stream is still active or stuck.
- **No markdown rendering during streaming** — text is displayed as raw `Text` during streaming, then re-rendered as `Markdown` in a Panel after completion. This causes a jarring visual jump.
- **Tool calls shown as raw text** — `→ tool_name(args)` is appended to the text stream, not displayed as a separate formatted block.
- **No token counter during streaming** — the user can't see how many tokens have been used or how much budget remains.
- **No cost display** — OpenRouter provides cost info but it's never shown.
- **No interrupt** — no way to stop a streaming response mid-generation.
- **No ETA** — no indication of how long the response will take.
- **Verbose mode is all-or-nothing** — either you see every tool call or you see nothing. No middle ground.

---

## 7. Session Management — 4/10

### What exists
- `SessionManager` with PostgreSQL persistence and 5-second TTL cache.
- Create, get, list, archive, switch sessions.
- Session state stored as MessagePack.
- `last_activity` timestamp tracking.

### What's broken
- **No session persistence across REPL restarts** — history is saved to file, but the current session ID is not. Every time you start the REPL, you get a new session.
- **No session export/import** — no way to save a conversation to a file or load it back.
- **No session search** — `/sessions` lists the 10 most recent but doesn't allow searching by title or content.
- **No session metadata** — no tags, no created-at display, no token usage summary.
- **`/switch` requires full UUID** — no fuzzy matching, no tab completion, no "switch to last session" shortcut.
- **No session deletion** — can archive but can't delete.
- **No session fork** — no way to branch a conversation.
- **No multi-session support** — can only have one active session at a time.
- **Session ID displayed as raw UUID** — `550e8400-e29b-41d4-a716-446655440000` is not user-friendly. Should show a short hash or title.

---

## 8. Config System — 5/10

### What exists
- YAML config file at `~/.agent-harness/config.yaml`.
- Environment variable overrides (`AGENT_HARNESS_<KEY>`).
- Per-session overrides (in-memory only).
- Type coercion for bool, int, float.
- `config-set` CLI command with `--persist` flag.

### What's broken
- **Default theme is `"default"`** — doesn't exist in the registry (see §1).
- **No config validation** — setting `context_budget` to a negative number or `max_iterations` to 0 will silently break the agent.
- **No config migration** — if the schema changes, old config files will break.
- **No interactive config editor** — `/config` shows values but doesn't allow setting them. Must use `/model`, `/provider`, `/budget` individually.
- **Config changes in REPL don't persist** — `/model` changes the in-memory value but doesn't save to file. If you restart, you lose the change.
- **No config reset** — no way to reset to defaults.
- **No config diff** — no way to see what's changed from defaults.
- **Sensitive values in plain text** — API keys are in env vars (good) but the config file could accidentally contain sensitive data.

---

## 9. Accessibility — 1/10

### What exists
- Nothing. No accessibility features whatsoever.

### What's missing
- **No screen reader support** — Rich output is visual-only. No text alternatives.
- **No high contrast theme** — the dark theme has low contrast in some areas (e.g., `#565f89` on `#1a1b26`).
- **No keyboard-only navigation** — the help menu requires arrow keys but there's no alternative for users who can't use them.
- **No font size adjustment** — no way to increase font size for visually impaired users.
- **Color-only status indicators** — status dots use only color. No text labels or patterns.
- **No reduced motion option** — animations can't be disabled for users with motion sensitivity.
- **No internationalization** — all strings are hardcoded English.

---

## 10. Consistency — 3/10

### What's inconsistent
- **Two output styles**: Typer commands use raw `console.print()`, REPL uses `VisualContext` with panels.
- **Two prompt styles**: `ah chat` has no prompt, `ah repl` has `ah (short_id) >`.
- **Two error styles**: Typer commands print `[red]error[/red]`, REPL prints `[red]Agent error:[/red]`.
- **Two table styles**: Typer commands use `Table()` directly, REPL uses `visual.tables.standard()`.
- **Inconsistent command naming**: `ah sessions` (plural) vs `/sessions` (plural) vs `ah config` (singular) vs `/config` (singular) vs `ah memory-list` (kebab) vs `/memory-list` (kebab).
- **Inconsistent flag naming**: `--continue` vs `--session` vs `--model` vs `--provider` — some have short forms, some don't.
- **Inconsistent output formatting**: Some commands print extra blank lines, some don't. Some use panels, some don't.
- **Inconsistent status messages**: "Session created" vs "New session created" vs "Switched to session".
- **Inconsistent error messages**: "Provider error" vs "Unknown provider" vs "LLM provider error".

---

## 11. Critical UX Bugs

### 11.1 No Ctrl+C Interrupt During Agent Run
Pressing Ctrl+C while the agent is running will either crash the REPL or leave it in an inconsistent state. There's no `KeyboardInterrupt` handler in the streaming loop.

### 11.2 No Timeout for User Input
The prompt will wait forever. No idle timeout, no way to cancel a pending input.

### 11.3 Help Menu Selection Doesn't Execute
Selecting a command in the help menu prints it to stdout but doesn't execute it. The user must type it again.

### 11.4 No Multi-line Input
The prompt is single-line only. No way to paste a multi-line prompt or code block.

### 11.5 No Conversation Export
No way to save a conversation to a file. No `/export` command.

### 11.6 No Context Clear
`/clear` clears the screen but doesn't clear the agent's context. The agent still remembers the previous conversation.

### 11.7 No Token Usage Display in Prompt
The prompt shows `ah (short_id) >` but doesn't show token usage, budget remaining, or cost.

### 11.8 No Model/Provider Display in Prompt
The prompt doesn't show which model or provider is active. The user must type `/status` to find out.

### 11.9 No Session Title Display
The prompt shows a short UUID but not the session title. The user can't tell which session they're in without typing `/status`.

### 11.10 No Way to Go Back
No way to undo a message, go back to a previous state, or replay a conversation.

---

## 12. What Good Looks Like

Every serious agent CLI has solved these problems:

| Feature | Claude Code | Hermes | OpenCode | Codex CLI | AgentHarness |
|---------|-------------|--------|----------|-----------|--------------|
| Interactive REPL | ✅ | ✅ | ✅ | ✅ | ✅ |
| Streaming with cursor | ✅ | ✅ | ✅ | ✅ | ❌ |
| Ctrl+C interrupt | ✅ | ✅ | ✅ | ✅ | ❌ |
| Multi-line input | ✅ | ✅ | ✅ | ✅ | ❌ |
| Session persistence | ✅ | ✅ | ✅ | ✅ | ❌ |
| Conversation export | ✅ | ✅ | ✅ | ✅ | ❌ |
| Token/cost display | ✅ | ✅ | ✅ | ✅ | ❌ |
| Autocomplete (files, args) | ✅ | ✅ | ✅ | ✅ | ❌ |
| Error recovery | ✅ | ✅ | ✅ | ✅ | ❌ |
| Animations | ✅ | ✅ | ✅ | ✅ | ❌ (built but unused) |
| Accessibility | ✅ | ✅ | ✅ | ✅ | ❌ |
| Config validation | ✅ | ✅ | ✅ | ✅ | ❌ |
| Session search | ✅ | ✅ | ✅ | ✅ | ❌ |
| Multi-session | ✅ | ✅ | ✅ | ✅ | ❌ |

---

## 13. Priority Fix List

### P0 — Ship-blocking
1. **Wire animations into the REPL** — at minimum, show a `Spinner` while waiting for the LLM and a `StreamingAnimation` cursor during streaming.
2. **Add Ctrl+C interrupt handling** — catch `KeyboardInterrupt` in the streaming loop and stop gracefully.
3. **Fix the default theme bug** — change `DEFAULTS["theme"]` from `"default"` to `"dark"`.
4. **Make help menu selection execute the command** — currently it just prints the command.
5. **Add multi-line input support** — at minimum, support paste with `Alt+Enter` or `Ctrl+D` to finish.

### P1 — User experience
6. **Add token/cost display in the prompt** — show `tokens/budget` and estimated cost.
7. **Add model/provider display in the prompt** — show `model@provider` in the prompt.
8. **Add session title display in the prompt** — show title instead of UUID.
9. **Add conversation export** — `/export <filename>` to save conversation as markdown.
10. **Add context clear** — `/clear` should clear the agent's context, not just the screen.
11. **Add error panels** — use `PanelStyles.error()` for error display.
12. **Add error recovery suggestions** — "Did you mean...", "Try /switch to another session", etc.
13. **Add config validation** — validate values on set, reject invalid values.
14. **Add config persistence in REPL** — `/model`, `/provider`, `/budget` should persist by default.

### P2 — Polish
15. **Add autocomplete for session IDs** — `/switch` should suggest recent sessions.
16. **Add autocomplete for model names** — `/model` should suggest known models.
17. **Add session search** — `/sessions --search <query>`.
18. **Add session deletion** — `/delete <session_id>`.
19. **Add session fork** — `/fork` to branch a conversation.
20. **Add light/dark auto-detection** — call `detect_terminal_theme()` at startup.
21. **Add high contrast theme** — for accessibility.
22. **Add reduced motion option** — disable animations.
23. **Add consistent output formatting** — use `VisualContext` everywhere.
24. **Add consistent command naming** — pick one convention and stick to it.
25. **Add ASCII logo at startup** — print `ASCII_LOGO_SMALL` in the welcome panel.

---

## 14. The Fundamental Problem

The AgentHarness CLI was built **inside-out**. The backend (agent loop, context management, RAG, memory, tools) is production-grade — 330 tests, proper error handling, audit logging, rate limiting, retry logic. But the frontend was bolted on as an afterthought.

The REPL is the primary interface, but it's the least polished part of the system. The animation library is 930 lines of dead code. The visual design system is comprehensive but underused. The autocomplete only works for slash commands. The help menu doesn't execute commands. Errors are printed as raw text. There's no Ctrl+C handling.

**The engine is a Ferrari. The dashboard is a cardboard cutout.**

To fix this, the team needs to:
1. **Stop building backend features** until the CLI is usable.
2. **Wire the existing animation library** into the REPL.
3. **Add interrupt handling** — this is a safety issue.
4. **Add multi-line input** — users need to paste code and prompts.
5. **Add session persistence** — users need to resume conversations.
6. **Add export** — users need to save their work.
7. **Add accessibility** — colorblind users and screen reader users are excluded.
8. **Add consistency** — the two output styles must be unified.

Until these are fixed, AgentHarness is a developer tool that only the developer can use. And that's not a product.
