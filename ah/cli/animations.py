"""
Comprehensive animation library for AgentHarness CLI.

Provides spinners, loaders, progress bars, streaming effects, transitions,
and animation management — all TTY-aware and Rich Live compatible.

Usage:
    from ah.cli.animations import Spinner, ProgressBar, AnimationRunner

    # Simple spinner
    with Spinner("dots", console, "Loading...") as spin:
        spin.start()
        # ... do work ...
        spin.stop()

    # Progress bar
    with ProgressBar(console, total=100) as bar:
        for i in range(100):
            bar.update(i + 1)

    # Concurrent animations
    runner = AnimationRunner(console)
    runner.add("main", Spinner("dots", console, "Working..."))
    runner.add("sub", SquareLoader(console, "Processing..."))
    runner.start_all()
    # ... do work ...
    runner.stop_all()
"""
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

# ─── TTY Detection ───────────────────────────────────────────────────────────


def is_tty() -> bool:
    """Check if stdout is a TTY (terminal)."""
    return sys.stdout.isatty()


def should_animate() -> bool:
    """Determine if animations should be shown.

    Returns True only when stdout is a TTY and NO_COLOR is not set.
    """
    import os

    if os.environ.get("NO_COLOR"):
        return False
    return is_tty()


# ─── Color Constants ─────────────────────────────────────────────────────────

PRIMARY = "#00D4FF"
SECONDARY = "#7C3AED"
SUCCESS = "#10B981"
WARNING = "#F59E0B"
ERROR = "#EF4444"
INFO = "#3B82F6"
MUTED = "#6B7280"
TEXT = "#E5E7EB"


# ─── Frame Definitions ───────────────────────────────────────────────────────

DOTS_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
BOUNCE_FRAMES = ["( ●    )", "(  ●   )", "(   ●  )", "(    ● )", "(     ●)",
                 "(    ● )", "(   ●  )", "(  ●   )", "( ●    )", "(●     )"]
GROW_FRAMES = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█", "▇", "▆", "▅", "▄", "▃", "▂"]
ARROW_FRAMES = ["←", "↖", "↑", "↗", "→", "↘", "↓", "↙"]
STAR_FRAMES = ["✦", "✧", "★", "✦", "✧", "☆", "✦", "✧", "★", "✦"]
MOON_FRAMES = ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"]
PULSE_FRAMES = ["○", "◔", "◑", "◕", "●", "◕", "◑", "◔"]
BRAIN_FRAMES = ["🧠", "💭", "🧠", "💡", "🧠", "⚡", "🧠", "🔮"]
SPARKLE_FRAMES = ["✧", "✦", "✨", "✦", "✧", "⭐", "✧", "✦", "✨", "✦"]

SPINNER_FRAMES: dict[str, list[str]] = {
    "dots": DOTS_FRAMES,
    "bounce": BOUNCE_FRAMES,
    "grow": GROW_FRAMES,
    "arrows": ARROW_FRAMES,
    "star": STAR_FRAMES,
    "moon": MOON_FRAMES,
    "pulse": PULSE_FRAMES,
    "brain": BRAIN_FRAMES,
    "sparkle": SPARKLE_FRAMES,
}

THINKING_VERBS = [
    "Thinking",
    "Processing",
    "Analyzing",
    "Reasoning",
    "Computing",
    "Evaluating",
    "Synthesizing",
    "Deducing",
    "Inferring",
    "Calculating",
]


# ─── FrameAnimation Base Class ───────────────────────────────────────────────


