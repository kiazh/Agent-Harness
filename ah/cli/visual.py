"""
AgentHarness Visual Design System
=================================

Comprehensive visual theming for the `ah` CLI. Provides:
  - ColorScheme dataclass with dark/light variants
  - 8 built-in themes (default/gold, crimson, ocean, forest, sunset, midnight, arctic, volcanic)
  - PanelStyles for info/success/warning/error/agent/tool
  - TableStyles for standard/compact/status
  - PromptStyles for prompt symbol and input
  - StatusIndicators with dot/badge/label
  - VisualContext class that wraps Rich Console with theme awareness
  - Light mode detection via environment variables

Usage:
    from ah.cli.visual import VisualContext, ThemeName

    viz = VisualContext()
    viz.panel("Hello", style="info")
    viz.status("active", level="success")
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.style import Style
from rich import box


# ─── Theme Name Enum ─────────────────────────────────────────────────────────


class ThemeName(str, Enum):
    """Available theme names."""

    DEFAULT = "default"
    GOLD = "gold"
    CRIMSON = "crimson"
    OCEAN = "ocean"
    FOREST = "forest"
    SUNSET = "sunset"
    MIDNIGHT = "midnight"
    ARCTIC = "arctic"
    VOLCANIC = "volcanic"


# ─── Color Scheme ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ColorScheme:
    """Complete color palette for a theme.

    Each color is a Rich-compatible color string (hex, ANSI name, or RGB).
    Dark and light variants are provided for each semantic role.
    """

    # Core semantic colors
    primary: str
    secondary: str
    success: str
    warning: str
    error: str
    info: str
    muted: str
    text: str
    background: str

    # Extended palette
    accent: str = ""       # Highlight / emphasis
    border: str = ""       # Panel/table borders
    highlight: str = ""    # Selection / active item
    dim: str = ""          # De-emphasized text

    # Light-mode overrides (empty = use same as dark)
    light_primary: str = ""
    light_secondary: str = ""
    light_success: str = ""
    light_warning: str = ""
    light_error: str = ""
    light_info: str = ""
    light_muted: str = ""
    light_text: str = ""
    light_background: str = ""
    light_accent: str = ""
    light_border: str = ""
    light_highlight: str = ""
    light_dim: str = ""

    def is_light(self) -> bool:
        """Return True if this scheme has distinct light-mode colors."""
        return bool(self.light_primary)

    def get(self, role: str, light: bool = False) -> str:
        """Get a color by role name, optionally for light mode.

        Falls back to the dark variant if no light override exists.
        """
        if light:
            light_val = getattr(self, f"light_{role}", "")
            if light_val:
                return light_val
        return getattr(self, role, "")

    def style(self, role: str, *, light: bool = False, **kwargs: Any) -> Style:
        """Build a Rich Style from a color role.

        Args:
            role: Semantic color role (primary, success, etc.)
            light: Use light-mode variant if available
            **kwargs: Additional Style attributes (bold, italic, underline, etc.)
        """
        color = self.get(role, light=light)
        return Style(color=color, **kwargs)


# ─── Built-in Themes ────────────────────────────────────────────────────────


def _default_scheme() -> ColorScheme:
    """Default/Gold theme — the spec's canonical palette."""
    return ColorScheme(
        primary="#00D4FF",
        secondary="#7C3AED",
        success="#10B981",
        warning="#F59E0B",
        error="#EF4444",
        info="#3B82F6",
        muted="#6B7280",
        text="#E5E7EB",
        background="#111827",
        accent="#FFD700",
        border="#374151",
        highlight="#1F2937",
        dim="#9CA3AF",
        # Light mode
        light_primary="#0066CC",
        light_secondary="#5B21B6",
        light_success="#059669",
        light_warning="#D97706",
        light_error="#DC2626",
        light_info="#2563EB",
        light_muted="#6B7280",
        light_text="#1F2937",
        light_background="#F9FAFB",
        light_accent="#B8860B",
        light_border="#D1D5DB",
        light_highlight="#E5E7EB",
        light_dim="#9CA3AF",
    )


def _crimson_scheme() -> ColorScheme:
    """Crimson theme — bold reds and warm accents."""
    return ColorScheme(
        primary="#DC2626",
        secondary="#991B1B",
        success="#16A34A",
        warning="#EA580C",
        error="#B91C1C",
        info="#2563EB",
        muted="#78716C",
        text="#FEF2F2",
        background="#1C1917",
        accent="#F59E0B",
        border="#44403C",
        highlight="#292524",
        dim="#A8A29E",
        # Light mode
        light_primary="#DC2626",
        light_secondary="#7F1D1D",
        light_success="#15803D",
        light_warning="#C2410C",
        light_error="#991B1B",
        light_info="#1D4ED8",
        light_muted="#78716C",
        light_text="#1C1917",
        light_background="#FAFAF9",
        light_accent="#B45309",
        light_border="#D6D3D1",
        light_highlight="#E7E5E4",
        light_dim="#A8A29E",
    )


