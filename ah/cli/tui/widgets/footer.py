"""Footer / status line.

Adapted from earendil-works/pi (MIT). See ../LICENSE.pi.

A single truncated line showing session, model, token usage, git branch, and
mode indicators, styled with theme roles.
"""
from __future__ import annotations

from ah.cli.tui.theme import Theme
from ah.cli.tui.utils import truncate_to_width

__all__ = ["Footer"]


class Footer:
    """Compact status bar fed by live session state."""

    def __init__(self, theme: Theme) -> None:
        self._theme = theme
        self.session_id = ""
        self.model = ""
        self.tokens = 0
        self.branch = ""
        self.mode = ""

    def update(self, **kwargs) -> None:
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)

    def render(self, width: int) -> list[str]:
        t = self._theme
        parts: list[str] = []
        dot = t.style("●", "accent")
        if self.session_id:
            parts.append(f"{dot} {t.style('session', 'muted')} {t.style(self.session_id[:8], 'dim')}")
        else:
            parts.append(f"{dot} {t.style('no session', 'muted')}")
        if self.model:
            parts.append(f"{t.style('model', 'muted')} {t.style(self.model, 'dim')}")
        parts.append(f"{t.style('tokens', 'muted')} {t.style(f'{self.tokens:,}', 'dim')}")
        if self.branch:
            parts.append(f"{t.style('⎇', 'success')} {t.style(self.branch, 'dim')}")
        if self.mode:
            parts.append(t.style(self.mode, "accent"))
        line = t.style("  ", "muted").join(parts)
        return [truncate_to_width(line, width, "")]

    def invalidate(self) -> None:
        pass