class FrameAnimation(ABC):
    """Abstract base class for all frame-based animations.

    Subclasses must define `frames` (list of frame strings) and can override
    `interval` (seconds between frames, default 0.08).

    The animation is TTY-aware: when not a TTY or NO_COLOR is set, `render()`
    returns a static string instead of animated frames.
    """

    def __init__(
        self,
        console: Console,
        label: str = "",
        interval: float = 0.08,
        style: str = "cyan",
        enabled: bool | None = None,
    ) -> None:
        self.console = console
        self.label = label
        self.interval = interval
        self.style = style
        self._frame_index = 0
        self._running = False
        self._task: asyncio.Task | None = None
        self._live: Live | None = None
        self._enabled = enabled if enabled is not None else should_animate()

    @property
    @abstractmethod
    def frames(self) -> list[str]:
        """Return the list of animation frame strings."""
        ...

    @property
    def current_frame(self) -> str:
        """Get the current frame string."""
        if not self.frames:
            return ""
        return self.frames[self._frame_index % len(self.frames)]

    def advance(self) -> None:
        """Advance to the next frame."""
        if self.frames:
            self._frame_index = (self._frame_index + 1) % len(self.frames)

    def reset(self) -> None:
        """Reset animation to the first frame."""
        self._frame_index = 0

    def render(self) -> RenderableType:
        """Render the current animation state as a Rich renderable.

        When animations are disabled (non-TTY or NO_COLOR), returns a static
        string. Otherwise returns a Rich Text object with styling.
        """
        if not self._enabled:
            return Text(self.label, style=self.style) if self.label else Text("")

        frame = self.current_frame
        if self.label:
            text = Text()
            text.append(frame + " ", style=self.style)
            text.append(self.label, style=self.style)
            return text
        return Text(frame, style=self.style)

    async def _animate_loop(self) -> None:
        """Internal animation loop for use with Rich Live."""
        while self._running:
            self.advance()
            if self._live:
                self._live.update(self.render())
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        """Start the animation."""
        if not self._enabled:
            return
        if self._running:
            return
        self._running = True
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._animate_loop())
        except RuntimeError:
            # No running event loop — animation won't advance but won't crash
            logger.debug("No running event loop for animation start")

    def stop(self) -> None:
        """Stop the animation."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._live:
            self._live.stop()
            self._live = None

    def __enter__(self) -> FrameAnimation:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.stop()


# ─── Spinner ─────────────────────────────────────────────────────────────────


class Spinner(FrameAnimation):
    """Animated spinner with 9 frame types.

    Frame types:
        - dots:    Braille dot spinner (⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏)
        - bounce:  Bouncing ball in parentheses
        - grow:    Growing bar (▁▂▃▄▅▆▇█)
        - arrows:  Rotating arrows (←↖↑↗→↘↓↙)
        - star:    Star twinkle (✦✧★)
        - moon:    Moon phases (🌑🌒🌓🌔🌕🌖🌗🌘)
        - pulse:   Pulsing circle (○◔◑◕●)
        - brain:   Brain/thinking emoji cycle
        - sparkle: Sparkle shimmer (✧✦✨⭐)

    All spinners default to 80ms intervals.

    Usage:
        spinner = Spinner("dots", console, "Loading...")
        spinner.start()
        # ... work ...
        spinner.stop()

    Or as context manager:
        with Spinner("dots", console, "Loading...") as spin:
            # ... work ...
    """

    def __init__(
        self,
        frame_type: str,
        console: Console,
        label: str = "",
        interval: float = 0.08,
        style: str = "cyan",
        enabled: bool | None = None,
    ) -> None:
        if frame_type not in SPINNER_FRAMES:
            valid = ", ".join(SPINNER_FRAMES.keys())
            raise ValueError(f"Unknown spinner type '{frame_type}'. Valid: {valid}")
        self.frame_type = frame_type
        super().__init__(console, label, interval, style, enabled)

    @property
    def frames(self) -> list[str]:
        return SPINNER_FRAMES[self.frame_type]


# ─── SquareLoader ────────────────────────────────────────────────────────────


class SquareLoader(FrameAnimation):
    """Claude Code-style square loading bar.

    Displays a square bracket loading bar with block characters:
        [████████░░░░░░░░░░░░] 40%

    The bar fills from left to right using █ (filled) and ░ (empty).
    When complete, shows ✓. When error, shows ✗.

    Usage:
        loader = SquareLoader(console, "Processing...", width=20)
        loader.start()
        loader.set_progress(0.5)  # 50%
        loader.complete()  # Shows ✓
    """

    def __init__(
        self,
        console: Console,
        label: str = "",
        width: int = 20,
        interval: float = 0.08,
        style: str = "cyan",
        enabled: bool | None = None,
    ) -> None:
        self.width = max(10, min(40, width))
        self._progress: float = 0.0
        self._state: str = "active"  # active, complete, error, indeterminate
        self._indeterminate_frame = 0
        super().__init__(console, label, interval, style, enabled)

    @property
    def frames(self) -> list[str]:
        # SquareLoader manages its own rendering, frames not used directly
        return [""]

    def set_progress(self, progress: float) -> None:
        """Set progress as a float between 0.0 and 1.0."""
        self._progress = max(0.0, min(1.0, progress))
        self._state = "active"

    def complete(self) -> None:
        """Mark as complete (shows ✓)."""
        self._progress = 1.0
        self._state = "complete"

    def error(self) -> None:
        """Mark as error (shows ✗)."""
        self._state = "error"

    def indeterminate(self) -> None:
        """Set to indeterminate state (animated ▓ blocks)."""
        self._state = "indeterminate"

    def render(self) -> RenderableType:
        """Render the square loading bar."""
        if not self._enabled:
            if self._state == "complete":
                return Text(f"✓ {self.label}", style=SUCCESS)
            elif self._state == "error":
                return Text(f"✗ {self.label}", style=ERROR)
            return Text(self.label, style=self.style) if self.label else Text("")

        bar = self._render_bar()
        pct = f" {self._progress * 100:.0f}%" if self._state == "active" else ""

        text = Text()
        if self.label:
            text.append(self.label + " ", style=self.style)
        text.append(bar, style=self._get_bar_style())
        if pct:
            text.append(pct, style=MUTED)

        if self._state == "complete":
            text.append(" ✓", style=SUCCESS)
        elif self._state == "error":
            text.append(" ✗", style=ERROR)

        return text

    def _render_bar(self) -> str:
        """Render the bar portion: [████████░░░░░░░░░░░░]"""
        if self._state == "indeterminate":
            # Animated indeterminate bar
            filled = self.width // 2
            shift = self._indeterminate_frame % (self.width - filled + 1)
            bar = "░" * shift + "▓" * filled + "░" * (self.width - filled - shift)
            self._indeterminate_frame += 1
        elif self._state == "error":
            bar = "x" * self.width
        elif self._state == "complete":
            bar = "█" * self.width
        else:
            filled = int(self.width * self._progress)
            bar = "█" * filled + "░" * (self.width - filled)
        return f"[{bar}]"

    def _get_bar_style(self) -> str:
        if self._state == "complete":
            return SUCCESS
        elif self._state == "error":
            return ERROR
        elif self._state == "indeterminate":
            return WARNING
        return self.style

    async def _animate_loop(self) -> None:
        """Override to handle indeterminate animation."""
        while self._running:
            if self._state == "indeterminate":
                self.advance()
            if self._live:
                self._live.update(self.render())
            await asyncio.sleep(self.interval)


# ─── ThinkingAnimation ───────────────────────────────────────────────────────


class ThinkingAnimation(FrameAnimation):
    """Thinking animation with rotating verbs.

    Cycles through thinking verbs (Thinking, Processing, Analyzing, etc.)
    with a spinner prefix.

    Usage:
        anim = ThinkingAnimation(console, spinner_type="dots")
        anim.start()
        # ... work ...
        anim.stop()
    """

    def __init__(
        self,
        console: Console,
        spinner_type: str = "dots",
        interval: float = 0.08,
        style: str = "cyan",
        enabled: bool | None = None,
    ) -> None:
        self.spinner_type = spinner_type
        self._verb_index = 0
        super().__init__(console, "", interval, style, enabled)

    @property
    def frames(self) -> list[str]:
        return SPINNER_FRAMES.get(self.spinner_type, DOTS_FRAMES)

    @property
    def current_verb(self) -> str:
        return THINKING_VERBS[self._verb_index % len(THINKING_VERBS)]

    def advance(self) -> None:
        """Advance both frame and verb."""
        super().advance()
        if self._frame_index % 3 == 0:
            self._verb_index = (self._verb_index + 1) % len(THINKING_VERBS)

    def render(self) -> RenderableType:
        """Render spinner + verb."""
        if not self._enabled:
            return Text(self.current_verb + "...", style=self.style)

        frame = self.current_frame
        text = Text()
        text.append(frame + " ", style=self.style)
        text.append(self.current_verb + "...", style=self.style)
        return text


# ─── ProgressBar ─────────────────────────────────────────────────────────────


@dataclass
class ProgressBar:
    """Progress bar with percentage and ETA.

    Displays a visual bar with percentage complete and estimated time remaining.

    Usage:
        bar = ProgressBar(console, total=100, label="Processing")
        for i in range(100):
            bar.update(i + 1)
            await asyncio.sleep(0.1)
        bar.complete()
    """

    console: Console
    total: int
    label: str = ""
    width: int = 30
    style: str = "cyan"
    show_percentage: bool = True
    show_eta: bool = True
    show_count: bool = True
    enabled: bool | None = None

    def __post_init__(self) -> None:
        self._current: int = 0
        self._start_time: float = 0.0
        self._complete: bool = False
        self._enabled = self.enabled if self.enabled is not None else should_animate()
        self._live: Live | None = None
        self._running = False

    def start(self) -> None:
        """Start the progress bar display."""
        if not self._enabled:
            return
        self._start_time = time.monotonic()
        self._running = True
        self._live = Live(
            self.render(),
            console=self.console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.start()

    def update(self, current: int) -> None:
        """Update progress to the given value."""
        self._current = max(0, min(self.total, current))
        if self._live and self._running:
            self._live.update(self.render())

    def advance(self, amount: int = 1) -> None:
        """Advance progress by the given amount."""
        self.update(self._current + amount)

    def complete(self) -> None:
        """Mark as complete and stop."""
        self._current = self.total
        self._complete = True
        if self._live and self._running:
            self._live.update(self.render())
        self.stop()

    def stop(self) -> None:
        """Stop the progress bar."""
        self._running = False
        if self._live:
            self._live.stop()
            self._live = None

    def render(self) -> RenderableType:
        """Render the progress bar."""
        if not self._enabled:
            if self._complete:
                return Text(f"✓ {self.label}", style=SUCCESS) if self.label else Text("✓", style=SUCCESS)
            pct = self._current / self.total if self.total > 0 else 0
            return Text(f"{self.label} {pct * 100:.0f}%", style=self.style) if self.label else Text(f"{pct * 100:.0f}%", style=self.style)

        elapsed = time.monotonic() - self._start_time if self._start_time > 0 else 0
        pct = self._current / self.total if self.total > 0 else 0
        filled = int(self.width * pct)
        bar = "█" * filled + "░" * (self.width - filled)

        # ETA calculation
        eta_str = ""
        if self.show_eta and not self._complete and self._current > 0 and elapsed > 0:
            rate = self._current / elapsed
            remaining = (self.total - self._current) / rate if rate > 0 else 0
            eta_str = self._format_duration(remaining)

        text = Text()
        if self.label:
            text.append(self.label + " ", style=self.style)
        text.append(f"[{bar}]", style=self.style)
        if self.show_percentage:
            text.append(f" {pct * 100:5.1f}%", style=MUTED)
        if self.show_count:
            text.append(f" {self._current}/{self.total}", style=MUTED)
        if eta_str:
            text.append(f" ETA: {eta_str}", style=MUTED)
        if self._complete:
            text.append(" ✓", style=SUCCESS)

        return text

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format seconds into human-readable duration."""
        if seconds < 60:
            return f"{seconds:.0f}s"
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        if minutes < 60:
            return f"{minutes}m {secs}s"
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours}h {mins}m"

    def __enter__(self) -> ProgressBar:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.complete()
        else:
            self.stop()