def _ocean_scheme() -> ColorScheme:
    """Ocean theme — deep blues and teals."""
    return ColorScheme(
        primary="#0EA5E9",
        secondary="#06B6D4",
        success="#14B8A6",
        warning="#F59E0B",
        error="#F43F5E",
        info="#3B82F6",
        muted="#64748B",
        text="#F0F9FF",
        background="#0C1222",
        accent="#22D3EE",
        border="#1E3A5F",
        highlight="#162D4A",
        dim="#94A3B8",
        # Light mode
        light_primary="#0284C7",
        light_secondary="#0891B2",
        light_success="#0D9488",
        light_warning="#D97706",
        light_error="#E11D48",
        light_info="#2563EB",
        light_muted="#64748B",
        light_text="#0F172A",
        light_background="#F0F9FF",
        light_accent="#06B6D4",
        light_border="#BAE6FD",
        light_highlight="#E0F2FE",
        light_dim="#94A3B8",
    )


def _forest_scheme() -> ColorScheme:
    """Forest theme — greens and earth tones."""
    return ColorScheme(
        primary="#22C55E",
        secondary="#16A34A",
        success="#10B981",
        warning="#EAB308",
        error="#EF4444",
        info="#0EA5E9",
        muted="#78716C",
        text="#F0FDF4",
        background="#0A1F0A",
        accent="#84CC16",
        border="#166534",
        highlight="#14532D",
        dim="#A3A380",
        # Light mode
        light_primary="#16A34A",
        light_secondary="#15803D",
        light_success="#059669",
        light_warning="#CA8A04",
        light_error="#DC2626",
        light_info="#0284C7",
        light_muted="#78716C",
        light_text="#14532D",
        light_background="#F0FDF4",
        light_accent="#65A30D",
        light_border="#BBF7D0",
        light_highlight="#DCFCE7",
        light_dim="#A3A380",
    )


def _sunset_scheme() -> ColorScheme:
    """Sunset theme — warm oranges, pinks, and purples."""
    return ColorScheme(
        primary="#F97316",
        secondary="#EC4899",
        success="#22C55E",
        warning="#FBBF24",
        error="#EF4444",
        info="#8B5CF6",
        muted="#78716C",
        text="#FFF7ED",
        background="#1C1210",
        accent="#FB923C",
        border="#7C2D12",
        highlight="#431407",
        dim="#A8A29E",
        # Light mode
        light_primary="#EA580C",
        light_secondary="#DB2777",
        light_success="#16A34A",
        light_warning="#D97706",
        light_error="#DC2626",
        light_info="#7C3AED",
        light_muted="#78716C",
        light_text="#431407",
        light_background="#FFF7ED",
        light_accent="#F97316",
        light_border="#FED7AA",
        light_highlight="#FFEDD5",
        light_dim="#A8A29E",
    )


def _midnight_scheme() -> ColorScheme:
    """Midnight theme — deep purples and night-sky blues."""
    return ColorScheme(
        primary="#8B5CF6",
        secondary="#6366F1",
        success="#34D399",
        warning="#FBBF24",
        error="#F87171",
        info="#60A5FA",
        muted="#6B7280",
        text="#EDE9FE",
        background="#0F0A1E",
        accent="#A78BFA",
        border="#312E81",
        highlight="#1E1B4B",
        dim="#9CA3AF",
        # Light mode
        light_primary="#7C3AED",
        light_secondary="#4F46E5",
        light_success="#059669",
        light_warning="#D97706",
        light_error="#DC2626",
        light_info="#2563EB",
        light_muted="#6B7280",
        light_text="#1E1B4B",
        light_background="#F5F3FF",
        light_accent="#8B5CF6",
        light_border="#C4B5FD",
        light_highlight="#EDE9FE",
        light_dim="#9CA3AF",
    )


def _arctic_scheme() -> ColorScheme:
    """Arctic theme — ice blues, whites, and cool grays."""
    return ColorScheme(
        primary="#38BDF8",
        secondary="#818CF8",
        success="#34D399",
        warning="#FCD34D",
        error="#FB7185",
        info="#22D3EE",
        muted="#94A3B8",
        text="#F8FAFC",
        background="#0B1120",
        accent="#7DD3FC",
        border="#1E3A5F",
        highlight="#172554",
        dim="#CBD5E1",
        # Light mode
        light_primary="#0284C7",
        light_secondary="#4F46E5",
        light_success="#059669",
        light_warning="#D97706",
        light_error="#E11D48",
        light_info="#0891B2",
        light_muted="#64748B",
        light_text="#0F172A",
        light_background="#F8FAFC",
        light_accent="#0EA5E9",
        light_border="#BAE6FD",
        light_highlight="#E0F2FE",
        light_dim="#94A3B8",
    )


