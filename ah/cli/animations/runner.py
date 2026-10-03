"""AnimationRunner and module-level convenience factories."""
from __future__ import annotations

import asyncio
import logging
import math
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Coroutine,
    Generator,
    Optional,
    Protocol,
    Sequence,
)

from rich.console import Console, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)

from ah.cli.animations._base import (
    DOTS_FRAMES,
    BOUNCE_FRAMES,
    GROW_FRAMES,
    ARROW_FRAMES,
    STAR_FRAMES,
    MOON_FRAMES,
    PULSE_FRAMES,
    BRAIN_FRAMES,
    SPARKLE_FRAMES,
    SPINNER_FRAMES,
    THINKING_VERBS,
    PRIMARY,
    SECONDARY,
    SUCCESS,
    WARNING,
    ERROR,
    INFO,
    MUTED,
    TEXT,
    is_tty,
    should_animate,
)
from ah.cli.animations.spinners import (
    FrameAnimation,
    Spinner,
    SquareLoader,
    ThinkingAnimation,
    KawaiiSpinner,
)
from ah.cli.animations.loaders import (
    ProgressBar,
    StreamingAnimation,
    ToolExecutionAnimation,
    ErrorAnimation,
    SuccessAnimation,
)
from ah.cli.animations.transitions import (
    FadeTransition,
    KnightRiderScanner,
    BackgroundPulse,
    FrameCache,
    FlashMessage,
    ScrollAccelerator,
)
# ─── AnimationRunner ─────────────────────────────────────────────────────────


class AnimationRunner:
    """Manage multiple concurrent animations.

    Allows starting, stopping, and updating multiple animations simultaneously.
    Each animation is registered with a unique key.

    Usage:
        runner = AnimationRunner(console)
        runner.add("spinner", Spinner("dots", console, "Loading..."))
        runner.add("progress", ProgressBar(console, total=100))
        runner.start_all()
        # ... do work ...
        runner.update("progress", 50)
        runner.stop_all()
    """

    def __init__(self, console: Console, enabled: bool | None = None) -> None:
        self.console = console
        self._enabled = enabled if enabled is not None else should_animate()
        self._animations: dict[str, FrameAnimation] = {}
        self._progress_bars: dict[str, ProgressBar] = {}
        self._streaming: dict[str, StreamingAnimation] = {}
        self._running = False

    def add(self, key: str, animation: FrameAnimation) -> None:
        """Add a frame-based animation."""
        self._animations[key] = animation

    def add_progress(self, key: str, bar: ProgressBar) -> None:
        """Add a progress bar."""
        self._progress_bars[key] = bar

    def add_streaming(self, key: str, stream: StreamingAnimation) -> None:
        """Add a streaming animation."""
        self._streaming[key] = stream

    def remove(self, key: str) -> None:
        """Remove and stop an animation by key."""
        if key in self._animations:
            self._animations[key].stop()
            del self._animations[key]
        if key in self._progress_bars:
            self._progress_bars[key].stop()
            del self._progress_bars[key]
        if key in self._streaming:
            self._streaming[key].stop()
            del self._streaming[key]

    def start_all(self) -> None:
        """Start all registered animations."""
        if not self._enabled:
            return
        self._running = True
        for anim in self._animations.values():
            anim.start()
        for bar in self._progress_bars.values():
            bar.start()
        for stream in self._streaming.values():
            stream.start()

    def stop_all(self) -> None:
        """Stop all registered animations."""
        self._running = False
        for anim in self._animations.values():
            anim.stop()
        for bar in self._progress_bars.values():
            bar.stop()
        for stream in self._streaming.values():
            stream.stop()

    def update(self, key: str, **kwargs: Any) -> None:
        """Update an animation by key.

        For ProgressBar: update(current=value)
        For StreamingAnimation: add_token(token=value) or set_text(text=value)
        For SquareLoader: set_progress(progress=value)
        """
        if key in self._progress_bars:
            if "current" in kwargs:
                self._progress_bars[key].update(kwargs["current"])
        elif key in self._streaming:
            if "token" in kwargs:
                self._streaming[key].add_token(kwargs["token"])
            elif "text" in kwargs:
                self._streaming[key].set_text(kwargs["text"])
        elif key in self._animations:
            anim = self._animations[key]
            if isinstance(anim, SquareLoader) and "progress" in kwargs:
                anim.set_progress(kwargs["progress"])

    def complete(self, key: str, **kwargs: Any) -> None:
        """Mark an animation as complete.

        For ProgressBar: complete()
        For SquareLoader: complete()
        For ToolExecutionAnimation: complete(duration=value)
        For StreamingAnimation: complete()
        """
        if key in self._progress_bars:
            self._progress_bars[key].complete()
        elif key in self._streaming:
            self._streaming[key].complete()
        elif key in self._animations:
            anim = self._animations[key]
            if isinstance(anim, SquareLoader):
                anim.complete()
            elif isinstance(anim, ToolExecutionAnimation):
                anim.complete(**kwargs)

    def __enter__(self) -> AnimationRunner:
        self.start_all()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.stop_all()