# ─── StreamingAnimation ──────────────────────────────────────────────────────


class StreamingAnimation:
    """Token-by-token streaming text display with cursor.

    Displays text as it arrives, character by character or token by token,
    with a blinking cursor at the end.

    Usage:
        stream = StreamingAnimation(console, style="green")
        stream.start()
        for token in response_tokens:
            stream.add_token(token)
            await asyncio.sleep(0.02)
        stream.complete()
    """

    def __init__(
        self,
        console: Console,
        style: str = "green",
        cursor: str = "▌",
        cursor_blink: bool = True,
        enabled: bool | None = None,
    ) -> None:
        self.console = console
        self.style = style
        self.cursor = cursor
        self.cursor_blink = cursor_blink
        self._enabled = enabled if enabled is not None else should_animate()
        self._text: str = ""
        self._complete: bool = False
        self._live: Live | None = None
        self._running = False
        self._cursor_visible = True
        self._cursor_task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the streaming display."""
        if not self._enabled:
            return
        self._running = True
        self._live = Live(
            self.render(),
            console=self.console,
            refresh_per_second=20,
            transient=False,
        )
        self._live.start()
        if self.cursor_blink:
            try:
                loop = asyncio.get_running_loop()
                self._cursor_task = loop.create_task(self._cursor_blink_loop())
            except RuntimeError:
                pass

    def add_token(self, token: str) -> None:
        """Add a token (or character) to the streaming display."""
        self._text += token
        if self._live and self._running:
            self._live.update(self.render())

    def set_text(self, text: str) -> None:
        """Set the full text (replaces current content)."""
        self._text = text
        if self._live and self._running:
            self._live.update(self.render())

    def complete(self) -> None:
        """Mark as complete (removes cursor)."""
        self._complete = True
        if self._live and self._running:
            self._live.update(self.render())
        self.stop()

    def stop(self) -> None:
        """Stop the streaming display."""
        self._running = False
        if self._cursor_task:
            self._cursor_task.cancel()
            self._cursor_task = None
        if self._live:
            self._live.stop()
            self._live = None

    def render(self) -> RenderableType:
        """Render the streaming text with cursor."""
        if not self._enabled:
            return Text(self._text, style=self.style)

        text = Text()
        text.append(self._text, style=self.style)
        if not self._complete and self._cursor_visible:
            text.append(self.cursor, style=self.style)
        return text

    async def _cursor_blink_loop(self) -> None:
        """Blink the cursor on and off."""
        while self._running and not self._complete:
            self._cursor_visible = not self._cursor_visible
            if self._live:
                self._live.update(self.render())
            await asyncio.sleep(0.5)

    def __enter__(self) -> StreamingAnimation:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.complete()
        else:
            self.stop()


# ─── ToolExecutionAnimation ──────────────────────────────────────────────────


class ToolExecutionAnimation(FrameAnimation):
    """Tool execution animation with start/complete states.

    Shows a spinner with the tool name during execution, then transitions
    to a complete state with duration.

    Usage:
        anim = ToolExecutionAnimation(console, "web_search")
        anim.start()
        # ... execute tool ...
        anim.complete(duration=1.23)
    """

    def __init__(
        self,
        console: Console,
        tool_name: str,
        spinner_type: str = "dots",
        interval: float = 0.08,
        style: str = "yellow",
        enabled: bool | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.spinner_type = spinner_type
        self._state: str = "running"  # running, complete, error
        self._duration: float = 0.0
        self._start_time: float = 0.0
        super().__init__(console, "", interval, style, enabled)

    @property
    def frames(self) -> list[str]:
        return SPINNER_FRAMES.get(self.spinner_type, DOTS_FRAMES)

    def start(self) -> None:
        """Start the tool execution animation."""
        self._start_time = time.monotonic()
        super().start()

    def complete(self, duration: float | None = None) -> None:
        """Mark tool execution as complete."""
        self._state = "complete"
        if duration is not None:
            self._duration = duration
        elif self._start_time > 0:
            self._duration = time.monotonic() - self._start_time
        self.stop()

    def error(self) -> None:
        """Mark tool execution as error."""
        self._state = "error"
        if self._start_time > 0:
            self._duration = time.monotonic() - self._start_time
        self.stop()

    def render(self) -> RenderableType:
        """Render the tool execution state."""
        if not self._enabled:
            if self._state == "complete":
                return Text(f"✓ {self.tool_name} ({self._duration:.2f}s)", style=SUCCESS)
            elif self._state == "error":
                return Text(f"✗ {self.tool_name}", style=ERROR)
            return Text(f"→ {self.tool_name}...", style=self.style)

        frame = self.current_frame
        text = Text()

        if self._state == "running":
            text.append(frame + " ", style=self.style)
            text.append(f"Executing {self.tool_name}...", style=self.style)
        elif self._state == "complete":
            text.append("✓ ", style=SUCCESS)
            text.append(self.tool_name, style=SUCCESS)
            text.append(f" ({self._duration:.2f}s)", style=MUTED)
        elif self._state == "error":
            text.append("✗ ", style=ERROR)
            text.append(self.tool_name, style=ERROR)

        return text


# ─── ErrorAnimation ──────────────────────────────────────────────────────────


class ErrorAnimation:
    """Error display animation with red flash/shake effect.

    Shows an error message in a red panel with a brief flash effect.

    Usage:
        err = ErrorAnimation(console, "Session not found")
        err.show()
        await asyncio.sleep(0.5)
        err.clear()
    """

    def __init__(
        self,
        console: Console,
        message: str,
        suggestion: str = "",
        title: str = "Error",
        icon: str = "✗",
        style: str = "red",
        flash_duration: float = 0.3,
        enabled: bool | None = None,
    ) -> None:
        self.console = console
        self.message = message
        self.suggestion = suggestion
        self.title = title
        self.icon = icon
        self.style = style
        self.flash_duration = flash_duration
        self._enabled = enabled if enabled is not None else should_animate()
        self._live: Live | None = None
        self._running = False
        self._flash_task: asyncio.Task | None = None

    def show(self) -> None:
        """Show the error animation."""
        if not self._enabled:
            self.console.print(f"[red]✗ {self.message}[/red]")
            return
        self._running = True
        self._live = Live(
            self.render(),
            console=self.console,
            refresh_per_second=10,
            transient=True,
        )
        self._live.start()
        try:
            loop = asyncio.get_running_loop()
            self._flash_task = loop.create_task(self._flash_loop())
        except RuntimeError:
            pass

    def clear(self) -> None:
        """Clear the error display."""
        self._running = False
        if self._flash_task:
            self._flash_task.cancel()
            self._flash_task = None
        if self._live:
            self._live.stop()
            self._live = None

    def render(self) -> RenderableType:
        """Render the error panel."""
        content = Text()
        content.append(f"{self.icon} {self.title}: ", style=f"{self.style} bold")
        content.append(self.message, style=TEXT)
        if self.suggestion:
            content.append("\n\n")
            content.append("Suggestion: ", style=f"{INFO} bold")
            content.append(self.suggestion, style=INFO)

        return Panel(
            content,
            border_style=self.style,
            title=f"[bold {self.style}]{self.icon} {self.title}[/]",
            title_align="left",
        )

    async def _flash_loop(self) -> None:
        """Flash the error panel briefly."""
        await asyncio.sleep(self.flash_duration)
        if self._running:
            self.clear()

    def __enter__(self) -> ErrorAnimation:
        self.show()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.clear()


# ─── SuccessAnimation ────────────────────────────────────────────────────────


class SuccessAnimation:
    """Success display animation with green checkmark.

    Shows a success message with a green checkmark, optionally in a panel.

    Usage:
        succ = SuccessAnimation(console, "Model set to: gpt-4o")
        succ.show()
        await asyncio.sleep(0.5)
        succ.clear()
    """

    def __init__(
        self,
        console: Console,
        message: str,
        title: str = "Success",
        icon: str = "✓",
        style: str = "green",
        show_panel: bool = False,
        flash_duration: float = 0.3,
        enabled: bool | None = None,
    ) -> None:
        self.console = console
        self.message = message
        self.title = title
        self.icon = icon
        self.style = style
        self.show_panel = show_panel
        self.flash_duration = flash_duration
        self._enabled = enabled if enabled is not None else should_animate()
        self._live: Live | None = None
        self._running = False
        self._flash_task: asyncio.Task | None = None

    def show(self) -> None:
        """Show the success animation."""
        if not self._enabled:
            self.console.print(f"[green]✓ {self.message}[/green]")
            return
        self._running = True
        self._live = Live(
            self.render(),
            console=self.console,
            refresh_per_second=10,
            transient=True,
        )
        self._live.start()
        try:
            loop = asyncio.get_running_loop()
            self._flash_task = loop.create_task(self._flash_loop())
        except RuntimeError:
            pass

    def clear(self) -> None:
        """Clear the success display."""
        self._running = False
        if self._flash_task:
            self._flash_task.cancel()
            self._flash_task = None
        if self._live:
            self._live.stop()
            self._live = None

    def render(self) -> RenderableType:
        """Render the success message."""
        content = Text()
        content.append(f"{self.icon} ", style=f"{self.style} bold")
        content.append(self.message, style=self.style)

        if self.show_panel:
            return Panel(
                content,
                border_style=self.style,
                title=f"[bold {self.style}]{self.icon} {self.title}[/]",
                title_align="left",
            )
        return content

    async def _flash_loop(self) -> None:
        """Flash the success message briefly."""
        await asyncio.sleep(self.flash_duration)
        if self._running:
            self.clear()

    def __enter__(self) -> SuccessAnimation:
        self.show()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.clear()


# ─── FadeTransition ──────────────────────────────────────────────────────────


class FadeTransition:
    """Smooth fade transition between two renderable states.

    Creates a fade effect by gradually changing opacity or cross-fading
    between two panels/text blocks.

    Usage:
        fade = FadeTransition(console, old_content, new_content)
        await fade.fade_in()
        # ... swap content ...
        await fade.fade_out()
    """

    def __init__(
        self,
        console: Console,
        duration: float = 0.3,
        steps: int = 10,
        style: str = "cyan",
        enabled: bool | None = None,
    ) -> None:
        self.console = console
        self.duration = duration
        self.steps = steps
        self.style = style
        self._enabled = enabled if enabled is not None else should_animate()
        self._live: Live | None = None
        self._running = False

    @staticmethod
    def _smoothstep(t: float) -> float:
        """Smoothstep easing function: t² × (3 - 2t)"""
        return t * t * (3.0 - 2.0 * t)

    async def fade_in(
        self,
        renderable: RenderableType,
        label: str = "",
    ) -> None:
        """Fade in a renderable from empty to full opacity."""
        if not self._enabled:
            self.console.print(renderable)
            return

        self._running = True
        step_duration = self.duration / self.steps

        self._live = Live(
            Text("", style=self.style),
            console=self.console,
            refresh_per_second=20,
            transient=True,
        )
        self._live.start()

        for i in range(1, self.steps + 1):
            opacity = self._smoothstep(i / self.steps)
            # Simulate fade by showing partial content
            text = Text()
            if label:
                text.append(label + " ", style=self.style)
            text.append("█" * int(20 * opacity), style=self.style)
            self._live.update(text)
            await asyncio.sleep(step_duration)

        self._live.update(renderable)
        await asyncio.sleep(0.1)
        self._running = False
        if self._live:
            self._live.stop()
            self._live = None

    async def fade_out(
        self,
        renderable: RenderableType,
        label: str = "",
    ) -> None:
        """Fade out a renderable from full to empty."""
        if not self._enabled:
            return

        self._running = True
        step_duration = self.duration / self.steps

        self._live = Live(
            renderable,
            console=self.console,
            refresh_per_second=20,
            transient=True,
        )
        self._live.start()

        for i in range(self.steps - 1, -1, -1):
            opacity = self._smoothstep(i / self.steps)
            text = Text()
            if label:
                text.append(label + " ", style=self.style)
            text.append("█" * int(20 * opacity), style=self.style)
            self._live.update(text)
            await asyncio.sleep(step_duration)

        self._running = False
        if self._live:
            self._live.stop()
            self._live = None

    async def cross_fade(
        self,
        old: RenderableType,
        new: RenderableType,
        label: str = "",
    ) -> None:
        """Cross-fade from old to new renderable."""
        if not self._enabled:
            self.console.print(new)
            return

        self._running = True
        step_duration = self.duration / self.steps

        self._live = Live(
            old,
            console=self.console,
            refresh_per_second=20,
            transient=True,
        )
        self._live.start()

        for i in range(1, self.steps + 1):
            opacity = self._smoothstep(i / self.steps)
            text = Text()
            if label:
                text.append(label + " ", style=self.style)
            # Show transition indicator
            filled = int(20 * opacity)
            text.append("█" * filled, style=self.style)
            text.append("░" * (20 - filled), style=MUTED)
            self._live.update(text)
            await asyncio.sleep(step_duration)

        self._live.update(new)
        await asyncio.sleep(0.1)
        self._running = False
        if self._live:
            self._live.stop()
            self._live = None

    def stop(self) -> None:
        """Stop any running transition."""
        self._running = False
        if self._live:
            self._live.stop()
            self._live = None



# ─── KnightRiderScanner ─────────────────────────────────────────────────────


class KnightRiderScanner(FrameAnimation):
    """Knight Rider-style bidirectional scanner with gradient trail.

    A bright "head" scans left-to-right and right-to-left with a
    fading trail behind it. Hold frames pause at each end.

    Usage:
        scanner = KnightRiderScanner(console, "Scanning...", width=30)
        scanner.start()
        # ... work ...
        scanner.stop()
    """

    def __init__(
        self,
        console: Console,
        label: str = "",
        width: int = 30,
        interval: float = 0.05,
        style: str = "red",
        head_char: str = "█",
        trail_chars: list[str] | None = None,
        hold_frames: int = 3,
        enabled: bool | None = None,
    ) -> None:
        self.width = max(10, min(60, width))
        self.head_char = head_char
        self.trail_chars = trail_chars or ["█", "▉", "▊", "▋", "▌", "▍", "▎", "▏"]
        self.hold_frames = hold_frames
        self._position = 0
        self._direction = 1  # 1 = right, -1 = left
        self._hold_counter = 0
        super().__init__(console, label, interval, style, enabled)

    @property
    def frames(self) -> list[str]:
        return [""]

    def advance(self) -> None:
        """Advance the scanner position with hold frames at edges."""
        if self._hold_counter > 0:
            self._hold_counter -= 1
            return

        self._position += self._direction

        if self._position >= self.width - 1:
            self._position = self.width - 1
            self._direction = -1
            self._hold_counter = self.hold_frames
        elif self._position <= 0:
            self._position = 0
            self._direction = 1
            self._hold_counter = self.hold_frames

    def render(self) -> RenderableType:
        """Render the scanner with gradient trail."""
        if not self._enabled:
            return Text(self.label, style=self.style) if self.label else Text("")

        # Build the scanner bar with gradient trail
        bar_chars = []
        for i in range(self.width):
            dist = abs(i - self._position)
            if dist == 0:
                bar_chars.append(self.head_char)
            elif dist <= len(self.trail_chars):
                bar_chars.append(self.trail_chars[dist - 1])
            else:
                bar_chars.append("░")

        text = Text()
        if self.label:
            text.append(self.label + " ", style=self.style)
        text.append("[" + "".join(bar_chars) + "]", style=self.style)
        return text


# ─── BackgroundPulse ────────────────────────────────────────────────────────


class BackgroundPulse:
    """Full-screen animated background with expanding rings.

    Creates a pulsing background effect with 3 phase-offset expanding rings
    and a logo shimmer. Uses frame caching and FPS capping for performance.

    Usage:
        pulse = BackgroundPulse(console, logo="AH")
        pulse.start()
        # ... work ...
        pulse.stop()
    """

    def __init__(
        self,
        console: Console,
        logo: str = "AH",
        interval: float = 0.1,
        fps_cap: int = 15,
        ring_chars: list[str] | None = None,
        style: str = "cyan",
        enabled: bool | None = None,
    ) -> None:
        self.console = console
        self.logo = logo
        self.interval = interval
        self.fps_cap = max(1, min(60, fps_cap))
        self.ring_chars = ring_chars or ["○", "◯", "◎", "●"]
        self.style = style
        self._enabled = enabled if enabled is not None else should_animate()
        self._live: Live | None = None
        self._running = False
        self._frame_cache: dict[int, Text] = {}
        self._last_frame_time = 0.0
        self._frame_count = 0
        self._task: asyncio.Task | None = None

    def _get_frame(self, frame_num: int) -> Text:
        """Get a cached frame or compute a new one."""
        if frame_num in self._frame_cache:
            return self._frame_cache[frame_num]

        # Compute frame with 3 phase-offset rings
        text = Text()
        console_width = self.console.width or 80
        for ring_idx in range(3):
            phase = (frame_num + ring_idx * 7) % 24
            char = self.ring_chars[ring_idx % len(self.ring_chars)]
            # Create expanding ring effect
            ring_width = console_width - ring_idx * 4
            if ring_width > 0:
                line = " " * (ring_idx * 2) + char * ring_width
                text.append(line + "\n", style=self.style)

        # Logo shimmer
        shimmer_phase = frame_num % 10
        shimmer_style = self.style if shimmer_phase < 5 else MUTED
        text.append(f"\n  {self.logo}\n", style=shimmer_style)

        # Cache the frame (limit cache size)
        if len(self._frame_cache) > 100:
            self._frame_cache.clear()
        self._frame_cache[frame_num] = text
        return text

    def start(self) -> None:
        """Start the background pulse animation."""
        if not self._enabled:
            return
        self._running = True
        self._live = Live(
            self._get_frame(0),
            console=self.console,
            refresh_per_second=self.fps_cap,
            transient=True,
        )
        self._live.start()
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._animate_loop())
        except RuntimeError:
            pass

    def stop(self) -> None:
        """Stop the background pulse animation."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._live:
            self._live.stop()
            self._live = None

    async def _animate_loop(self) -> None:
        """Animation loop with FPS capping."""
        while self._running:
            # FPS capping
            now = time.monotonic()
            elapsed = now - self._last_frame_time
            min_interval = 1.0 / self.fps_cap
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)

            self._frame_count += 1
            if self._live:
                self._live.update(self._get_frame(self._frame_count))
            self._last_frame_time = time.monotonic()

    def __enter__(self) -> BackgroundPulse:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.stop()


