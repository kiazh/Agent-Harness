"""Tool-execution card for the transcript.

Adapted from earendil-works/pi (MIT). See ../LICENSE.pi.

Shows a tool call as a titled box whose background reflects state
(pending/success/error) using the theme's ``tool*Bg`` roles, with the tool name
as the title and an output preview below.
"""
from __future__ import annotations

from ah.cli.tui.theme import Theme
from ah.cli.tui.utils import pad_to_width, truncate_to_width, wrap_text_with_ansi

__all__ = ["ToolExecution"]

_STATE_BG = {
    "pending": "toolPendingBg",
    "running": "toolPendingBg",
    "success": "toolSuccessBg",
    "error": "toolErrorBg",
}
_STATE_ICON = {"pending": "○", "running": "◐", "success": "✓", "error": "✗"}


class ToolExecution:
    """A single tool call card. Update ``state``/``output`` then re-render."""

    def __init__(self, tool_name: str, theme: Theme, args: str = "") -> None:
        self.tool_name = tool_name
        self.args = args
        self.output = ""
        self.state = "pending"
        self._theme = theme

    def set_running(self) -> None:
        self.state = "running"

    def set_result(self, output: str, is_error: bool = False) -> None:
        self.output = output
        self.state = "error" if is_error else "success"

    def render(self, width: int) -> list[str]:
        t = self._theme
        bg_role = _STATE_BG.get(self.state, "toolPendingBg")
        icon = _STATE_ICON.get(self.state, "○")
        inner = max(1, width - 2)

        title = f"{icon} {self.tool_name}"
        if self.args:
            title += f" {self.args}"
        title_line = t.style_bg(
            " " + pad_to_width(truncate_to_width(t.style(title, "toolTitle", bold=True), inner - 1, ""), inner - 1),
            bg_role,
        )
        out = [title_line]

        if self.output:
            for wl in wrap_text_with_ansi(self.output, inner - 1)[:8]:
                line = t.style_bg(" " + pad_to_width(t.style(wl, "toolOutput"), inner - 1), bg_role)
                out.append(line)
        return out

    def invalidate(self) -> None:
        pass
