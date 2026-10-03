"""Theme definitions: ThemeName, ColorScheme, built-in themes, system theme."""
from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.style import Style
from rich import box

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
from ah.cli.visual.panels import PanelStyles, PANEL_STYLES
from ah.cli.visual.tables import TableStyles, TABLE_STYLES
from ah.cli.visual.banners import BANNER_LOGO, BANNER_HERO

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
    SYSTEM = "system"



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


# ─── System Theme ────────────────────────────────────────────────────────────


# ANSI color family definitions: (hue_center, hue_range, saturation_boost)
_COLOR_FAMILIES: dict[str, tuple[float, float, float]] = {
    "red": (0.0, 15.0, 1.0),
    "orange": (30.0, 15.0, 1.0),
    "yellow": (60.0, 15.0, 1.0),
    "green": (120.0, 30.0, 0.9),
    "cyan": (180.0, 20.0, 0.9),
    "blue": (240.0, 30.0, 1.0),
    "purple": (270.0, 20.0, 1.0),
    "magenta": (300.0, 20.0, 1.0),
}


def _classify_ansi_color(r: int, g: int, b: int) -> str:
    """Classify an RGB color into a color family name."""
    L, C, H = srgb_to_oklch(r / 255.0, g / 255.0, b / 255.0)
    if C < 0.02:
        return "neutral"

    best_family = "blue"
    best_dist = 360.0
    for family, (hue_center, hue_range, _) in _COLOR_FAMILIES.items():
        dist = min(abs(H - hue_center), 360 - abs(H - hue_center))
        if dist < best_dist:
            best_dist = dist
            best_family = family
    return best_family


def _polynomial_contrast_curve(t: float, contrast: float = 4.5) -> float:
    """Polynomial contrast curve for light/dark adaptation.

    Maps t in [0, 1] to a contrast-adjusted value using a polynomial curve
    that ensures minimum contrast ratio.
    """
    # Use a power curve that pushes values away from middle gray
    if t < 0.5:
        # Darken: t^gamma where gamma > 1
        gamma = 1.0 + (contrast / 10.0)
        return t ** gamma * 0.5
    else:
        # Lighten: 1 - (1-t)^gamma
        gamma = 1.0 + (contrast / 10.0)
        return 1.0 - (1.0 - t) ** gamma * 0.5