# ─── KawaiiSpinner ──────────────────────────────────────────────────────────────


class KawaiiSpinner(FrameAnimation):
    """Kawaii-style spinner with cute faces and configurable wings.

    Cycles through kawaii faces with optional wing decorations and
    thinking verbs.

    Usage:
        kawaii = KawaiiSpinner(console, "Thinking...", wings="彡")
        kawaii.start()
        # ... work ...
        kawaii.stop()
    """

    KAWAII_FACES = [
        "(◕‿◕)",
        "(｡◕‿◕｡)",
        "(◕ᴗ◕)",
        "(｡◕ᴗ◕｡)",
        "(✿◠‿◠)",
        "(◠‿◠✿)",
        "(｡◠‿◠｡)",
        "(✧◡✧)",
    ]

    def __init__(
        self,
        console: Console,
        label: str = "",
        interval: float = 0.15,
        style: str = "magenta",
        wings: str = "",
        show_verbs: bool = True,
        enabled: bool | None = None,
    ) -> None:
        self.wings = wings
        self.show_verbs = show_verbs
        self._verb_index = 0
        super().__init__(console, label, interval, style, enabled)

    @property
    def frames(self) -> list[str]:
        return self.KAWAII_FACES

    @property
    def current_verb(self) -> str:
        return THINKING_VERBS[self._verb_index % len(THINKING_VERBS)]

    def advance(self) -> None:
        """Advance face and verb."""
        super().advance()
        if self._frame_index % 4 == 0:
            self._verb_index = (self._verb_index + 1) % len(THINKING_VERBS)

    def render(self) -> RenderableType:
        """Render kawaii face with wings and verb."""
        if not self._enabled:
            return Text(self.label, style=self.style) if self.label else Text("")

        face = self.current_frame
        text = Text()

        if self.wings:
            text.append(self.wings + " ", style=self.style)

        text.append(face + " ", style=self.style)

        if self.show_verbs:
            text.append(self.current_verb + "...", style=self.style)
        elif self.label:
            text.append(self.label, style=self.style)

        if self.wings:
            text.append(" " + self.wings, style=self.style)

        return text


