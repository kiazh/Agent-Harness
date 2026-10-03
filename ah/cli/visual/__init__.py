"""
AgentHarness Visual Design System
=================================

Comprehensive visual theming for the `ah` CLI.

This package is split into focused submodules:
  - ah.cli.visual.colors: OKLCH/OKHSL color space, gamut mapping, tint, contrast
  - ah.cli.visual.themes: ColorScheme, ThemeName, built-in themes, system theme
  - ah.cli.visual.panels: PanelStyles
  - ah.cli.visual.tables: TableStyles
  - ah.cli.visual.styles: PromptStyles, StatusLevel, StatusIndicators
  - ah.cli.visual.banners: per-skin banner ASCII art
  - ah.cli.visual.detection: light-mode and no-color detection
  - ah.cli.visual.integrations: prompt_toolkit styles, syntax highlighting
  - ah.cli.visual.context: VisualContext wrapper

Usage:
    from ah.cli.visual import VisualContext, ThemeName

    viz = VisualContext()
    viz.panel("Hello", style="info")
    viz.status("active", level="success")
"""
from __future__ import annotations

from ah.cli.visual.colors import (
    srgb_to_oklab,
    oklab_to_srgb,
    oklch_to_oklab,
    oklab_to_oklch,
    oklch_to_srgb,
    srgb_to_oklch,
    gamut_map_bisection,
    hex_to_rgb,
    rgb_to_hex,
    parse_color,
    tint,
    relative_luminance,
    contrast_ratio,
)
from ah.cli.visual.themes import (
    ThemeName,
    ColorScheme,
    THEMES,
)
from ah.cli.visual.panels import PanelStyles, PANEL_STYLES
from ah.cli.visual.tables import TableStyles, TABLE_STYLES
from ah.cli.visual.styles import (
    PromptStyles,
    StatusLevel,
    StatusIndicators,
)
from ah.cli.visual.banners import BANNER_LOGO, BANNER_HERO
from ah.cli.visual.detection import (
    detect_light_mode,
    detect_light_mode_osc,
    detect_no_color,
)
from ah.cli.visual.integrations import (
    generate_prompt_toolkit_style,
    generate_syntax_highlight_rules,
)
from ah.cli.visual.context import (
    VisualContext,
    get_visual_context,
    get_default_visual,
)

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
    "PANEL_STYLES",
    "TABLE_STYLES",
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
