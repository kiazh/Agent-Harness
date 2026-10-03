"""Basic display components: Text, TruncatedText, Spacer, Box.

Adapted from earendil-works/pi (MIT). See LICENSE.pi.
"""
from __future__ import annotations

from typing import Callable

from ah.cli.tui.utils import pad_to_width, truncate_to_width, wrap_text_with_ansi

__all__ = ["Text", "TruncatedText", "Spacer", "Box"]


class Text:
    """Multi-line text with word wrapping and optional padding."""

    def __init__(self, text: str = "", padding_x: int = 0, padding_y: int = 0) -> None:
        self._text = text
        self._px = padding_x
        self._py = padding_y
        self._cache_w: int | None = None
        self._cache: list[str] | None = None

    def set_text(self, text: str) -> None:
        self._text = text
        self.invalidate()

    def render(self, width: int) -> list[str]:
        if self._cache is not None and self._cache_w == width:
            return self._cache
        inner = max(1, width - 2 * self._px)
        body = wrap_text_with_ansi(self._text, inner)
        pad = " " * self._px
        lines = [pad + pad_to_width(l, inner) + pad for l in body]
        if self._py:
            blank = " " * width
            lines = [blank] * self._py + lines + [blank] * self._py
        self._cache, self._cache_w = lines, width
        return lines

    def invalidate(self) -> None:
        self._cache = None
        self._cache_w = None


class TruncatedText:
    """Single-line text truncated to the viewport width (status lines, headers)."""

    def __init__(self, text: str = "", padding_x: int = 0) -> None:
        self._text = text
        self._px = padding_x

    def set_text(self, text: str) -> None:
        self._text = text

    def render(self, width: int) -> list[str]:
        inner = max(1, width - 2 * self._px)
        pad = " " * self._px
        return [pad + truncate_to_width(self._text, inner) + pad]

    def invalidate(self) -> None:
        pass


class Spacer:
    """Empty vertical space of *lines* rows."""

    def __init__(self, lines: int = 1) -> None:
        self._lines = max(0, lines)

    def render(self, width: int) -> list[str]:
        return ["" for _ in range(self._lines)]

    def invalidate(self) -> None:
        pass


class Box:
    """Wraps a child, applying horizontal/vertical padding and an optional
    per-line background/style function."""

    def __init__(
        self,
        child,
        padding_x: int = 1,
        padding_y: int = 1,
        style_fn: Callable[[str], str] | None = None,
    ) -> None:
        self._child = child
        self._px = padding_x
        self._py = padding_y
        self._style_fn = style_fn

    def set_style_fn(self, fn: Callable[[str], str] | None) -> None:
        self._style_fn = fn

    def render(self, width: int) -> list[str]:
        inner = max(1, width - 2 * self._px)
        child_lines = self._child.render(inner)
        pad = " " * self._px
        out: list[str] = []
        blank = " " * width
        for _ in range(self._py):
            out.append(blank)
        for l in child_lines:
            line = pad + pad_to_width(l, inner) + pad
            out.append(line)
        for _ in range(self._py):
            out.append(blank)
        if self._style_fn is not None:
            out = [self._style_fn(l) for l in out]
        return out

    def invalidate(self) -> None:
        self._child.invalidate()
