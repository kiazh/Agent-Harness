# TUI Research: Production Terminal UIs for AI Agent CLIs

**Date:** 2026-10-01  
**Scope:** What a production TUI looks like for an AI agent CLI, with recommendations for AgentHarness.

---

## 1. The Python TUI Landscape

Three libraries dominate Python terminal UIs. They are complementary, not competing — production systems combine them.

| Library | Type | Role | GitHub Stars |
|---------|------|------|-------------|
| **Rich** | Rendering library | Output formatting: tables, panels, syntax highlighting, progress bars, markdown | ~57k |
| **Textual** | Full TUI framework | Interactive apps: widgets, layout, CSS-like styling, async event loop, mouse | ~36k |
| **prompt_toolkit** | Input/REPL toolkit | Line editing, autocompletion, history, key bindings, multiline prompts | ~11k |

**Key insight:** Rich and Textual are by the same author (Will McGugan). Textual is built on Rich — every Rich renderable works inside a Textual widget. prompt_toolkit powers IPython and pgcli; it is the gold standard for interactive line input.

### When to use which

- **Rich alone** — Scripts that need beautiful output but no interactivity. Progress bars, tables, syntax-highlighted code, spinners. Zero flicker with `Live`.
- **prompt_toolkit + Rich** — REPL-style CLIs. Rich renders output; prompt_toolkit handles input (history, completion, key bindings). Preserves native terminal scrollback and copy-paste. This is what Hermes uses.
- **Textual** — Full-screen interactive apps. Multi-pane layouts, dashboards, sidebar navigation, command palette, mouse support. Owns the alt-buffer. This is what Claude Code's TUI mode and many modern agent CLIs use.

---

## 2. What Production AI Agent CLIs Actually Do

Every major coding agent converges on the same terminal UX pattern:

| CLI | TUI Stack | Pattern |
|-----|-----------|---------|
| **Claude Code** | Ink (React for terminal) + custom differential renderer | Full-screen, streaming markdown, collapsible tool calls, diffs, approval prompts |
| **Codex CLI** | Ink (React) | Similar to Claude Code — streaming, tool panels, session management |
| **GitHub Copilot CLI** | Ink (React) | Chat-shaped, streaming, slash commands |
| **Aider** | Custom (prompt_toolkit + Rich) | REPL with streaming, syntax highlighting, git integration |
| **Hermes** | prompt_toolkit (REPL) + Ink (full TUI) | Dual-mode: lightweight REPL and full-screen TUI |
| **OpenCode** | Custom | Terminal-native, streaming, tool approval |
| **GenericAgent** | Textual (v2) + prompt_toolkit (v3) | Two TUI versions: widget-based and scrollback-first |

### The universal pattern

```
┌──────────────────────────────────────────────────┐
│  Header: model, session, tokens, cost            │
├──────────────────────────────────────────────────┤
│                                                  │
│  Conversation (scrollable, streaming)            │
│  ─ User messages (right-aligned or distinct)     │
│  ─ Assistant responses (markdown, syntax hl)    │
│  ─ Tool calls (collapsible, with status)         │
│  ─ Tool results (truncated, expandable)          │
│  ─ Diffs (unified, with inline approval)         │
│                                                  │
├──────────────────────────────────────────────────┤
│  Input bar (multiline, history, completion)      │
├──────────────────────────────────────────────────┤
│  Footer: status, key hints, progress             │
└──────────────────────────────────────────────────┘
```

---

## 3. Streaming Display

### The core challenge

LLM responses stream at 50-100+ tokens/second. The terminal must update in place without flicker, without blocking input, and without corrupting scrollback.

### Approaches

#### 3.1 Rich `Live` (simplest)

```python
from rich.console import Console
from rich.live import Live
from rich.text import Text

console = Console()

with Live(console=console, refresh_per_second=10, transient=False) as live:
    response_text = ""
    async for event in agent.run_stream(session_id, message):
        if event.type == "text":
            response_text += event.content
            live.update(Text(response_text, style="green"))
```

