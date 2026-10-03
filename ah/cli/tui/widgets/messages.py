"""Message cards for the transcript.

Adapted from earendil-works/pi (MIT). See ../LICENSE.pi.

``UserMessage`` renders the user's turn with a left accent bar and the themed
``userMessage*`` background; ``AssistantMessage`` renders streamed assistant
text as themed Markdown. Both wrap to the viewport width.
"""
from __future__ import annotations

from ah.cli.tui.components.markdown import Markdown
from ah.cli.tui.theme import Theme
from ah.cli.tui.utils import pad_to_width, wrap_text_with_ansi

__all__ = ["UserMessage", "AssistantMessage"]


class UserMessage:
    """A user turn: accent left bar + themed background block."""

    def __init__(self, text: str, theme: Theme) -> None:
        self._text = text
        self._theme = theme

    def render(self, width: int) -> list[str]:
        t = self._theme
        bar = t.style("▌", "accent")
        inner = max(1, width - 2)
        out: list[str] = []
        for wl in wrap_text_with_ansi(self._text, inner):
            body = t.style_bg(" " + pad_to_width(wl, inner), "userMessageBg")
            out.append(f"{bar}{body}")
        return out or [bar]

    def invalidate(self) -> None:
        pass


class AssistantMessage:
    """An assistant turn: themed Markdown, updated as tokens stream in."""

    def __init__(self, text: str, theme: Theme) -> None:
        self._md = Markdown(text, theme)
        self._text = text

    def set_text(self, text: str) -> None:
        self._text = text
        self._md.set_text(text)

    def append(self, delta: str) -> None:
        self._text += delta
        self._md.set_text(self._text)

    def render(self, width: int) -> list[str]:
        return self._md.render(width)

    def invalidate(self) -> None:
        self._md.invalidate()
