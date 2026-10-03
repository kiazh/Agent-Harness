"""Spinner animations: Spinner, SquareLoader, ThinkingAnimation, KawaiiSpinner."""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import (
    TYPE_CHECKING,
)

from rich.console import Console, RenderableType
from rich.live import Live
from rich.text import Text

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)

from ah.cli.animations._base import (
    DOTS_FRAMES,
    SPINNER_FRAMES,
    THINKING_VERBS,
    SUCCESS,
    WARNING,
    ERROR,
    MUTED,
    should_animate,
)
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