# ─── FrameCache ─────────────────────────────────────────────────────────────


class FrameCache:
    """Simple LRU cache for animation frames.

    Caches rendered frames to avoid recomputation for complex animations.
    """

    def __init__(self, max_size: int = 100) -> None:
        self.max_size = max_size
        self._cache: dict[int, Text] = {}
        self._access_order: list[int] = []

    def get(self, key: int) -> Text | None:
        """Get a cached frame by key."""
        if key in self._cache:
            # Move to end (most recently used)
            self._access_order.remove(key)
            self._access_order.append(key)
            return self._cache[key]
        return None

    def put(self, key: int, frame: Text) -> None:
        """Cache a frame."""
        if key in self._cache:
            self._access_order.remove(key)
        elif len(self._cache) >= self.max_size:
            # Evict least recently used
            lru_key = self._access_order.pop(0)
            del self._cache[lru_key]

        self._cache[key] = frame
        self._access_order.append(key)

    def clear(self) -> None:
        """Clear the cache."""
        self._cache.clear()
        self._access_order.clear()

    def __len__(self) -> int:
        return len(self._cache)


# ─── FlashMessage ───────────────────────────────────────────────────────────


class FlashMessage:
    """Flash message with inverse-video and setTimeout auto-dismiss.

    Displays a message with inverse video (reverse) styling that
    automatically dismisses after a timeout.

    Usage:
        flash = FlashMessage(console, "Saved!", duration=2.0)
        flash.show()
        # ... or as context manager ...
        with FlashMessage(console, "Done!", duration=1.5):
            # ... work ...
    """

    def __init__(
        self,
        console: Console,
        message: str,
        duration: float = 2.0,
        style: str = "yellow",
        icon: str = "⚡",
        enabled: bool | None = None,
    ) -> None:
        self.console = console
        self.message = message
        self.duration = duration
        self.style = style
        self.icon = icon
        self._enabled = enabled if enabled is not None else should_animate()
        self._live: Live | None = None
        self._running = False
        self._task: asyncio.Task | None = None

    def show(self) -> None:
        """Show the flash message."""
        if not self._enabled:
            self.console.print(f"[reverse]{self.icon} {self.message}[/reverse]")
            return

        self._running = True
        self._live = Live(
            self.render(),
            console=self.console,
            refresh_per_second=10,
            transient=True,
        )
        self._live.start()

        # setTimeout equivalent
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._auto_dismiss())
        except RuntimeError:
            pass

    def clear(self) -> None:
        """Clear the flash message."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._live:
            self._live.stop()
            self._live = None

    def render(self) -> RenderableType:
        """Render the flash message with inverse video."""
        text = Text()
        text.append(f"{self.icon} ", style=f"{self.style} bold")
        text.append(self.message, style=self.style)
        return text

    async def _auto_dismiss(self) -> None:
        """Auto-dismiss after timeout (setTimeout equivalent)."""
        await asyncio.sleep(self.duration)
        if self._running:
            self.clear()

    def __enter__(self) -> FlashMessage:
        self.show()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.clear()


# ─── ScrollAccelerator ───────────────────────────────────────────────────────


class ScrollAccelerator:
    """Velocity-based scroll acceleration with physics.

    Provides smooth scrolling with acceleration and deceleration
    based on velocity physics.

    Usage:
        scroll = ScrollAccelerator()
        scroll.press_down()  # Start accelerating
        pos = scroll.update()  # Get new position
        scroll.release()  # Decelerate to stop
    """

    def __init__(
        self,
        acceleration: float = 0.5,
        deceleration: float = 0.3,
        max_velocity: float = 10.0,
        friction: float = 0.8,
    ) -> None:
        self.acceleration = acceleration
        self.deceleration = deceleration
        self.max_velocity = max_velocity
        self.friction = friction
        self._velocity = 0.0
        self._position = 0.0
        self._target_position = 0.0
        self._scrolling = False

    def press_down(self) -> None:
        """Start scrolling down (accelerate)."""
        self._scrolling = True
        self._velocity = min(self._velocity + self.acceleration, self.max_velocity)

    def press_up(self) -> None:
        """Start scrolling up (accelerate)."""
        self._scrolling = True
        self._velocity = max(self._velocity - self.acceleration, -self.max_velocity)

    def release(self) -> None:
        """Release (decelerate to stop)."""
        self._scrolling = False

    def update(self) -> int:
        """Update position based on velocity. Returns integer position."""
        if not self._scrolling:
            # Apply deceleration
            if abs(self._velocity) > 0.1:
                self._velocity *= self.friction
            else:
                self._velocity = 0.0

        self._position += self._velocity
        return int(self._position)

    def scroll_to(self, target: int) -> None:
        """Set a target position to scroll towards."""
        self._target_position = float(target)
        diff = self._target_position - self._position
        if abs(diff) > 1:
            self._velocity = max(-self.max_velocity, min(self.max_velocity, diff * 0.1))

    @property
    def position(self) -> int:
        """Get current scroll position."""
        return int(self._position)

    @property
    def velocity(self) -> float:
        """Get current velocity."""
        return self._velocity

    def reset(self) -> None:
        """Reset scroll state."""
        self._velocity = 0.0
        self._position = 0.0
        self._target_position = 0.0
        self._scrolling = False


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

