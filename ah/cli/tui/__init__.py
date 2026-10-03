"""AgentHarness TUI — a Python port of the pi coding-agent terminal UI.

Adapted from earendil-works/pi (MIT). See LICENSE.pi for attribution.

A minimal, flicker-free terminal UI framework with differential rendering,
synchronized output, an OKHSL/OKLCH color engine, pi's dark/light themes, a
component protocol, flexbox-style layout, and built-in widgets.
"""
from __future__ import annotations

from ah.cli.tui.colors import (
    Color,
    IndexedColor,
    OklchColor,
    RgbColor,
    TextStyle,
    background_ansi,
    color_to_hex,
    color_to_rgb,
    foreground_ansi,
    mix_colors,
    parse_color,
    style_text,
)
from ah.cli.tui.component import CURSOR_MARKER, Component, Container, Focusable, is_focusable
from ah.cli.tui.components import (
    Box,
    Editor,
    Input,
    Loader,
    SelectItem,
    SelectList,
    Spacer,
    Text,
    TruncatedText,
)
from ah.cli.tui.keys import Key, matches_key, parse_key
from ah.cli.tui.layout import HStack, ScrollView, StackEntry, VStack
from ah.cli.tui.renderer import TuiMainScreen
from ah.cli.tui.terminal import ProcessTerminal, Terminal
from ah.cli.tui.theme import Theme, get_theme, list_themes
from ah.cli.tui.utils import (
    strip_ansi,
    truncate_to_width,
    visible_width,
    wrap_text_with_ansi,
)

__all__ = [
    # colors
    "Color", "IndexedColor", "RgbColor", "OklchColor", "TextStyle",
    "parse_color", "color_to_rgb", "color_to_hex", "mix_colors",
    "style_text", "foreground_ansi", "background_ansi",
    # component
    "Component", "Container", "Focusable", "is_focusable", "CURSOR_MARKER",
    # components
    "Text", "TruncatedText", "Spacer", "Box", "Input", "Editor",
    "Loader", "SelectItem", "SelectList",
    # keys
    "Key", "matches_key", "parse_key",
    # layout
    "VStack", "HStack", "ScrollView", "StackEntry",
    # renderer / terminal
    "TuiMainScreen", "ProcessTerminal", "Terminal",
    # theme
    "Theme", "get_theme", "list_themes",
    # utils
    "visible_width", "truncate_to_width", "wrap_text_with_ansi", "strip_ansi",
]
