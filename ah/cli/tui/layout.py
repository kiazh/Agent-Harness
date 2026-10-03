"""Layout components for the alternate-screen viewport.

Adapted from earendil-works/pi (MIT). See LICENSE.pi.

``VStack``/``HStack`` allocate constrained regions with a flexbox-like solver
(``basis``/``grow``/``shrink``/``min_size``/``max_size``); ``ScrollView`` owns
scrolling for one region and can follow streaming output at the bottom.
"""
from __future__ import annotations

from dataclasses import dataclass

from ah.cli.tui.component import Component
from ah.cli.tui.utils import pad_to_width, truncate_to_width, visible_width

__all__ = ["StackEntry", "VStack", "HStack", "ScrollView"]


@dataclass
class StackEntry:
    """One child in a stack with flex sizing constraints.

    ``basis`` is the preferred size: an int (fixed rows/cols) or ``"auto"``
    (use the child's natural size). ``grow`` distributes leftover space;
    ``shrink`` absorbs overflow; ``min_size``/``max_size`` clamp the result.
    """

    component: Component
    basis: "int | str" = "auto"
    grow: float = 0.0
    shrink: float = 1.0
    min_size: int = 0
    max_size: "int | None" = None


def _solve_flex(entries: list[StackEntry], total: int, natural: list[int]) -> list[int]:
    """Resolve each entry's size along the main axis to fill *total*."""
    n = len(entries)
    sizes: list[int] = []
    for e, nat in zip(entries, natural):
        base = nat if e.basis == "auto" else int(e.basis)
        sizes.append(max(e.min_size, base))

    used = sum(sizes)
    if used < total:
        grow_total = sum(e.grow for e in entries)
        if grow_total > 0:
            extra = total - used
            for i, e in enumerate(entries):
                if e.grow > 0:
                    add = round(extra * (e.grow / grow_total))
                    sizes[i] += add
    elif used > total:
        shrink_total = sum(e.shrink for e in entries)
        if shrink_total > 0:
            over = used - total
            for i, e in enumerate(entries):
                if e.shrink > 0:
                    cut = round(over * (e.shrink / shrink_total))
                    sizes[i] = max(e.min_size, sizes[i] - cut)

    # Clamp to max_size and fix rounding drift against the total.
    for i, e in enumerate(entries):
        if e.max_size is not None:
            sizes[i] = min(sizes[i], e.max_size)
    drift = total - sum(sizes)
    if drift != 0 and n > 0:
        # Apply drift to the first growable (or last) entry.
        idx = next((i for i, e in enumerate(entries) if e.grow > 0), n - 1)
        sizes[idx] = max(0, sizes[idx] + drift)
    return sizes


class VStack:
    """Vertical stack: children occupy row bands summing to the viewport height."""

    def __init__(self, entries: list[StackEntry], height: int | None = None) -> None:
        self._entries = entries
        self._height = height

    def set_height(self, height: int) -> None:
        self._height = height

    def render(self, width: int) -> list[str]:
        natural = [len(e.component.render(width)) for e in self._entries]
        if self._height is None:
            # Unbounded: just stack children.
            out: list[str] = []
            for e in self._entries:
                out.extend(e.component.render(width))
            return out
        sizes = _solve_flex(self._entries, self._height, natural)
        out = []
        for e, size in zip(self._entries, sizes):
            lines = e.component.render(width)
            if len(lines) > size:
                lines = lines[:size]
            elif len(lines) < size:
                lines = lines + [""] * (size - len(lines))
            out.extend(lines)
        return out[: self._height]

    def invalidate(self) -> None:
        for e in self._entries:
            e.component.invalidate()


class HStack:
    """Horizontal stack: children occupy column bands summing to the width."""

    def __init__(self, entries: list[StackEntry], gap: int = 0) -> None:
        self._entries = entries
        self._gap = gap

    def render(self, width: int) -> list[str]:
        gaps = self._gap * (len(self._entries) - 1) if self._entries else 0
        avail = max(0, width - gaps)
        natural = [max((visible_width(l) for l in e.component.render(avail)), default=0)
                   for e in self._entries]
        sizes = _solve_flex(self._entries, avail, natural)

        rendered = [e.component.render(size) for e, size in zip(self._entries, sizes)]
        height = max((len(r) for r in rendered), default=0)
        rows: list[str] = []
        for row_i in range(height):
            parts: list[str] = []
            for col_i, (lines, size) in enumerate(zip(rendered, sizes)):
                cell = lines[row_i] if row_i < len(lines) else ""
                parts.append(pad_to_width(truncate_to_width(cell, size, ""), size))
                if col_i < len(self._entries) - 1:
                    parts.append(" " * self._gap)
            rows.append("".join(parts))
        return rows

    def invalidate(self) -> None:
        for e in self._entries:
            e.component.invalidate()


class ScrollView:
    """A scrollable viewport over a single child component.

    With ``follow="end"`` it tracks the bottom as content grows (streaming),
    unless the user has scrolled up. ``viewport_height`` is set by the parent
    layout each frame.
    """

    def __init__(self, child: Component, *, follow: str = "end", viewport_height: int = 0) -> None:
        self._child = child
        self._follow = follow
        self.viewport_height = viewport_height
        self._scroll = 0
        self._pinned_to_end = follow == "end"

    def set_viewport_height(self, h: int) -> None:
        self.viewport_height = h

    def scroll_by(self, delta: int) -> None:
        self._scroll = max(0, self._scroll + delta)
        self._pinned_to_end = False

    def scroll_to_end(self) -> None:
        self._pinned_to_end = True

    def render(self, width: int) -> list[str]:
        lines = self._child.render(width)
        h = self.viewport_height or len(lines)
        max_scroll = max(0, len(lines) - h)
        if self._pinned_to_end:
            self._scroll = max_scroll
        else:
            self._scroll = min(self._scroll, max_scroll)
        window = lines[self._scroll:self._scroll + h]
        if len(window) < h:
            window = window + [""] * (h - len(window))
        return window

    def invalidate(self) -> None:
        self._child.invalidate()