**Pros:** Simple, zero flicker, works in any terminal.  
**Cons:** Re-renders the entire content on each update. Flickers with complex markdown (code blocks, nested lists). No input handling — the user cannot type while streaming.

#### 3.2 Rich `Live` + `Status` (spinner + content)

```python
from rich.status import Status

with Status("[bold green]Thinking...", spinner="dots") as status:
    async for chunk in llm.stream():
        response_text += chunk
        status.update(f"[bold green]Thinking... ({len(response_text)} chars)")
```

**Pros:** Shows progress while waiting for first token.  
**Cons:** Same limitations as Live.

#### 3.3 Textual `Markdown.get_stream()` (best for complex UIs)

```python
from textual.app import App, ComposeResult
from textual.widgets import Markdown, Input, Footer, Header
from textual.containers import VerticalScroll

class AgentApp(App):
    CSS = """
    Response { border: wide $success; background: $success 10%; }
    Prompt { background: $primary 10%; }
    """
    
    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="chat-view"):
            yield Markdown()  # Response widget
        yield Input(placeholder="How can I help?")
        yield Footer()
    
    @on(Input.Submitted)
    async def on_input(self, event: Input.Submitted) -> None:
        response = Response()
        await self.query_one("#chat-view").mount(response)
        # Stream tokens in a worker
        async for chunk in llm.stream():
            await response.append_markdown(chunk)
```

**Pros:** Native streaming markdown, no flicker, input stays responsive, scrollable history, CSS styling.  
**Cons:** Owns the alt-buffer — breaks native terminal selection and tmux scrollback. Steeper learning curve.

#### 3.4 prompt_toolkit + Rich live region (best for REPL preservation)

```python
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.live import Live

session = PromptSession(history=FileHistory("~/.ah_history"))
console = Console()

while True:
    user_input = session.prompt("> ")
    if user_input.startswith("/"):
        handle_command(user_input)
        continue
    
    with Live(console=console, refresh_per_second=8) as live:
        async for event in agent.run_stream(session_id, user_input):
            if event.type == "text":
                live.update(Text(event.content))
```

**Pros:** Preserves native scrollback, copy-paste, tmux integration. Input is best-in-class.  
**Cons:** No widget system. No split panes. No mouse support. Streaming can flicker with complex content.

### Flicker-free rendering techniques

Production systems use these to eliminate flicker:

1. **Synchronized output** — Wrap updates in `BSU`/`ESU` escape sequences (or `sync_start`/`sync_end` in Rich) so the terminal batches repaints.
2. **Double-buffered diffing** — Only emit the delta between frames, not the entire screen.
3. **Append-optimized fast path** — For simple character appends, emit ANSI codes directly without diffing.
4. **Transient live regions** — Use `transient=True` in Rich `Live` so the streaming area is replaced by the final output, not appended to scrollback.
5. **Refresh rate limiting** — 8-12 Hz is smooth enough for token streaming without wasting CPU.

---

## 4. Progress Bars and Indicators

### What to show

| Indicator | When | How |
|-----------|------|-----|
| **Spinner** | Waiting for first token | Rich `Status` or custom spinner |
| **"Thinking..."** | LLM generating | Rich `Status` with spinner |
| **Tool execution** | Tool running | Inline: `→ tool_name(args)` with spinner |
| **Iteration counter** | Each ReAct iteration | `Iteration 2/10` in header or status |
| **Token counter** | Real-time | `Tokens: 1,234 / 8,000` in footer |
| **Cost tracker** | Real-time | `Cost: $0.0123` in footer |
| **Context budget** | Real-time | Visual bar: `██████░░░░ 60%` |
| **Tool result** | After tool completes | `← result_preview` (truncated) |

### Rich progress bar example