def _volcanic_scheme() -> ColorScheme:
    """Volcanic theme — dark reds, oranges, and ash grays."""
    return ColorScheme(
        primary="#F97316",
        secondary="#DC2626",
        success="#4ADE80",
        warning="#FACC15",
        error="#EF4444",
        info="#38BDF8",
        muted="#78716C",
        text="#FEF2F2",
        background="#1A0A0A",
        accent="#FB923C",
        border="#7F1D1D",
        highlight="#450A0A",
        dim="#A8A29E",
        # Light mode
        light_primary="#EA580C",
        light_secondary="#B91C1C",
        light_success="#16A34A",
        light_warning="#CA8A04",
        light_error="#DC2626",
        light_info="#0284C7",
        light_muted="#78716C",
        light_text="#450A0A",
        light_background="#FEF2F2",
        light_accent="#F97316",
        light_border="#FECACA",
        light_highlight="#FEE2E2",
        light_dim="#A8A29E",
    )


# Theme registry
THEMES: dict[ThemeName, ColorScheme] = {
    ThemeName.DEFAULT: _default_scheme(),
    ThemeName.GOLD: _default_scheme(),       # alias
    ThemeName.CRIMSON: _crimson_scheme(),
    ThemeName.OCEAN: _ocean_scheme(),
    ThemeName.FOREST: _forest_scheme(),
    ThemeName.SUNSET: _sunset_scheme(),
    ThemeName.MIDNIGHT: _midnight_scheme(),
    ThemeName.ARCTIC: _arctic_scheme(),
    ThemeName.VOLCANIC: _volcanic_scheme(),
}


# ─── Panel Styles ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PanelStyles:
    """Pre-configured panel styles for different semantic uses.

    Each style defines border color, border box style, padding, and title
    alignment for a specific panel type.
    """

    # Border style (Rich box constant)
    border_style: str = "rounded"
    padding: tuple[int, int] = (1, 2)
    title_align: str = "left"
    expand: bool = True

    # Semantic overrides
    border_color: str = ""
    title_color: str = ""
    background_color: str = ""


# Pre-built panel style presets
PANEL_STYLES: dict[str, PanelStyles] = {
    "info": PanelStyles(
        border_color="info",
        title_color="info",
    ),
    "success": PanelStyles(
        border_color="success",
        title_color="success",
    ),
    "warning": PanelStyles(
        border_color="warning",
        title_color="warning",
    ),
    "error": PanelStyles(
        border_color="error",
        title_color="error",
    ),
    "agent": PanelStyles(
        border_color="primary",
        title_color="primary",
        border_style="double",
    ),
    "tool": PanelStyles(
        border_color="secondary",
        title_color="secondary",
        border_style="rounded",
    ),
    "muted": PanelStyles(
        border_color="muted",
        title_color="muted",
        border_style="rounded",
    ),
}


# ─── Table Styles ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TableStyles:
    """Pre-configured table styles for different use cases."""

    # Box drawing style
    box_style: str = "ROUNDED"
    show_header: bool = True
    show_edge: bool = True
    show_lines: bool = False
    pad_edge: bool = True
    collapse_padding: bool = False

    # Colors
    header_style: str = ""
    border_style: str = ""
    row_styles: tuple[str, ...] = ("", "")
    title_justify: str = "left"

    # Compact mode
    compact: bool = False


TABLE_STYLES: dict[str, TableStyles] = {
    "standard": TableStyles(
        box_style="ROUNDED",
        show_header=True,
        show_edge=True,
        show_lines=False,
        header_style="primary bold",
        border_style="muted",
        row_styles=("", "dim"),
    ),
    "compact": TableStyles(
        box_style="SIMPLE",
        show_header=True,
        show_edge=False,
        show_lines=False,
        header_style="primary",
        border_style="",
        row_styles=("", ""),
        compact=True,
    ),
    "status": TableStyles(
        box_style="HEAVY_HEAD",
        show_header=True,
        show_edge=True,
        show_lines=True,
        header_style="primary bold",
        border_style="muted",
        row_styles=("", "dim"),
    ),
}


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


# ─── Light Mode Detection ──────────────────────────────────────────────────


