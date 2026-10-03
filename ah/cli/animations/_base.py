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


