"""Loader animations: ProgressBar, StreamingAnimation, ToolExecutionAnimation, ErrorAnimation, SuccessAnimation."""
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
from ah.cli.animations.spinners import FrameAnimation
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