def detect_light_mode() -> bool:
    """Detect whether to use light mode based on environment variables.

    Checks (in order of precedence):
        1. AGENT_HARNESS_LIGHT_MODE env var ("true"/"false"/"1"/"0")
        2. COLORFGBG env var (if last value is light, e.g., "15;0" = dark, "0;15" = light)
        3. Default to dark mode

    Returns:
        True if light mode should be used, False otherwise.
    """
    # Explicit override
    env_val = os.environ.get("AGENT_HARNESS_LIGHT_MODE", "").strip().lower()
    if env_val in ("true", "1", "yes", "on"):
        return True
    if env_val in ("false", "0", "no", "off"):
        return False

    # COLORFGBG heuristic (common on Linux/macOS terminals)
    colorfgbg = os.environ.get("COLORFGBG", "")
    if colorfgbg:
        parts = colorfgbg.split(";")
        if len(parts) >= 2:
            try:
                bg = int(parts[-1])
                # 0-6 = dark, 7 = light, 8-15 = bright/light
                return bg >= 7
            except ValueError:
                pass

    # Default: dark mode
    return False


def detect_no_color() -> bool:
    """Detect whether colors should be disabled.

    Checks:
        1. NO_COLOR env var (any value)
        2. TERM=dumb
        3. Output is not a TTY (unless FORCE_COLOR is set)

    Returns:
        True if colors should be disabled.
    """
    if os.environ.get("NO_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return True
    if os.environ.get("FORCE_COLOR"):
        return False
    if not sys.stdout.isatty():
        return True
    return False


# ─── Visual Context ─────────────────────────────────────────────────────────


class VisualContext:
    """Theme-aware wrapper around Rich Console.

    Provides a unified interface for all visual output in the CLI,
    automatically applying the current theme's colors and styles.

    Attributes:
        console: The underlying Rich Console instance.
        theme: The current ColorScheme.
        light_mode: Whether light mode is active.
        no_color: Whether colors are disabled.
    """

    def __init__(
        self,
        theme: ThemeName | str = ThemeName.DEFAULT,
        *,
        light_mode: Optional[bool] = None,
        no_color: Optional[bool] = None,
        console: Optional[Console] = None,
    ) -> None:
        """Initialize VisualContext.

        Args:
            theme: Theme name or ThemeName enum.
            light_mode: Override light mode detection.
            no_color: Override no-color detection.
            console: Optional existing Console instance.
        """
        if isinstance(theme, str):
            try:
                theme = ThemeName(theme.lower())
            except ValueError:
                theme = ThemeName.DEFAULT

        self.theme: ColorScheme = THEMES[theme]
        self.theme_name: ThemeName = theme
        self.light_mode: bool = light_mode if light_mode is not None else detect_light_mode()
        self.no_color: bool = no_color if no_color is not None else detect_no_color()
        self.console: Console = console or Console(
            force_terminal=not self.no_color,
            no_color=self.no_color,
        )

        # Pre-built style objects
        self.panel_styles = PANEL_STYLES
        self.table_styles = TABLE_STYLES
        self.prompt_styles = PromptStyles()
        self.status_indicators = StatusIndicators()

    def _color(self, role: str) -> str:
        """Get a color from the current theme, respecting light mode."""
        if self.no_color:
            return ""
        return self.theme.get(role, light=self.light_mode)

    def _resolve_style(self, style: str) -> str:
        """Resolve a semantic style name to an actual Rich-compatible style string.

        If the style is a known semantic role (primary, success, etc.), returns
        the theme color. Otherwise returns the style as-is (for Rich built-ins
        like "bold", "dim", "cyan", etc.).
        """
        # Check if it's a semantic role
        if style in ("primary", "secondary", "success", "warning", "error", "info", "muted", "text", "accent", "border", "highlight", "dim"):
            if self.no_color:
                return ""
            color = self.theme.get(style, light=self.light_mode)
            return color if color else style
        return style

    def _style(self, role: str, **kwargs: Any) -> Style:
        """Build a Rich Style from a color role."""
        if self.no_color:
            return Style(**kwargs)
        return self.theme.style(role, light=self.light_mode, **kwargs)

    def style(self, role: str, **kwargs: Any) -> str:
        """Get a Rich markup string for a color role.

        Returns a string like "[bold cyan]" for use in Rich markup.
        """
        if self.no_color:
            return ""
        color = self.theme.get(role, light=self.light_mode)
        parts = [color] if color else []
        if kwargs.get("bold"):
            parts.append("bold")
        if kwargs.get("italic"):
            parts.append("italic")
        if kwargs.get("underline"):
            parts.append("underline")
        if kwargs.get("dim"):
            parts.append("dim")
        if kwargs.get("reverse"):
            parts.append("reverse")
        return " ".join(parts) if parts else ""

    # ─── Panel ──────────────────────────────────────────────────────────

    def panel(
        self,
        content: Any,
        *,
        style: str = "info",
        title: Optional[str] = None,
        title_align: str = "left",
        border_style: Optional[str] = None,
        padding: Optional[tuple[int, int]] = None,
        expand: bool = True,
        **kwargs: Any,
    ) -> Panel:
        """Create a themed Panel.

        Args:
            content: Panel content (str, Text, or Rich renderable).
            style: Semantic style name (info/success/warning/error/agent/tool/muted).
            title: Optional panel title.
            title_align: Title alignment.
            border_style: Override border style (Rich box name).
            padding: Override padding (vertical, horizontal).
            expand: Whether panel expands to full width.
            **kwargs: Additional Panel constructor args.

        Returns:
            A Rich Panel with theme-applied colors.
        """
        ps = self.panel_styles.get(style, self.panel_styles["info"])

        border_color = self._color(ps.border_color or style)
        title_color = self._color(ps.title_color or style)

        box_style = getattr(box, border_style or ps.border_style, box.ROUNDED)

        return Panel(
            content,
            title=title,
            title_align=title_align,
            border_style=border_color,
            box=box_style,
            padding=padding or ps.padding,
            expand=expand,
            **kwargs,
        )

    def print_panel(
        self,
        content: Any,
        *,
        style: str = "info",
        title: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Print a themed panel to the console."""
        self.console.print(self.panel(content, style=style, title=title, **kwargs))

    # ─── Table ──────────────────────────────────────────────────────────

    def table(
        self,
        *,
        style: str = "standard",
        title: Optional[str] = None,
        columns: Optional[list[tuple[str, str]]] = None,
        **kwargs: Any,
    ) -> Table:
        """Create a themed Table.

        Args:
            style: Table style name (standard/compact/status).
            title: Optional table title.
            columns: List of (header, style) tuples.
            **kwargs: Additional Table constructor args.

        Returns:
            A Rich Table with theme-applied colors.
        """
        ts = self.table_styles.get(style, self.table_styles["standard"])

        header_color = self._color("primary")
        border_color = self._color("muted")

        box_style = getattr(box, ts.box_style, box.ROUNDED)

        table = Table(
            title=title,
            box=box_style,
            show_header=ts.show_header,
            show_edge=ts.show_edge,
            show_lines=ts.show_lines,
            pad_edge=ts.pad_edge,
            collapse_padding=ts.collapse_padding,
            title_justify=ts.title_justify,
            **kwargs,
        )

        # Apply header style
        if header_color:
            table.header_style = f"bold {header_color}"

        # Apply border style
        if border_color:
            table.border_style = border_color

        # Apply row styles
        if ts.row_styles:
            table.row_styles = list(ts.row_styles)

        # Add columns
        if columns:
            for header, col_style in columns:
                # Resolve semantic color names to actual colors
                resolved_style = self._resolve_style(col_style or "text")
                table.add_column(header, style=resolved_style)

        return table

    def print_table(
        self,
        rows: list[list[str]],
        *,
        style: str = "standard",
        title: Optional[str] = None,
        columns: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> None:
        """Create and print a themed table with rows.

        Args:
            rows: List of row data (each row is a list of cell strings).
            style: Table style name.
            title: Optional table title.
            columns: Optional column headers.
            **kwargs: Additional Table constructor args.
        """
        col_defs = None
        if columns:
            col_defs = [(col, "text") for col in columns]

        table = self.table(style=style, title=title, columns=col_defs, **kwargs)
        for row in rows:
            table.add_row(*row)
        self.console.print(table)

    # ─── Status Indicators ──────────────────────────────────────────────

    def status_dot(self, level: str | StatusLevel) -> Text:
        """Create a colored status dot.

        Args:
            level: Status level (success/warning/error/info/muted/primary/secondary).

        Returns:
            Rich Text with a colored dot symbol.
        """
        if isinstance(level, StatusLevel):
            level = level.value

        color = self._color(level)
        symbol = "●"
        return Text(symbol, style=color)

    def status_badge(self, level: str | StatusLevel, label: Optional[str] = None) -> Text:
        """Create a status badge like [OK] or [WARN].

        Args:
            level: Status level.
            label: Optional custom label (overrides default).

        Returns:
            Rich Text with a badge.
        """
        if isinstance(level, StatusLevel):
            level = level.value

        color = self._color(level)
        si = self.status_indicators

        badge_map = {
            "success": si.badge_success,
            "warning": si.badge_warning,
            "error": si.badge_error,
            "info": si.badge_info,
            "muted": si.badge_muted,
        }
        symbol = badge_map.get(level, si.badge_muted)
        text = label or symbol

        return Text(f"{si.badge_prefix}{text}{si.badge_suffix}", style=color)

    def status_label(self, level: str | StatusLevel, label: Optional[str] = None) -> Text:
        """Create a status label like "active" or "pending".

        Args:
            level: Status level.
            label: Optional custom label.

        Returns:
            Rich Text with a colored label.
        """
        if isinstance(level, StatusLevel):
            level = level.value

        color = self._color(level)
        si = self.status_indicators

        label_map = {
            "success": si.label_success,
            "warning": si.label_warning,
            "error": si.label_error,
            "info": si.label_info,
            "muted": si.label_muted,
        }
        text = label or label_map.get(level, level)

        return Text(text, style=color)

    def status(
        self,
        level: str | StatusLevel,
        message: str,
        *,
        indicator: str = "dot",
    ) -> Text:
        """Create a full status line with indicator and message.

        Args:
            level: Status level.
            message: Status message.
            indicator: Indicator type (dot/badge/label/none).

        Returns:
            Rich Text with indicator + message.
        """
        if isinstance(level, StatusLevel):
            level = level.value

        parts: list[Text] = []

        if indicator == "dot":
            parts.append(self.status_dot(level))
        elif indicator == "badge":
            parts.append(self.status_badge(level))
        elif indicator == "label":
            parts.append(self.status_label(level))

        if indicator != "none":
            parts.append(Text(" "))

        color = self._color(level)
        parts.append(Text(message, style=color))

        # Combine
        result = Text()
        for part in parts:
            result.append_text(part)
        return result

    def print_status(
        self,
        level: str | StatusLevel,
        message: str,
        *,
        indicator: str = "dot",
    ) -> None:
        """Print a status line."""
        self.console.print(self.status(level, message, indicator=indicator))

    # ─── Prompt ─────────────────────────────────────────────────────────

    def prompt_text(
        self,
        session_id: Optional[str] = None,
        *,
        model: Optional[str] = None,
    ) -> Text:
        """Build the prompt display text.

        Args:
            session_id: Optional session ID to display.
            model: Optional model name to display.

        Returns:
            Rich Text with the full prompt.
        """
        ps = self.prompt_styles
        parts: list[Text] = []

        # Symbol
        parts.append(Text(ps.symbol, style=self.style("primary", bold=True)))

        # Session ID
        if session_id:
            short_id = session_id[:8]
            session_str = ps.session_format.format(id=short_id)
            parts.append(Text(f" {session_str}", style=self.style("secondary", dim=True)))

        # Model
        if model:
            parts.append(Text(f" [{model}]", style=self.style("muted")))

        # Separator
        parts.append(Text(f" {ps.separator} ", style=self.style("text", bold=True)))

        result = Text()
        for part in parts:
            result.append_text(part)
        return result

    def print_prompt(self, session_id: Optional[str] = None, **kwargs: Any) -> None:
        """Print the prompt line."""
        self.console.print(self.prompt_text(session_id, **kwargs), end="")

    # ─── Spinner / Progress ─────────────────────────────────────────────

    def spinner_text(self, message: str = "Thinking...", frame: int = 0) -> Text:
        """Create a spinner frame with message.

        Args:
            message: Message to display next to spinner.
            frame: Spinner frame index.

        Returns:
            Rich Text with spinner + message.
        """
        si = self.status_indicators
        frames = si.spinner_frames
        symbol = frames[frame % len(frames)]
        color = self._color("primary")

        result = Text()
        result.append(symbol, style=color)
        result.append(f" {message}", style=self._style("text"))
        return result

    def progress_bar(
        self,
        current: int,
        total: int,
        *,
        width: int = 20,
        message: Optional[str] = None,
        show_percentage: bool = True,
    ) -> Text:
        """Create a progress bar.

        Args:
            current: Current progress value.
            total: Total value (for percentage calculation).
            width: Bar width in characters.
            message: Optional message to prepend.
            show_percentage: Whether to show percentage.

        Returns:
            Rich Text with progress bar.
        """
        si = self.status_indicators
        if total <= 0:
            total = 1
        ratio = min(current / total, 1.0)
        filled = int(width * ratio)
        empty = width - filled

        bar_color = self._color("primary")
        empty_color = self._color("muted")

        result = Text()
        if message:
            result.append(f"{message} ", style=self._style("text"))

        result.append("[", style=self._style("muted"))
        result.append(si.bar_fill * filled, style=bar_color)
        result.append(si.bar_empty * empty, style=empty_color)
        result.append("]", style=self._style("muted"))

        if show_percentage:
            pct = int(ratio * 100)
            result.append(f" {pct}%", style=self._style("text"))

        return result

    # ─── Utility ────────────────────────────────────────────────────────

    def separator(self, char: str = "─", width: Optional[int] = None) -> Text:
        """Create a horizontal separator line.

        Args:
            char: Character to use for the separator.
            width: Width of separator (default: console width).

        Returns:
            Rich Text with separator.
        """
        if width is None:
            width = self.console.width
        color = self._color("muted")
        return Text(char * width, style=color)

    def print_separator(self, char: str = "─", width: Optional[int] = None) -> None:
        """Print a separator line."""
        self.console.print(self.separator(char, width))

    def header(self, text: str, *, level: int = 1) -> Text:
        """Create a header text with appropriate styling.

        Args:
            text: Header text.
            level: Header level (1-3), affects size/style.

        Returns:
            Rich Text with header styling.
        """
        if level == 1:
            return Text(text, style=self.style("primary", bold=True))
        elif level == 2:
            return Text(text, style=self.style("secondary", bold=True))
        else:
            return Text(text, style=self.style("text", bold=True))

    def print_header(self, text: str, *, level: int = 1) -> None:
        """Print a header."""
        self.console.print(self.header(text, level=level))

    def muted(self, text: str) -> Text:
        """Create muted/dimmed text."""
        return Text(text, style=self.style("muted", dim=True))

    def highlight(self, text: str) -> Text:
        """Create highlighted/accent text."""
        return Text(text, style=self.style("accent", bold=True))

    def success(self, text: str) -> Text:
        """Create success-colored text."""
        return Text(text, style=self.style("success", bold=True))

    def error(self, text: str) -> Text:
        """Create error-colored text."""
        return Text(text, style=self.style("error", bold=True))

    def warning(self, text: str) -> Text:
        """Create warning-colored text."""
        return Text(text, style=self.style("warning", bold=True))

    def info(self, text: str) -> Text:
        """Create info-colored text."""
        return Text(text, style=self.style("info"))

    # ─── Theme Management ───────────────────────────────────────────────

    def set_theme(self, theme: ThemeName | str) -> None:
        """Switch to a different theme.

        Args:
            theme: New theme name.
        """
        if isinstance(theme, str):
            try:
                theme = ThemeName(theme.lower())
            except ValueError:
                return
        self.theme = THEMES[theme]
        self.theme_name = theme

    def set_light_mode(self, enabled: bool) -> None:
        """Enable or disable light mode."""
        self.light_mode = enabled

    def set_no_color(self, enabled: bool) -> None:
        """Enable or disable colors."""
        self.no_color = enabled
        self.console.no_color = enabled

    def get_theme_names(self) -> list[str]:
        """Get list of available theme names."""
        return [t.value for t in ThemeName]

    # ─── Specialized Table Methods ─────────────────────────────────────

    def print_sessions_table(self, sessions: list[Any]) -> None:
        """Print a beautiful sessions table."""
        table = self.table(title="Sessions")
        table.add_column("ID", style=self._resolve_style("primary"), no_wrap=True)
        table.add_column("Title", style=self._resolve_style("text"))
        table.add_column("Status", style=self._resolve_style("success"))
        table.add_column("Agent", style=self._resolve_style("muted"))
        table.add_column("Goal", style=self._resolve_style("muted"))
        table.add_column("Last Activity", style=self._resolve_style("muted"))

        for s in sessions:
            table.add_row(
                str(s.id)[:8],
                s.title or "(untitled)",
                s.status,
                s.agent_id,
                (s.goal or "")[:40],
                s.last_activity.strftime("%Y-%m-%d %H:%M"),
            )

        self.console.print(table)

    def print_context_table(self, chunks: list[Any], session_id: str) -> None:
        """Print a beautiful context chunks table."""
        table = self.table(title=f"Context Chunks (session {str(session_id)[:8]})")
        table.add_column("Type", style=self._resolve_style("primary"))
        table.add_column("Agent", style=self._resolve_style("success"))
        table.add_column("Tokens", style=self._resolve_style("warning"))
        table.add_column("Created", style=self._resolve_style("muted"))

        for c in chunks:
            table.add_row(
                c.chunk_type,
                c.agent_id,
                str(c.token_count),
                c.created_at.strftime("%H:%M:%S"),
            )

        self.console.print(table)

    def print_skills_table(self, skills: list[Any]) -> None:
        """Print a beautiful skills table."""
        table = self.table(title=f"Skills ({len(skills)} loaded)")
        table.add_column("Name", style=self._resolve_style("primary"))
        table.add_column("Description", style=self._resolve_style("text"))
        table.add_column("Triggers", style=self._resolve_style("muted"))
        table.add_column("Uses", style=self._resolve_style("warning"))
        table.add_column("Views", style=self._resolve_style("success"))
        table.add_column("Last Activity", style=self._resolve_style("muted"))

        for s in skills:
            last_act = (
                s.last_activity_at.strftime("%Y-%m-%d %H:%M")
                if s.last_activity_at
                else "never"
            )
            table.add_row(
                s.name,
                s.description[:60],
                ", ".join(s.triggers[:3]),
                str(s.use_count),
                str(s.view_count),
                last_act,
            )

        self.console.print(table)

    def print_status_panel(self, status_data: dict[str, str]) -> None:
        """Print a status panel."""
        table = self.table(title="Status")
        table.add_column("Key", style=self._resolve_style("primary"))
        table.add_column("Value", style=self._resolve_style("text"))

        for key, value in status_data.items():
            table.add_row(key, value)

        self.console.print(table)

    def print_config_table(self, config_dict: dict[str, Any]) -> None:
        """Print a configuration table."""
        table = self.table(title="Configuration")
        table.add_column("Key", style=self._resolve_style("primary"))
        table.add_column("Value", style=self._resolve_style("text"))

        for key, value in config_dict.items():
            table.add_row(key, str(value))

        self.console.print(table)

    def print_memory_table(self, memories: list[Any]) -> None:
        """Print a memories table."""
        table = self.table(title=f"Memories ({len(memories)})")
        table.add_column("ID", style=self._resolve_style("primary"), no_wrap=True)
        table.add_column("Category", style=self._resolve_style("success"))
        table.add_column("Importance", style=self._resolve_style("warning"))
        table.add_column("Content", style=self._resolve_style("text"))

        for m in memories:
            table.add_row(
                str(m.id)[:8],
                m.category,
                f"{m.importance:.2f}",
                m.content[:60],
            )

        self.console.print(table)

    # ─── Response Display ───────────────────────────────────────────────

    def print_response_panel(self, response_text: str, title: str = "Agent") -> None:
        """Print a response in a beautiful panel."""
        from rich.markdown import Markdown

        panel = self.panel(
            Markdown(response_text),
            style="agent",
            title=title,
        )
        self.console.print(panel)

    def print_metadata(self, iterations: int, tool_calls: int, tokens: int) -> None:
        """Print response metadata."""
        self.console.print(
            f"  Iterations: {iterations} | Tool calls: {tool_calls} | Tokens: {tokens}",
            style=self.style("muted", dim=True),
        )

    def print_session_id(self, session_id: str) -> None:
        """Print session ID in muted style."""
        self.console.print(f"  Session ID: {session_id}", style=self.style("muted", dim=True))

    # ─── Status Display ──────────────────────────────────────────────────

    def error(self, message: str, title: str = "Error", suggestion: str = "") -> None:
        """Print an error message with red styling.

        Args:
            message: Error message
            title: Error panel title
            suggestion: Optional suggestion for recovery
        """
        content = Text()
        content.append("✗ ", style=self.style("error", bold=True))
        content.append(message, style=self._style("text"))
        if suggestion:
            content.append(f"\n\n💡 {suggestion}", style=self._style("info"))

        panel = Panel(
            content,
            title=title,
            border_style=self._color("error"),
            box=box.ROUNDED,
        )
        self.console.print(panel)

    def print_status_line(self, label: str, value: str, status: str = "info") -> None:
        """Print a status line with color-coded value.

        Args:
            label: Status label
            value: Status value
            status: Status type (success, error, warning, info)
        """
        color = self._color(status)
        self.console.print(f"  {label}: [{color}]{value}[/{color}]")

    def print_divider(self, char: str = "─", length: int = 60) -> None:
        """Print a divider line."""
        self.console.print(char * length, style=self._color("muted"))

    def print_header(self, text: str) -> None:
        """Print a section header."""
        self.console.print()
        self.console.print(f"  {text}", style=self.style("primary", bold=True))
        self.console.print(f"  {'─' * len(text)}", style=self._color("muted"))

    def __repr__(self) -> str:
        return (
            f"VisualContext(theme={self.theme_name.value!r}, "
            f"light_mode={self.light_mode}, no_color={self.no_color})"
        )


# ─── Convenience Functions ──────────────────────────────────────────────────


def get_visual_context(
    theme: Optional[str] = None,
    *,
    light_mode: Optional[bool] = None,
    no_color: Optional[bool] = None,
) -> VisualContext:
    """Get a VisualContext with the specified or default theme.

    Args:
        theme: Theme name (default: from AGENT_HARNESS_THEME env var or "default").
        light_mode: Override light mode detection.
        no_color: Override no-color detection.

    Returns:
        Configured VisualContext instance.
    """
    if theme is None:
        theme = os.environ.get("AGENT_HARNESS_THEME", "default")
    return VisualContext(theme, light_mode=light_mode, no_color=no_color)


# ─── Module-level default instance ──────────────────────────────────────────

_default_viz: Optional[VisualContext] = None


def get_default_visual() -> VisualContext:
    """Get or create the module-level default VisualContext."""
    global _default_viz
    if _default_viz is None:
        _default_viz = get_visual_context()
    return _default_viz


# ─── Exports ────────────────────────────────────────────────────────────────

__all__ = [
    "ColorScheme",
    "PanelStyles",
    "PromptStyles",
    "StatusIndicators",
    "StatusLevel",
    "TableStyles",
    "ThemeName",
    "VisualContext",
    "THEMES",
    "detect_light_mode",
    "detect_no_color",
    "get_visual_context",
    "get_default_visual",
]
