"""Differential renderer for the TUI.

Adapted from earendil-works/pi (MIT). See LICENSE.pi.

``TuiMainScreen`` renders a component tree into the main terminal buffer using
three strategies — first render, full redraw on width change, and a line-diff
normal update — all wrapped in synchronized output (CSI 2026) for flicker-free
atomic repaints.
"""
from __future__ import annotations


from ah.cli.tui.component import CURSOR_MARKER, Component, is_focusable
from ah.cli.tui.terminal import SYNC_BEGIN, SYNC_END, ProcessTerminal, Terminal
from ah.cli.tui.utils import truncate_to_width, visible_width

__all__ = ["TuiMainScreen"]


class TuiMainScreen:
    """Main-screen renderer with differential line updates.

    Owns a root component list, a focus target, and the last-rendered frame for
    diffing. Call :meth:`request_render` to repaint; it only rewrites lines that
    changed since the previous frame.
    """

    def __init__(self, terminal: Terminal | None = None) -> None:
        self.terminal: Terminal = terminal or ProcessTerminal()
        self._children: list[Component] = []
        self._focus: Component | None = None
        self._prev_lines: list[str] = []
        self._prev_width: int = -1
        self._first_render = True
        self._running = False
        # Rows the hardware cursor sits above the block's last line (set by
        # _place_cursor, undone at the start of the next render).
        self._cursor_rows_up = 0

    # ─── tree / focus ────────────────────────────────────────────────────
    def add_child(self, child: Component) -> None:
        self._children.append(child)

    def remove_child(self, child: Component) -> None:
        try:
            self._children.remove(child)
        except ValueError:
            pass

    def set_focus(self, component: Component | None) -> None:
        # Clear previous focus flag.
        if self._focus is not None and is_focusable(self._focus):
            self._focus.focused = False  # type: ignore[attr-defined]
        self._focus = component
        if component is not None and is_focusable(component):
            component.focused = True  # type: ignore[attr-defined]

    # ─── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        self._running = True
        self.request_render()

    def stop(self) -> None:
        self._running = False
        if isinstance(self.terminal, ProcessTerminal):
            self.terminal.stop()

    # ─── input ───────────────────────────────────────────────────────────
    def handle_input(self, data: str) -> None:
        if self._focus is not None:
            handler = getattr(self._focus, "handle_input", None)
            if handler is not None:
                handler(data)
        self.request_render()

    # ─── rendering ─────────────────────────────────────────────────────────
    def _compose(self, width: int) -> list[str]:
        lines: list[str] = []
        for child in self._children:
            for line in child.render(width):
                # Enforce the width contract defensively.
                if visible_width(line) > width:
                    line = truncate_to_width(line, width, "")
                lines.append(line)
        return lines

    def request_render(self) -> None:
        width = self.terminal.columns
        lines = self._compose(width)

        # _place_cursor() may have parked the cursor above the bottom of the
        # block (at the editor). Every strategy below assumes the cursor sits
        # on the last drawn line, so move it back down first.
        if self._cursor_rows_up > 0:
            self.terminal.write(f"\x1b[{self._cursor_rows_up}B\r")
            self._cursor_rows_up = 0

        if self._first_render:
            self._render_first(lines)
            self._first_render = False
        elif width != self._prev_width:
            self._render_full(lines)
        else:
            self._render_diff(lines)

        self._prev_lines = lines
        self._prev_width = width
        self._place_cursor(lines, width)

    def _render_first(self, lines: list[str]) -> None:
        out = SYNC_BEGIN + "\r"
        out += "\r\n".join(_visible_cursor_stripped(l) for l in lines)
        out += SYNC_END
        self.terminal.write(out)

    def _render_full(self, lines: list[str]) -> None:
        # Move to the top of the previously drawn block, clear, redraw.
        out = SYNC_BEGIN
        if self._prev_lines:
            out += f"\x1b[{len(self._prev_lines) - 1}A" if len(self._prev_lines) > 1 else ""
            out += "\r\x1b[0J"
        out += "\r\n".join(_visible_cursor_stripped(l) for l in lines)
        out += SYNC_END
        self.terminal.write(out)

    def _render_diff(self, lines: list[str]) -> None:
        prev = self._prev_lines
        # Find first changed line.
        first = 0
        while first < len(prev) and first < len(lines) and prev[first] == lines[first]:
            first += 1
        if first == len(prev) == len(lines):
            return  # nothing changed

        total_prev = len(prev)
        # Move cursor from the bottom of the previous block up to `first`.
        up = (total_prev - 1) - first
        out = SYNC_BEGIN
        if up > 0:
            out += f"\x1b[{up}A"
        out += "\r\x1b[0J"  # clear from first changed line to end
        out += "\r\n".join(_visible_cursor_stripped(l) for l in lines[first:])
        out += SYNC_END
        self.terminal.write(out)

    def _place_cursor(self, lines: list[str], width: int) -> None:
        """Position the hardware cursor at CURSOR_MARKER if the focus wants it."""
        for row, line in enumerate(lines):
            idx = line.find(CURSOR_MARKER)
            if idx != -1:
                col = visible_width(line[:idx])
                # Move to bottom then up to the marker row, over to the column.
                up = (len(lines) - 1) - row
                seq = "\r"
                if up > 0:
                    seq += f"\x1b[{up}A"
                if col > 0:
                    seq += f"\x1b[{col}C"
                self.terminal.write(seq)
                self._cursor_rows_up = max(0, up)
                if isinstance(self.terminal, ProcessTerminal):
                    self.terminal.show_cursor()
                return


def _visible_cursor_stripped(line: str) -> str:
    """Remove the zero-width CURSOR_MARKER before writing (it is not drawable)."""
    return line.replace(CURSOR_MARKER, "")