```python
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn

with Progress(
    SpinnerColumn(),
    TextColumn("[progress.description]{task.description}"),
    BarColumn(),
    TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
    TextColumn("{task.completed}/{task.total} tokens"),
) as progress:
    task = progress.add_task("Generating...", total=None)
    async for chunk in llm.stream():
        progress.update(task, advance=len(chunk.split()))
```

### Textual approach

Textual widgets have a `loading` reactive that shows a `LoadingIndicator`:

```python
widget.loading = True  # Shows spinner
# ... do work ...
widget.loading = False  # Shows content
```

---

## 5. Syntax Highlighting

### Rich `Syntax`

```python
from rich.syntax import Syntax

code = Syntax(
    source_code,
    lexer="python",
    theme="monokai",
    line_numbers=True,
    indent_guides=True,
    word_wrap=True,
)
console.print(code)
```

**Lexers:** All Pygments lexers (Python, JS, Rust, Go, SQL, Bash, JSON, YAML, etc.)  
**Themes:** `monokai`, `github-dark`, `dracula`, `solarized-light`, etc.

### Textual `Markdown` widget

Textual's `Markdown` widget renders syntax-highlighted code blocks automatically:

```python
from textual.widgets import Markdown

md = Markdown()
md.update("```python\ndef hello():\n    pass\n```")
```

### prompt_toolkit syntax highlighting

```python
from prompt_toolkit.lexers import PygmentsLexer
from pygments.lexers import PythonLexer

session = PromptSession(lexer=PygmentsLexer(PythonLexer))
```

### Production pattern

Most agent CLIs use Rich's `Syntax` for code blocks within markdown responses. The LLM returns markdown with fenced code blocks; the renderer detects the language and applies the appropriate lexer.

---

## 6. Key Bindings

### prompt_toolkit key bindings

```python
from prompt_toolkit.key_binding import KeyBindings

kb = KeyBindings()

@kb.add("c-q")
def _(event):
    """Quit."""
    event.app.exit()

@kb.add("c-c")
def _(event):
    """Interrupt current operation."""
    interrupt_agent()

@kb.add("c-l")
def _(event):
    """Clear screen."""
    event.app.renderer.clear()

@kb.add("tab")
def _(event):
    """Accept suggestion."""
    b = event.app.current_buffer
    if b.complete_state:
        b.complete_next()
    else:
        b.start_completion(select_first=False)

session = PromptSession(key_bindings=kb)
```

### Textual key bindings

```python
class AgentApp(App):
    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+c", "cancel", "Cancel"),
        ("ctrl+l", "clear", "Clear"),
        ("up", "history_prev", "Previous"),
        ("down", "history_next", "Next"),
        ("tab", "complete", "Complete"),
    ]
    
    def action_cancel(self) -> None:
        """Cancel current stream."""
        self.cancel_worker()
```

### Standard key bindings for agent CLIs

| Key | Action |
|-----|--------|
| `Enter` | Submit message |
| `Shift+Enter` | Newline (multiline input) |
| `Ctrl+C` | Interrupt current operation |
| `Ctrl+C` (again) | Exit REPL |
| `Ctrl+D` | Exit REPL |
| `Ctrl+L` | Clear screen |
| `Up/Down` | History navigation |
| `Tab` | Autocomplete (slash commands, file paths) |
| `Ctrl+P` | Command palette (Textual) |
| `Escape` | Cancel / close modal |

---

## 7. Split Panes and Layout

### Textual layout system

Textual uses CSS-like styling for layout:

```python
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Footer, Input, Markdown, RichLog, Tree

class AgentApp(App):
    CSS = """
    #sidebar {
        width: 30;
        dock: left;
        border-right: solid $primary;
    }
    #chat {
        width: 1fr;
    }
    #input-bar {
        height: 3;
        dock: bottom;
    }
    #status-bar {
        height: 1;
        dock: bottom;
        background: $boost;
    }
    """
    
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Tree("Sessions")
            with VerticalScroll(id="chat"):
                yield Markdown()
        yield Input(placeholder="Message...")
        yield Footer()
```

