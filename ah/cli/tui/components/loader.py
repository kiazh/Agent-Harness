"""Animated loader (spinner) component.

Adapted from earendil-works/pi (MIT). See LICENSE.pi.

A frame-based spinner with a message. The owner advances frames (via a timer
or render loop) and calls the TUI's render request; ``render`` is pure.
"""
from __future__ import annotations

from typing import Callable

__all__ = ["Loader", "SPINNER_FRAMES"]

# Braille dot spinner (same glyph set pi uses).
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class Loader:
    """Spinner + message. Call :meth:`advance` to step the animation frame."""

    def __init__(
        self,
        message: str = "Loading...",
        spinner_fn: Callable[[str], str] | None = None,
        message_fn: Callable[[str], str] | None = None,
    ) -> None:
        self._message = message
        self._spinner_fn = spinner_fn or (lambda s: s)
        self._message_fn = message_fn or (lambda s: s)
        self._frame = 0
        self._active = False

    def start(self) -> None:
        self._active = True

    def stop(self) -> None:
        self._active = False

    def set_message(self, message: str) -> None:
        self._message = message

    def advance(self) -> None:
        self._frame = (self._frame + 1) % len(SPINNER_FRAMES)

    def render(self, width: int) -> list[str]:
        if not self._active:
            return [""]
        frame = SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]
        return [f"{self._spinner_fn(frame)} {self._message_fn(self._message)}"]

    def invalidate(self) -> None:
        pass
