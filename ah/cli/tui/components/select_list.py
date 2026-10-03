"""Interactive selection list with keyboard navigation and filtering.

Adapted from earendil-works/pi (MIT). See LICENSE.pi.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ah.cli.tui.keys import parse_key
from ah.cli.tui.utils import truncate_to_width

__all__ = ["SelectItem", "SelectList"]


@dataclass
class SelectItem:
    value: str
    label: str
    description: str = ""


class SelectList:
    """A filterable, arrow-key-navigable list.

    ``selected_fn``/``desc_fn`` style the current row and descriptions.
    """

    def __init__(
        self,
        items: list[SelectItem],
        max_visible: int = 8,
        selected_fn: Callable[[str], str] | None = None,
        desc_fn: Callable[[str], str] | None = None,
    ) -> None:
        self._items = items
        self._max = max_visible
        self._selected = 0
        self._filter = ""
        self._selected_fn = selected_fn or (lambda s: s)
        self._desc_fn = desc_fn or (lambda s: s)
        self.focused = True
        self.on_select: Callable[[SelectItem], None] | None = None
        self.on_cancel: Callable[[], None] | None = None

    def _filtered(self) -> list[SelectItem]:
        if not self._filter:
            return self._items
        f = self._filter.lower()
        return [it for it in self._items if f in it.label.lower() or f in it.value.lower()]

    def set_filter(self, text: str) -> None:
        self._filter = text
        self._selected = 0

    def render(self, width: int) -> list[str]:
        items = self._filtered()
        if not items:
            return [truncate_to_width("  (no matches)", width, "")]
        # Window around the selection.
        start = max(0, min(self._selected - self._max // 2, len(items) - self._max))
        start = max(0, start)
        window = items[start:start + self._max]
        lines: list[str] = []
        for i, it in enumerate(window):
            idx = start + i
            prefix = "▶ " if idx == self._selected else "  "
            label = f"{prefix}{it.label}"
            if it.description:
                label += "  " + self._desc_fn(it.description)
            if idx == self._selected:
                label = self._selected_fn(f"{prefix}{it.label}") + (
                    "  " + self._desc_fn(it.description) if it.description else ""
                )
            lines.append(truncate_to_width(label, width, ""))
        return lines

    def handle_input(self, data: str) -> None:
        key = parse_key(data)
        items = self._filtered()
        if key == "up":
            self._selected = max(0, self._selected - 1)
        elif key == "down":
            self._selected = min(len(items) - 1, self._selected + 1)
        elif key == "enter":
            if items and self.on_select:
                self.on_select(items[self._selected])
        elif key in ("escape", "ctrl+c"):
            if self.on_cancel:
                self.on_cancel()

    def invalidate(self) -> None:
        pass