# ─── Convenience Functions ───────────────────────────────────────────────────


async def animate_spinner(
    console: Console,
    coro: Coroutine[Any, Any, Any],
    spinner_type: str = "dots",
    label: str = "Loading...",
) -> Any:
    """Run a coroutine with a spinner animation.

    Usage:
        result = await animate_spinner(console, fetch_data(), label="Fetching...")
    """
    spinner = Spinner(spinner_type, console, label)
    spinner.start()
    try:
        result = await coro
        return result
    finally:
        spinner.stop()


async def animate_progress(
    console: Console,
    coro: Coroutine[Any, Any, Any],
    total: int,
    label: str = "Processing",
) -> Any:
    """Run a coroutine with a progress bar.

    Usage:
        result = await animate_progress(console, process_items(), total=100)
    """
    bar = ProgressBar(console, total=total, label=label)
    bar.start()
    try:
        result = await coro
        bar.complete()
        return result
    except Exception:
        bar.stop()
        raise


# ─── Module-level convenience for quick access ───────────────────────────────


def get_spinner(
    console: Console,
    label: str = "",
    spinner_type: str = "dots",
) -> Spinner:
    """Quick factory for a Spinner."""
    return Spinner(spinner_type, console, label)


def get_progress_bar(
    console: Console,
    total: int,
    label: str = "",
) -> ProgressBar:
    """Quick factory for a ProgressBar."""
    return ProgressBar(console, total=total, label=label)


def get_thinking_animation(
    console: Console,
    spinner_type: str = "dots",
) -> ThinkingAnimation:
    """Quick factory for a ThinkingAnimation."""
    return ThinkingAnimation(console, spinner_type=spinner_type)


def get_streaming_animation(
    console: Console,
    style: str = "green",
) -> StreamingAnimation:
    """Quick factory for a StreamingAnimation."""
    return StreamingAnimation(console, style=style)


def get_tool_animation(
    console: Console,
    tool_name: str,
    spinner_type: str = "dots",
) -> ToolExecutionAnimation:
    """Quick factory for a ToolExecutionAnimation."""
    return ToolExecutionAnimation(console, tool_name, spinner_type=spinner_type)


def get_error_animation(
    console: Console,
    message: str,
    suggestion: str = "",
) -> ErrorAnimation:
    """Quick factory for an ErrorAnimation."""
    return ErrorAnimation(console, message, suggestion=suggestion)


def get_success_animation(
    console: Console,
    message: str,
    show_panel: bool = False,
) -> SuccessAnimation:
    """Quick factory for a SuccessAnimation."""
    return SuccessAnimation(console, message, show_panel=show_panel)


def get_fade_transition(
    console: Console,
    duration: float = 0.3,
) -> FadeTransition:
    """Quick factory for a FadeTransition."""
    return FadeTransition(console, duration=duration)


def get_animation_runner(console: Console) -> AnimationRunner:
    """Quick factory for an AnimationRunner."""
    return AnimationRunner(console)

def get_knight_rider_scanner(
    console: Console,
    label: str = "",
    width: int = 30,
) -> KnightRiderScanner:
    """Quick factory for a KnightRiderScanner."""
    return KnightRiderScanner(console, label=label, width=width)


def get_background_pulse(
    console: Console,
    logo: str = "AH",
) -> BackgroundPulse:
    """Quick factory for a BackgroundPulse."""
    return BackgroundPulse(console, logo=logo)


def get_kawaii_spinner(
    console: Console,
    label: str = "",
    wings: str = "",
) -> KawaiiSpinner:
    """Quick factory for a KawaiiSpinner."""
    return KawaiiSpinner(console, label=label, wings=wings)


def get_flash_message(
    console: Console,
    message: str,
    duration: float = 2.0,
) -> FlashMessage:
    """Quick factory for a FlashMessage."""
    return FlashMessage(console, message, duration=duration)


def get_scroll_accelerator() -> ScrollAccelerator:
    """Quick factory for a ScrollAccelerator."""
    return ScrollAccelerator()

