"""Style presets: PromptStyles, StatusLevel, StatusIndicators."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# ─── Prompt Styles ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PromptStyles:
    """Styling for the interactive prompt.

    Defines the appearance of the prompt symbol, session ID, and input
    area in the REPL.
    """

    # Prompt symbol (e.g., "ah")
    symbol: str = "ah"
    symbol_style: str = "primary bold"

    # Session ID display (e.g., "(abc12345)")
    session_style: str = "secondary dim"
    session_format: str = "({id})"  # {id} placeholder

    # Prompt arrow/separator
    separator: str = ">"
    separator_style: str = "text bold"

    # Input area
    input_style: str = "text"
    suggestion_style: str = "muted italic"

    # Continuation prompt (multi-line)
    continuation_symbol: str = "..."
    continuation_style: str = "muted"

    # Welcome banner
    banner_border_style: str = "primary"
    banner_title_style: str = "primary bold"
    banner_text_style: str = "text"



# ─── Status Indicators ──────────────────────────────────────────────────────


class StatusLevel(str, Enum):
    """Semantic status levels."""

    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    INFO = "info"
    MUTED = "muted"
    PRIMARY = "primary"
    SECONDARY = "secondary"


@dataclass(frozen=True)
class StatusIndicators:
    """Pre-configured status indicator styles.

    Each indicator type (dot, badge, label) has a symbol, color role,
    and optional prefix/suffix.
    """

    # Dot indicator: ● success  ● warning  ● error
    dot_symbol: str = "●"
    dot_success: str = "success"
    dot_warning: str = "warning"
    dot_error: str = "error"
    dot_info: str = "info"
    dot_muted: str = "muted"

    # Badge indicator: [OK]  [WARN]  [ERR]
    badge_success: str = "✓"
    badge_warning: str = "⚠"
    badge_error: str = "✗"
    badge_info: str = "ℹ"
    badge_muted: str = "○"
    badge_prefix: str = "["
    badge_suffix: str = "]"

    # Label indicator: "active", "inactive", "pending"
    label_success: str = "active"
    label_warning: str = "pending"
    label_error: str = "failed"
    label_info: str = "info"
    label_muted: str = "inactive"

    # Spinner frames (Unicode braille)
    spinner_frames: tuple[str, ...] = (
        "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",
    )
    spinner_style: str = "primary"

    # Progress bar characters
    bar_fill: str = "█"
    bar_empty: str = "░"
    bar_partial: str = "▒"
    bar_width: int = 20



