"""Transition animations: FadeTransition, KnightRiderScanner, BackgroundPulse, FrameCache, FlashMessage, ScrollAccelerator."""
from __future__ import annotations

import asyncio
import logging
import time
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
    MUTED,
    should_animate,
)
from ah.cli.animations.spinners import FrameAnimation
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


