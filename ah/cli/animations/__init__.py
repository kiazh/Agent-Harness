"""
AgentHarness animation library.

Provides spinners, loaders, progress bars, streaming effects, transitions,
and animation management — all TTY-aware and Rich Live compatible.

This package is split into submodules:
  - ah.cli.animations._base: shared imports, TTY detection, colors, frames
  - ah.cli.animations.spinners: Spinner, SquareLoader, ThinkingAnimation, KawaiiSpinner
  - ah.cli.animations.loaders: ProgressBar, StreamingAnimation, ToolExecutionAnimation,
    ErrorAnimation, SuccessAnimation
  - ah.cli.animations.transitions: FadeTransition, KnightRiderScanner, BackgroundPulse,
    FrameCache, FlashMessage, ScrollAccelerator
  - ah.cli.animations.runner: AnimationRunner and convenience factories

Usage:
    from ah.cli.animations import Spinner, ProgressBar, AnimationRunner

    with Spinner("dots", console, "Loading...") as spin:
        spin.start()
        spin.stop()
"""
from __future__ import annotations

from ah.cli.animations._base import (
    is_tty,
    should_animate,
    PRIMARY,
    SECONDARY,
    SUCCESS,
    WARNING,
    ERROR,
    INFO,
    MUTED,
    TEXT,
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
from ah.cli.animations.runner import (
    AnimationRunner,
    get_spinner,
    get_progress_bar,
    get_thinking_animation,
    get_streaming_animation,
    get_tool_animation,
    get_error_animation,
    get_success_animation,
    get_fade_transition,
    get_animation_runner,
    get_knight_rider_scanner,
    get_background_pulse,
    get_kawaii_spinner,
    get_flash_message,
    get_scroll_accelerator,
)

__all__ = [
    # base
    "is_tty",
    "should_animate",
    "PRIMARY",
    "SECONDARY",
    "SUCCESS",
    "WARNING",
    "ERROR",
    "INFO",
    "MUTED",
    "TEXT",
    "DOTS_FRAMES",
    "BOUNCE_FRAMES",
    "GROW_FRAMES",
    "ARROW_FRAMES",
    "STAR_FRAMES",
    "MOON_FRAMES",
    "PULSE_FRAMES",
    "BRAIN_FRAMES",
    "SPARKLE_FRAMES",
    "SPINNER_FRAMES",
    "THINKING_VERBS",
    # spinners
    "FrameAnimation",
    "Spinner",
    "SquareLoader",
    "ThinkingAnimation",
    "KawaiiSpinner",
    # loaders
    "ProgressBar",
    "StreamingAnimation",
    "ToolExecutionAnimation",
    "ErrorAnimation",
    "SuccessAnimation",
    # transitions
    "FadeTransition",
    "KnightRiderScanner",
    "BackgroundPulse",
    "FrameCache",
    "FlashMessage",
    "ScrollAccelerator",
    # runner
    "AnimationRunner",
    "get_spinner",
    "get_progress_bar",
    "get_thinking_animation",
    "get_streaming_animation",
    "get_tool_animation",
    "get_error_animation",
    "get_success_animation",
    "get_fade_transition",
    "get_animation_runner",
    "get_knight_rider_scanner",
    "get_background_pulse",
    "get_kawaii_spinner",
    "get_flash_message",
    "get_scroll_accelerator",
]
