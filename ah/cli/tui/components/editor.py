"""Text input components: Input (single line) and Editor (multiline).

Adapted from earendil-works/pi (MIT). See LICENSE.pi.

Both draw a fake inverse-video cursor and emit CURSOR_MARKER so the renderer
can position the hardware cursor for IME. Key bindings follow pi's defaults
(Ctrl+A/E/W/U/K, word navigation, arrows, Enter to submit).
"""
from __future__ import annotations

from typing import Callable

from ah.cli.tui.component import CURSOR_MARKER
from ah.cli.tui.keys import parse_key
from ah.cli.tui.utils import truncate_to_width, wrap_text_with_ansi

__all__ = ["Input", "Editor"]


def _render_with_cursor(before: str, at: str, after: str, focused: bool) -> str:
    """Render a line with a fake inverse cursor at *at* when focused."""
    if not focused:
        return before + at + after
    cursor_char = at if at else " "
    return f"{before}{CURSOR_MARKER}\x1b[7m{cursor_char}\x1b[27m{after}"


class Input:
    """Single-line text input with horizontal scrolling."""

    def __init__(self, value: str = "", prompt: str = "") -> None:
        self._value = value
        self._cursor = len(value)
        self._prompt = prompt
        self.focused = False
        self.on_submit: Callable[[str], None] | None = None
        self.on_change: Callable[[str], None] | None = None

    def get_value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value
        self._cursor = len(value)

    def render(self, width: int) -> list[str]:
        p = self._prompt
        before = self._value[: self._cursor]
        at = self._value[self._cursor: self._cursor + 1]
        after = self._value[self._cursor + 1:]
        line = p + _render_with_cursor(before, at, after, self.focused)
        return [truncate_to_width(line, width, "")]

    def handle_input(self, data: str) -> None:
        key = parse_key(data)
        if key == "enter":
            if self.on_submit:
                self.on_submit(self._value)
        elif key == "backspace":
            if self._cursor > 0:
                self._value = self._value[: self._cursor - 1] + self._value[self._cursor:]
                self._cursor -= 1
                self._changed()
        elif key == "delete":
            if self._cursor < len(self._value):
                self._value = self._value[: self._cursor] + self._value[self._cursor + 1:]
                self._changed()
        elif key == "left":
            self._cursor = max(0, self._cursor - 1)
        elif key == "right":
            self._cursor = min(len(self._value), self._cursor + 1)
        elif key in ("home", "ctrl+a"):
            self._cursor = 0
        elif key in ("end", "ctrl+e"):
            self._cursor = len(self._value)
        elif key == "ctrl+u":
            self._value = self._value[self._cursor:]
            self._cursor = 0
            self._changed()
        elif key == "ctrl+k":
            self._value = self._value[: self._cursor]
            self._changed()
        elif key in ("ctrl+w", "alt+backspace"):
            self._delete_word_back()
        elif len(data) == 1 and data.isprintable():
            self._value = self._value[: self._cursor] + data + self._value[self._cursor:]
            self._cursor += 1
            self._changed()

    def _delete_word_back(self) -> None:
        i = self._cursor
        while i > 0 and self._value[i - 1].isspace():
            i -= 1
        while i > 0 and not self._value[i - 1].isspace():
            i -= 1
        self._value = self._value[:i] + self._value[self._cursor:]
        self._cursor = i
        self._changed()

    def _changed(self) -> None:
        if self.on_change:
            self.on_change(self._value)

    def invalidate(self) -> None:
        pass


class Editor:
    """Multi-line editor with a top/bottom rule, fake cursor, and word wrap.

    ``border_fn`` styles the horizontal rules (e.g. theme border color).
    Enter submits; Alt+Enter inserts a newline.
    """

    def __init__(
        self,
        value: str = "",
        border_fn: Callable[[str], str] | None = None,
        padding_x: int = 0,
    ) -> None:
        self._value = value
        self._cursor = len(value)
        self._border_fn = border_fn or (lambda s: s)
        self._px = padding_x
        self.focused = False
        self.on_submit: Callable[[str], None] | None = None
        self.on_change: Callable[[str], None] | None = None

    def get_value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value
        self._cursor = len(value)

    def _rule(self, width: int) -> str:
        return self._border_fn("─" * width)

    def render(self, width: int) -> list[str]:
        inner = max(1, width - 2 * self._px)
        pad = " " * self._px
        before = self._value[: self._cursor]
        at = self._value[self._cursor: self._cursor + 1]
        after = self._value[self._cursor + 1:]
        text = _render_with_cursor(before, at, (at + after)[1:] if at else after, self.focused)
        # Wrap the full logical text (cursor markup is zero-width to the wrapper).
        body = wrap_text_with_ansi(text if self._value or self.focused else "", inner) or [""]
        lines = [self._rule(width)]
        for l in body:
            lines.append(pad + l + pad)
        lines.append(self._rule(width))
        return lines

    def handle_input(self, data: str) -> None:
        key = parse_key(data)
        if key in ("alt+enter", "ctrl+enter", "shift+enter"):
            self._insert("\n")
        elif key == "enter":
            if self.on_submit:
                self.on_submit(self._value)
        elif key == "backspace":
            if self._cursor > 0:
                self._value = self._value[: self._cursor - 1] + self._value[self._cursor:]
                self._cursor -= 1
                self._changed()
        elif key == "left":
            self._cursor = max(0, self._cursor - 1)
        elif key == "right":
            self._cursor = min(len(self._value), self._cursor + 1)
        elif key in ("home", "ctrl+a"):
            self._cursor = 0
        elif key in ("end", "ctrl+e"):
            self._cursor = len(self._value)
        elif key == "ctrl+u":
            self._value = self._value[self._cursor:]
            self._cursor = 0
            self._changed()
        elif key == "ctrl+k":
            self._value = self._value[: self._cursor]
            self._changed()
        elif len(data) == 1 and data.isprintable():
            self._insert(data)

    def _insert(self, s: str) -> None:
        self._value = self._value[: self._cursor] + s + self._value[self._cursor:]
        self._cursor += len(s)
        self._changed()

    def _changed(self) -> None:
        if self.on_change:
            self.on_change(self._value)

    def invalidate(self) -> None:
        pass
