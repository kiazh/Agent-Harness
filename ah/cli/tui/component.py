"""Component protocol for the TUI.

Adapted from earendil-works/pi (MIT). See LICENSE.pi.

Every component implements ``render(width) -> list[str]`` returning one string
per line, where each line's visible width must not exceed ``width``. Optional
``handle_input(data)`` receives raw terminal input when focused. ``invalidate()``
clears any cached render state.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = [
    "Component",
    "Container",
    "Focusable",
    "is_focusable",
    "CURSOR_MARKER",
]

# Zero-width APC escape sequence marking where the hardware cursor should sit.
# The renderer scans rendered output for this and positions the real cursor,
# while components draw their own visible (inverse-video) cursor.
CURSOR_MARKER = "\x1b_pi-cursor\x1b\\"


@runtime_checkable
class Component(Protocol):
    """Minimal rendering unit."""

    def render(self, width: int) -> list[str]:
        """Return lines (each visible width <= ``width``)."""
        ...

    def invalidate(self) -> None:
        """Clear cached render state so the next render recomputes."""
        ...


@runtime_checkable
class Focusable(Protocol):
    """A component that owns a text cursor and can receive focus.

    The renderer sets ``focused`` and looks for :data:`CURSOR_MARKER` in the
    component's output to place the hardware cursor (for IME support).
    """

    focused: bool

    def render(self, width: int) -> list[str]: ...
    def invalidate(self) -> None: ...


def is_focusable(obj: object) -> bool:
    return hasattr(obj, "focused")


class Container:
    """Groups child components, rendering them top-to-bottom.

    Serves as a base for higher-level widgets. Children are rendered in order
    and their line lists concatenated.
    """

    def __init__(self) -> None:
        self._children: list[Component] = []

    @property
    def children(self) -> list[Component]:
        return list(self._children)

    def add_child(self, child: Component) -> None:
        self._children.append(child)
        self.invalidate()

    def remove_child(self, child: Component) -> None:
        try:
            self._children.remove(child)
        except ValueError:
            pass
        self.invalidate()

    def clear(self) -> None:
        self._children.clear()
        self.invalidate()

    def render(self, width: int) -> list[str]:
        lines: list[str] = []
        for child in self._children:
            lines.extend(child.render(width))
        return lines

    def handle_input(self, data: str) -> None:
        for child in self._children:
            handler = getattr(child, "handle_input", None)
            if handler is not None:
                handler(data)
                return

    def invalidate(self) -> None:
        for child in self._children:
            child.invalidate()
