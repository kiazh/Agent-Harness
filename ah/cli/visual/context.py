"""VisualContext: theme-aware Rich Console wrapper."""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.style import Style
from rich import box

from ah.cli.visual.colors import (
    contrast_ratio,
    hex_to_rgb,
    parse_color,
    relative_luminance,
    rgb_to_hex,
    tint,
)
from ah.cli.visual.panels import PanelStyles, PANEL_STYLES
from ah.cli.visual.tables import TableStyles, TABLE_STYLES
from ah.cli.visual.banners import BANNER_LOGO, BANNER_HERO
from ah.cli.visual.themes import ColorScheme, ThemeName, THEMES
from ah.cli.visual.styles import PromptStyles, StatusIndicators, StatusLevel
from ah.cli.visual.detection import (
    detect_light_mode,
    detect_light_mode_osc,
    detect_no_color,
)
from ah.cli.visual.integrations import (
    generate_prompt_toolkit_style,
    generate_syntax_highlight_rules,
)

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

        # Color remapping hook
        self._color_remapper: Optional[Callable[[str, str], str]] = None

    def _color(self, role: str) -> str:
        """Get a color from the current theme, respecting light mode."""
        if self.no_color:
            return ""
        color = self.theme.get(role, light=self.light_mode)
        if self._color_remapper and color:
            color = self._color_remapper(role, color)
        return color

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
            if self._color_remapper and color:
                color = self._color_remapper(style, color)
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
        if self._color_remapper and color:
            color = self._color_remapper(role, color)
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

    # ─── Color Remapping Hook ───────────────────────────────────────────

    def set_color_remapper(self, remapper: Optional[Callable[[str, str], str]]) -> None:
        """Set a color remapping hook.

        The remapper is called with (role, color) and should return
        a modified color string. Set to None to disable.

        Args:
            remapper: Callable(role: str, color: str) -> str, or None.
        """
        self._color_remapper = remapper

    def get_color_remapper(self) -> Optional[Callable[[str, str], str]]:
        """Get the current color remapper."""
        return self._color_remapper

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

    # ─── Banner Art ─────────────────────────────────────────────────────

    def get_banner_logo(self, theme_name: Optional[str] = None) -> list[str]:
        """Get the banner logo ASCII art for a theme.

        Args:
            theme_name: Theme name (default: current theme).

        Returns:
            List of strings representing the ASCII art lines.
        """
        name = (theme_name or self.theme_name.value).lower()
        return BANNER_LOGO.get(name, BANNER_LOGO["default"])

    def get_banner_hero(self, theme_name: Optional[str] = None) -> list[str]:
        """Get the banner hero ASCII art for a theme.

        Args:
            theme_name: Theme name (default: current theme).

        Returns:
            List of strings representing the ASCII art lines.
        """
        name = (theme_name or self.theme_name.value).lower()
        return BANNER_HERO.get(name, BANNER_HERO["default"])

    def print_banner_logo(self, theme_name: Optional[str] = None) -> None:
        """Print the banner logo for a theme."""
        for line in self.get_banner_logo(theme_name):
            self.console.print(line, style=self.style("primary"))

    def print_banner_hero(self, theme_name: Optional[str] = None) -> None:
        """Print the banner hero for a theme."""
        for line in self.get_banner_hero(theme_name):
            self.console.print(line, style=self.style("text"))

    # ─── prompt_toolkit Style ───────────────────────────────────────────

    def get_prompt_toolkit_style(self) -> dict[str, str]:
        """Get prompt_toolkit style dictionary for the current theme."""
        return generate_prompt_toolkit_style(self.theme, light=self.light_mode)

    # ─── Syntax Highlighting ────────────────────────────────────────────

    def get_syntax_highlight_rules(self) -> dict[str, str]:
        """Get syntax highlighting rules for the current theme."""
        return generate_syntax_highlight_rules(self.theme, light=self.light_mode)

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
                str(s.usage_count),
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
        if color:
            self.console.print(f"  {label}: [{color}]{value}[/{color}]")
        else:
            self.console.print(f"  {label}: {value}")

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
    "BANNER_LOGO",
    "BANNER_HERO",
    "detect_light_mode",
    "detect_light_mode_osc",
    "detect_no_color",
    "get_visual_context",
    "get_default_visual",
    "tint",
    "contrast_ratio",
    "relative_luminance",
    "parse_color",
    "hex_to_rgb",
    "rgb_to_hex",
    "srgb_to_oklab",
    "oklab_to_srgb",
    "oklch_to_oklab",
    "oklab_to_oklch",
    "oklch_to_srgb",
    "srgb_to_oklch",
    "gamut_map_bisection",
    "generate_prompt_toolkit_style",
    "generate_syntax_highlight_rules",
]