def _derive_system_scheme(
    ansi_palette: Optional[list[tuple[int, int, int]]] = None,
    light: bool = False,
) -> ColorScheme:
    """Derive a ColorScheme from the terminal's ANSI palette.

    Uses color families and polynomial contrast curves to generate
    a cohesive theme from the terminal's 16-color palette.
    """
    if ansi_palette is None:
        # Default xterm palette
        ansi_palette = [
            (0, 0, 0),       # black
            (205, 0, 0),     # red
            (0, 205, 0),     # green
            (205, 205, 0),   # yellow
            (0, 0, 238),     # blue
            (205, 0, 205),   # magenta
            (0, 205, 205),   # cyan
            (229, 229, 229), # white
            (127, 127, 127), # bright black
            (255, 0, 0),     # bright red
            (0, 255, 0),     # bright green
            (255, 255, 0),   # bright yellow
            (92, 92, 255),   # bright blue
            (255, 0, 255),   # bright magenta
            (0, 255, 255),   # bright cyan
            (255, 255, 255), # bright white
        ]

    # Classify each color into a family
    families: dict[str, list[tuple[int, int, int]]] = {}
    for r, g, b in ansi_palette:
        family = _classify_ansi_color(r, g, b)
        families.setdefault(family, []).append((r, g, b))

    def _avg_color(colors: list[tuple[int, int, int]]) -> tuple[int, int, int]:
        if not colors:
            return (128, 128, 128)
        r = sum(c[0] for c in colors) // len(colors)
        g = sum(c[1] for c in colors) // len(colors)
        b = sum(c[2] for c in colors) // len(colors)
        return (r, g, b)

    def _adjust_lightness(r: int, g: int, b: int, factor: float) -> tuple[int, int, int]:
        """Adjust lightness using polynomial contrast curve."""
        L, C, H = srgb_to_oklch(r / 255.0, g / 255.0, b / 255.0)
        L_new = _polynomial_contrast_curve(L, contrast=4.5)
        L_new = max(0.0, min(1.0, L_new * factor))
        L_new, C, H = gamut_map_bisection(L_new, C, H)
        nr, ng, nb = oklch_to_srgb(L_new, C, H)
        return (
            int(round(nr * 255)),
            int(round(ng * 255)),
            int(round(nb * 255)),
        )

    def _to_hex(r: int, g: int, b: int) -> str:
        return rgb_to_hex(r, g, b)

    # Derive semantic colors from families
    blue = _avg_color(families.get("blue", [(0, 0, 255)]))
    green = _avg_color(families.get("green", [(0, 255, 0)]))
    red = _avg_color(families.get("red", [(255, 0, 0)]))
    yellow = _avg_color(families.get("yellow", [(255, 255, 0)]))
    cyan = _avg_color(families.get("cyan", [(0, 255, 255)]))
    magenta = _avg_color(families.get("magenta", [(255, 0, 255)]))
    neutral = _avg_color(families.get("neutral", [(128, 128, 128)]))

    if light:
        # Light mode: darker text on light background
        bg = (250, 250, 250)
        text = _adjust_lightness(*neutral, factor=0.15)
        muted = _adjust_lightness(*neutral, factor=0.45)
        primary = _adjust_lightness(*blue, factor=0.4)
        secondary = _adjust_lightness(*magenta, factor=0.4)
        success = _adjust_lightness(*green, factor=0.4)
        warning = _adjust_lightness(*yellow, factor=0.4)
        error = _adjust_lightness(*red, factor=0.4)
        info = _adjust_lightness(*cyan, factor=0.4)
        accent = _adjust_lightness(*yellow, factor=0.3)
        border = _adjust_lightness(*neutral, factor=0.6)
        highlight = (240, 240, 240)
        dim = _adjust_lightness(*neutral, factor=0.55)
    else:
        # Dark mode: light text on dark background
        bg = (18, 18, 18)
        text = _adjust_lightness(*neutral, factor=0.9)
        muted = _adjust_lightness(*neutral, factor=0.6)
        primary = _adjust_lightness(*blue, factor=1.2)
        secondary = _adjust_lightness(*magenta, factor=1.2)
        success = _adjust_lightness(*green, factor=1.2)
        warning = _adjust_lightness(*yellow, factor=1.2)
        error = _adjust_lightness(*red, factor=1.2)
        info = _adjust_lightness(*cyan, factor=1.2)
        accent = _adjust_lightness(*yellow, factor=1.3)
        border = _adjust_lightness(*neutral, factor=0.4)
        highlight = (40, 40, 40)
        dim = _adjust_lightness(*neutral, factor=0.7)

    return ColorScheme(
        primary=_to_hex(*primary),
        secondary=_to_hex(*secondary),
        success=_to_hex(*success),
        warning=_to_hex(*warning),
        error=_to_hex(*error),
        info=_to_hex(*info),
        muted=_to_hex(*muted),
        text=_to_hex(*text),
        background=_to_hex(*bg),
        accent=_to_hex(*accent),
        border=_to_hex(*border),
        highlight=_to_hex(*highlight),
        dim=_to_hex(*dim),
        # Light mode overrides
        light_primary=_to_hex(*_adjust_lightness(*blue, factor=0.4)),
        light_secondary=_to_hex(*_adjust_lightness(*magenta, factor=0.4)),
        light_success=_to_hex(*_adjust_lightness(*green, factor=0.4)),
        light_warning=_to_hex(*_adjust_lightness(*yellow, factor=0.4)),
        light_error=_to_hex(*_adjust_lightness(*red, factor=0.4)),
        light_info=_to_hex(*_adjust_lightness(*cyan, factor=0.4)),
        light_muted=_to_hex(*_adjust_lightness(*neutral, factor=0.45)),
        light_text=_to_hex(*_adjust_lightness(*neutral, factor=0.15)),
        light_background="#FAFAFA",
        light_accent=_to_hex(*_adjust_lightness(*yellow, factor=0.3)),
        light_border=_to_hex(*_adjust_lightness(*neutral, factor=0.6)),
        light_highlight="#F0F0F0",
        light_dim=_to_hex(*_adjust_lightness(*neutral, factor=0.55)),
    )


def _system_scheme() -> ColorScheme:
    """System theme — derives colors from terminal palette."""
    return _derive_system_scheme()


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
    ThemeName.SYSTEM: _system_scheme(),
}