### Layout patterns for agent CLIs

**Pattern 1: Chat + Sidebar (Claude Code style)**
```
┌──────────┬─────────────────────────────┐
│ Sessions │  Conversation               │
│          │  ─ User message             │
│ > abc123 │  ─ Assistant response       │
│   def456 │  ─ Tool call (collapsed)    │
│   ghi789 │  ─ Tool result              │
│          │                             │
├──────────┴─────────────────────────────┤
│ > Input                                │
├─────────────────────────────────────────┤
│ Model: gpt-4o | Tokens: 1,234 | $0.01  │
└─────────────────────────────────────────┘
```

**Pattern 2: Chat + Tool Panel (IDE style)**
```
┌────────────────────────┬────────────────┐
│  Conversation          │  Tool Output   │
│                        │                │
│  User: Fix the bug     │  $ git diff    │
│                        │  +++ file.py   │
│  Assistant: I'll fix   │  - old code    │
│  it.                   │  + new code    │
│                        │                │
│                        │  [Accept] [Reject] │
├────────────────────────┴────────────────┤
│ > Input                                │
└─────────────────────────────────────────┘
```

**Pattern 3: Scrollback-first (REPL style)**
```
$ ah
> hello
[Agent streams response...]

> what did you do?
[Agent explains...]

> /sessions
  ID       Title          Status
  abc123   Fix bug        active
  def456   Refactor       idle

> /switch abc123
Switched to session abc123
```

### prompt_toolkit layout

prompt_toolkit has a layout engine with containers and controls:

```python
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl

root_container = HSplit([
    Window(content=FormattedTextControl("Header")),
    VSplit([
        Window(width=30, content=FormattedTextControl("Sidebar")),
        Window(content=FormattedTextControl("Chat")),
    ]),
    Window(height=3, content=FormattedTextControl("Input")),
])

layout = Layout(root_container)
```

---

## 8. Streaming Architecture Patterns

### Pattern A: Async generator + Live (simplest)

```python
async for event in agent.run_stream(session_id, message):
    if event.type == "text":
        live.update(Text(accumulated_text))
    elif event.type == "tool_call":
        show_tool_call(event.tool_name, event.tool_args)
    elif event.type == "tool_result":
        show_tool_result(event.tool_result)
```

### Pattern B: Worker + Message passing (Textual)

```python
@work(thread=True)
def stream_response(self, message: str) -> None:
    """Runs in a worker thread, posts messages to the app."""
    for chunk in llm.stream(message):
        self.call_from_thread(self.post_message, TokenChunk(chunk))
```

### Pattern C: Event-driven with cancellation

```python
class AgentApp(App):
    def on_key(self, event: events.Key) -> None:
        if event.key == "ctrl+c":
            if self.agent_running:
                self.agent_task.cancel()
                self.agent_running = False
            else:
                self.exit()
```

### Handling interruption

```python
import asyncio

async def run_agent():
    task = asyncio.create_task(agent.run_stream(...))
    try:
        async for event in task:
            yield event
    except asyncio.CancelledError:
        yield StreamEvent(type="cancelled")
        raise
```

---

## 9. Recommendations for AgentHarness

### Current state

AgentHarness uses Typer + Rich `Live` for one-shot commands. The `chat` command streams via `Live` but has no interactive REPL, no session management, no tool approval, and no progress indicators beyond iteration counters.

### Recommended architecture: Hybrid approach

**Phase 1: prompt_toolkit REPL (immediate, preserves scrollback)**

Build an interactive REPL using prompt_toolkit for input and Rich for output. This is the fastest path to a usable CLI.

```
ah                    # starts REPL
> hello               # prompt_toolkit handles input
[Agent streams via Rich Live]
> /sessions           # slash command
> /model gpt-4o       # switch model
> /quit               # exit
```

**Phase 2: Textual TUI (full-screen, multi-pane)**

