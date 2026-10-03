"""Offline preview of the pi-style TUI (ah/cli/tui).

Plays a scripted, fake agent turn so you can see the look and the streaming
behaviour without PostgreSQL or an API key.

    .venv\\Scripts\\python.exe examples\\tui_preview.py            # dark theme
    .venv\\Scripts\\python.exe examples\\tui_preview.py --light    # light theme
    .venv\\Scripts\\python.exe examples\\tui_preview.py --fast     # no delays
"""
from __future__ import annotations

import argparse
import sys
import time

from ah.cli.tui.components import Editor, Loader, Spacer
from ah.cli.tui.component import Container
from ah.cli.tui.renderer import TuiMainScreen
from ah.cli.tui.terminal import ProcessTerminal
from ah.cli.tui.theme import get_theme
from ah.cli.tui.widgets import AssistantMessage, Footer, ToolExecution, UserMessage

USER_TEXT = "Find where tool results get cached and tell me if it can go stale."

REPLY = """## Tool result caching

The cache lives in `ah/tools/base.py` inside `ToolRegistry.execute`.

- **Read-only tools** are cached for 60 seconds, keyed by tool name + arguments.
- **Side-effect tools** (`write_*`, `terminal`, `remember`) are never cached
  and *flush* the cache when they run.

So a `read_file` after a `write_file` always sees the new content:

```
write_file(path="notes.md")  # clears cache
read_file(path="notes.md")   # fresh read
```

> Stale results are no longer possible for the common write-then-read pattern.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--light", action="store_true", help="use the light theme")
    parser.add_argument("--fast", action="store_true", help="skip the animation delays")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    theme = get_theme("light" if args.light else "dark")
    delay = 0.0 if args.fast else 1.0

    def pause(seconds: float) -> None:
        if delay:
            time.sleep(seconds * delay)

    terminal = ProcessTerminal()
    tui = TuiMainScreen(terminal)
    transcript = Container()
    editor = Editor(border_fn=lambda s: theme.style(s, "border"))
    footer = Footer(theme)
    footer.update(session_id="3f9a2c71d0e4", model="anthropic/claude-3.5-sonnet",
                  tokens=0, branch="main", mode="Auto")
    loader = Loader(
        "Thinking...",
        spinner_fn=lambda s: theme.style(s, "accent"),
        message_fn=lambda s: theme.style(s, "muted"),
    )

    for child in (transcript, Spacer(1), editor, footer):
        tui.add_child(child)
    tui.set_focus(editor)

    terminal.hide_cursor()
    try:
        tui.request_render()
        pause(0.6)

        # The user "types" their message into the editor, then submits it.
        for ch in USER_TEXT:
            editor.handle_input(ch)
            tui.request_render()
            pause(0.02)
        pause(0.4)
        editor.set_value("")
        transcript.add_child(UserMessage(USER_TEXT, theme))
        transcript.add_child(Spacer(1))

        # Spinner while the "model" thinks.
        loader.start()
        transcript.add_child(loader)
        for _ in range(15):
            loader.advance()
            tui.request_render()
            pause(0.08)

        # A tool call: running -> success.
        transcript.remove_child(loader)
        tool = ToolExecution("search_files", theme, 'pattern="_result_cache"')
        tool.set_running()
        transcript.add_child(tool)
        tui.request_render()
        pause(1.0)
        tool.set_result('ah/tools/base.py:48: self._result_cache: dict = {}\n'
                        'ah/tools/base.py:213: if cache_key in self._result_cache:')
        transcript.add_child(Spacer(1))
        footer.update(tokens=1840)
        tui.request_render()
        pause(0.6)

        # The assistant reply streams in word by word.
        reply = AssistantMessage("", theme)
        transcript.add_child(reply)
        for word in REPLY.split(" "):
            reply.append(word + " ")
            tui.request_render()
            pause(0.03)
        footer.update(tokens=2412)
        tui.request_render()
    finally:
        # Leave the cursor below the drawn block.
        if tui._cursor_rows_up:
            terminal.write(f"\x1b[{tui._cursor_rows_up}B")
        terminal.write("\r\n")
        terminal.show_cursor()


if __name__ == "__main__":
    main()