Add a full-screen TUI mode for users who want it:

```
ah --tui              # starts Textual app
```

With sidebar (sessions), main chat area, tool panel, and status bar.

**Phase 3: agentui-style primitives**

Consider building or adopting agent-specific TUI primitives:
- Streaming markdown with zero flicker
- Collapsible tool call blocks
- Unified diff rendering with inline approval
- Slash command palette
- @-file picker

### Dependency matrix

| Dependency | Purpose | Already in project? |
|------------|---------|-------------------|
| `rich` | Rendering | Yes |
| `textual` | Full TUI framework | No — add for Phase 2 |
| `prompt_toolkit` | REPL input | No — add for Phase 1 |
| `pygments` | Syntax highlighting | Yes (Rich dependency) |

### Implementation priority

1. **Interactive REPL** — prompt_toolkit `PromptSession` with history, completion, key bindings
2. **Streaming output** — Rich `Live` with synchronized output, transient mode
3. **Progress indicators** — Spinner, token counter, context budget bar
4. **Slash commands** — `/help`, `/clear`, `/model`, `/context`, `/tokens`, `/export`, `/quit`
5. **Session management** — `/sessions`, `/switch`, `/fork`, `/archive`
6. **Tool approval** — Confirmation prompt before destructive tools
7. **Interruption** — Ctrl+C cancels current operation
8. **Textual TUI** — Full-screen mode with sidebar, chat, tool panel
9. **Configuration** — `~/.agentharness/config.yaml`
10. **Export/Import** — Markdown, JSON

---

## 10. Key Takeaways

1. **Rich is necessary but not sufficient.** It handles rendering but not input or layout. Every production system combines it with something else.

2. **prompt_toolkit is the best choice for REPL-style CLIs.** It preserves native terminal scrollback, has best-in-class input handling, and is battle-tested (IPython, pgcli).

3. **Textual is the best choice for full-screen apps.** It has a modern widget system, CSS-like styling, and a growing ecosystem. But it owns the alt-buffer and breaks native terminal integration.

4. **Streaming is the hardest problem.** Flicker-free rendering requires synchronized output, double-buffering, or append-optimized fast paths. Rich `Live` works for simple cases; Textual's `Markdown.get_stream()` is better for complex markdown.

5. **Production agent CLIs converge on the same pattern:** scrollable chat, streaming markdown, collapsible tool calls, slash commands, session management, and a status bar.

6. **Start with prompt_toolkit + Rich, migrate to Textual later.** This preserves terminal-native behavior while building toward a full TUI.

7. **The `agentui` library (PyPI) is a new option** that provides agent-specific TUI primitives on top of Rich + prompt_toolkit. Worth evaluating for Phase 3.

---

## References

- [Rich documentation](https://rich.readthedocs.io/)
- [Textual documentation](https://textual.textualize.io/)
- [prompt_toolkit documentation](https://python-prompt-toolkit.readthedocs.io/)
- [Textual high-performance terminal apps](https://textual.textualize.io/blog/2024/12/12/algorithms-for-high-performance-terminal-apps/)
- [agentui — TUI primitives for coding agents](https://pypi.org/project/agentic-tui/)
- [Claude Code no-flicker mode](https://juliangoldieaiautomation.com/blog/claude-code-no-flicker-mode)
- [Flywheel — zero-flicker terminal compositor](https://github.com/0xchasercat/flywheel)
- [Hermes CLI deep dive](http://akjamie.github.io/post/2026-05-17-hermes-cli-deep-dive)
- [Python TUI libraries 2026 comparison](https://pistack.xyz/posts/2026-07-01-python-terminal-ui-libraries-textual-rich-prompt-toolkit-urwid)
- [TUI Renaissance 2026](https://labhub.hopto.org/blog/culture/2026-05-14-tui-development-ratatui-bubbletea-ink-textual-terminal-ui-renaissance-deep-dive-2026)
