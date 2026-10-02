"""
AgentHarness Visual Design System
=================================

Comprehensive visual theming for the `ah` CLI. Provides:
  - ColorScheme dataclass with dark/light variants
  - 9 built-in themes (default/gold, crimson, ocean, forest, sunset, midnight, arctic, volcanic, system)
  - OKLCH/OKHSL color space support with gamut mapping via bisection
  - System theme that derives colors from terminal palette using color families + polynomial contrast curves
  - Color blending with tint() function
  - Light mode detection via OSC 11 background queries + luminance
  - Color remapping hook on theme get_color
  - Per-skin banner_logo and banner_hero ASCII art
  - prompt_toolkit style generation from skin palette
  - Syntax highlighting rules generated from theme colors
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


# ─── OKLCH / OKHSL Color Space ───────────────────────────────────────────────


def _srgb_to_linear(c: float) -> float:
    """Convert sRGB channel to linear light."""
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    """Convert linear light channel to sRGB."""
    if c <= 0.0031308:
        return c * 12.92
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055


def srgb_to_oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Convert sRGB (0-1) to OKLab (L, a, b)."""
    r_lin = _srgb_to_linear(r)
    g_lin = _srgb_to_linear(g)
    b_lin = _srgb_to_linear(b)

    l_ = 0.4122214708 * r_lin + 0.5363325363 * g_lin + 0.0514459929 * b_lin
    m_ = 0.2119034982 * r_lin + 0.6806995451 * g_lin + 0.1073969566 * b_lin
    s_ = 0.0883024619 * r_lin + 0.2817188376 * g_lin + 0.6299787005 * b_lin

    l_cbrt = l_ ** (1.0 / 3.0)
    m_cbrt = m_ ** (1.0 / 3.0)
    s_cbrt = s_ ** (1.0 / 3.0)

    L = 0.2104542553 * l_cbrt + 0.7936177850 * m_cbrt - 0.0040720468 * s_cbrt
    a = 1.9779984951 * l_cbrt - 2.4285922050 * m_cbrt + 0.4505937099 * s_cbrt
    b = 0.0259040371 * l_cbrt + 0.7827717662 * m_cbrt - 0.8086757660 * s_cbrt

    return L, a, b


def oklab_to_srgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    """Convert OKLab (L, a, b) to sRGB (0-1)."""
    l_cbrt = L + 0.3963377774 * a + 0.2158037573 * b
    m_cbrt = L - 0.1055613458 * a - 0.0638541728 * b
    s_cbrt = L - 0.0894841775 * a - 1.2914855480 * b

    l_ = l_cbrt ** 3
    m_ = m_cbrt ** 3
    s_ = s_cbrt ** 3

    r_lin = 4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
    g_lin = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
    b_lin = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_

    r = _linear_to_srgb(max(0.0, min(1.0, r_lin)))
    g = _linear_to_srgb(max(0.0, min(1.0, g_lin)))
    b = _linear_to_srgb(max(0.0, min(1.0, b_lin)))

    return r, g, b


def oklch_to_oklab(L: float, C: float, H: float) -> tuple[float, float, float]:
    """Convert OKLCH (L, C, H_degrees) to OKLab (L, a, b)."""
    h_rad = math.radians(H)
    a = C * math.cos(h_rad)
    b = C * math.sin(h_rad)
    return L, a, b


def oklab_to_oklch(L: float, a: float, b: float) -> tuple[float, float, float]:
    """Convert OKLab (L, a, b) to OKLCH (L, C, H_degrees)."""
    C = math.sqrt(a * a + b * b)
    H = math.degrees(math.atan2(b, a)) % 360.0
    return L, C, H


def oklch_to_srgb(L: float, C: float, H: float) -> tuple[float, float, float]:
    """Convert OKLCH to sRGB."""
    lab = oklch_to_oklab(L, C, H)
    return oklab_to_srgb(*lab)


def srgb_to_oklch(r: float, g: float, b: float) -> tuple[float, float, float]:
    """Convert sRGB to OKLCH."""
    lab = srgb_to_oklab(r, g, b)
    return oklab_to_oklch(*lab)


def _is_in_gamut(r: float, g: float, b: float, tolerance: float = 0.0001) -> bool:
    """Check if sRGB values are within gamut."""
    return (-tolerance <= r <= 1.0 + tolerance and
            -tolerance <= g <= 1.0 + tolerance and
            -tolerance <= b <= 1.0 + tolerance)


def gamut_map_bisection(L: float, C: float, H: float, tolerance: float = 0.001) -> tuple[float, float, float]:
    """Map an OKLCH color to sRGB gamut via bisection on chroma.

    Reduces chroma until the color is within sRGB gamut.
    Returns (L, C_mapped, H) in OKLCH.
    """
    if C < tolerance:
        return L, 0.0, H

    # Check if already in gamut
    r, g, b = oklch_to_srgb(L, C, H)
    if _is_in_gamut(r, g, b):
        return L, C, H

    # Bisection on chroma
    lo, hi = 0.0, C
    for _ in range(30):  # 30 iterations gives ~1e-9 precision
        mid = (lo + hi) / 2.0
        r, g, b = oklch_to_srgb(L, mid, H)
        if _is_in_gamut(r, g, b):
            lo = mid
        else:
            hi = mid
        if hi - lo < tolerance:
            break

    return L, lo, H


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert hex color string to RGB tuple."""
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 3:
        hex_color = ''.join(c * 2 for c in hex_color)
    if len(hex_color) != 6:
        return (0, 0, 0)
    return (
        int(hex_color[0:2], 16),
        int(hex_color[2:4], 16),
        int(hex_color[4:6], 16),
    )


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert RGB values to hex string."""
    return f"#{r:02X}{g:02X}{b:02X}"


def parse_color(color: str) -> tuple[int, int, int]:
    """Parse a color string (hex, rgb, or ANSI name) to RGB tuple."""
    color = color.strip()

    # Hex
    if color.startswith('#'):
        return hex_to_rgb(color)

    # RGB tuple
    if color.startswith('rgb(') and color.endswith(')'):
        parts = color[4:-1].split(',')
        if len(parts) == 3:
            return tuple(int(p.strip()) for p in parts)

    # ANSI color name mapping
    ansi_colors = {
        'black': (0, 0, 0), 'red': (255, 0, 0), 'green': (0, 255, 0),
        'yellow': (255, 255, 0), 'blue': (0, 0, 255), 'magenta': (255, 0, 255),
        'cyan': (0, 255, 255), 'white': (255, 255, 255),
        'bright_black': (128, 128, 128), 'bright_red': (255, 128, 128),
        'bright_green': (128, 255, 128), 'bright_yellow': (255, 255, 128),
        'bright_blue': (128, 128, 255), 'bright_magenta': (255, 128, 255),
        'bright_cyan': (128, 255, 255), 'bright_white': (255, 255, 255),
    }
    if color.lower() in ansi_colors:
        return ansi_colors[color.lower()]

    return (0, 0, 0)


def tint(color: str, target: str, amount: float = 0.5) -> str:
    """Blend two colors in OKLCH space.

    Args:
        color: Base color (hex string)
        target: Target color to blend toward (hex string)
        amount: Blend amount (0.0 = color, 1.0 = target)

    Returns:
        Hex string of the blended color.
    """
    amount = max(0.0, min(1.0, amount))
    if amount == 0.0:
        return color
    if amount == 1.0:
        return target

    r1, g1, b1 = parse_color(color)
    r2, g2, b2 = parse_color(target)

    L1, C1, H1 = srgb_to_oklch(r1 / 255.0, g1 / 255.0, b1 / 255.0)
    L2, C2, H2 = srgb_to_oklch(r2 / 255.0, g2 / 255.0, b2 / 255.0)

    # Interpolate in OKLCH
    L = L1 + (L2 - L1) * amount
    C = C1 + (C2 - C1) * amount

    # Shortest path for hue
    dh = H2 - H1
    if dh > 180:
        dh -= 360
    elif dh < -180:
        dh += 360
    H = (H1 + dh * amount) % 360.0

    # Gamut map
    L, C, H = gamut_map_bisection(L, C, H)

    r, g, b = oklch_to_srgb(L, C, H)
    return rgb_to_hex(
        int(round(r * 255)),
        int(round(g * 255)),
        int(round(b * 255)),
    )


def relative_luminance(r: int, g: int, b: int) -> float:
    """Calculate relative luminance per WCAG 2.0."""
    def channel(c: float) -> float:
        c = c / 255.0
        if c <= 0.03928:
            return c / 12.92
        return ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(color1: str, color2: str) -> float:
    """Calculate WCAG contrast ratio between two colors."""
    r1, g1, b1 = parse_color(color1)
    r2, g2, b2 = parse_color(color2)
    l1 = relative_luminance(r1, g1, b1)
    l2 = relative_luminance(r2, g2, b2)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


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


def _query_osc11_background() -> Optional[str]:
    """Query terminal background color via OSC 11 escape sequence.

    Sends OSC 11 query and reads the response from stdin.
    Returns the hex color string or None if query fails.
    """
    import select
    import termios
    import tty

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None

    try:
        # Save terminal settings
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        try:
            # Set raw mode for reading response
            tty.setcbreak(fd)

            # Send OSC 11 query
            sys.stdout.write("\033]11;?\033\\")
            sys.stdout.flush()

            # Wait for response with timeout
            if not select.select([sys.stdin], [], [], 0.1)[0]:
                return None

            # Read response
            response = ""
            while True:
                if not select.select([sys.stdin], [], [], 0.05)[0]:
                    break
                ch = sys.stdin.read(1)
                if not ch:
                    break
                response += ch
                if ch in ("\\", "\x07"):  # ST or BEL terminator
                    break

            # Parse OSC 11 response: \033]11;rgb:RRRR/GGGG/BBBB\033\\
            match = re.search(r'rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)', response)
            if match:
                r, g, b = match.groups()
                # Normalize to 8-bit
                r_val = int(r, 16) >> (len(r) * 4 - 8) if len(r) > 2 else int(r, 16)
                g_val = int(g, 16) >> (len(g) * 4 - 8) if len(g) > 2 else int(g, 16)
                b_val = int(b, 16) >> (len(b) * 4 - 8) if len(b) > 2 else int(b, 16)
                return rgb_to_hex(
                    max(0, min(255, r_val)),
                    max(0, min(255, g_val)),
                    max(0, min(255, b_val)),
                )
            return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except Exception:
        return None


def detect_light_mode_osc() -> bool:
    """Detect light mode via OSC 11 background color query.

    Queries the terminal for its background color and calculates
    relative luminance to determine if it's a light background.

    Returns:
        True if light mode should be used, False otherwise.
    """
    bg_color = _query_osc11_background()
    if bg_color is None:
        return False

    r, g, b = parse_color(bg_color)
    lum = relative_luminance(r, g, b)
    # Luminance > 0.5 means light background
    return lum > 0.5


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


# ─── Per-Skin Banner Art ────────────────────────────────────────────────────


BANNER_LOGO: dict[str, list[str]] = {
    "default": [
        "    _    _    ",
        "   / \\  | |__ ",
        "  / _ \\ | '_ \\",
        " / ___ \\| | | |",
        "/_/   \\_\\_| |_|",
    ],
    "gold": [
        "    _    _    ",
        "   / \\  | |__ ",
        "  / _ \\ | '_ \\",
        " / ___ \\| | | |",
        "/_/   \\_\\_| |_|",
    ],
    "crimson": [
        "   ___ ____  ",
        "  / __|  _ \\ ",
        " | (__| |_) |",
        "  \\___|  _/ ",
        "       |_|   ",
    ],
    "ocean": [
        "   ___  ____ ",
        "  / _ \\| ___|",
        " | | | |___ \\",
        " | |_| |___) |",
        "  \\___/|____/ ",
    ],
    "forest": [
        "  ___ _____  ",
        " |  ___|  ___|",
        " | |_  | |_   ",
        " |  _| |  _|  ",
        " |_|   |_|    ",
    ],
    "sunset": [
        "  ___ _   _  ",
        " / __| | | | ",
        " \\__ \\ |_| | ",
        " |___/\\__,_| ",
        "             ",
    ],
    "midnight": [
        "  __  __ ___ ",
        " |  \\/  |_ _|",
        " | |\\/| || | ",
        " | |  | || | ",
        " |_|  |_|___|",
    ],
    "arctic": [
        "    _    _   ",
        "   / \\  | |_ ",
        "  / _ \\ | __|",
        " / ___ \\| |_ ",
        "/_/   \\_\\\\__|",
    ],
    "volcanic": [
        " __   _____  ",
        " \\ \\ / / _ \\ ",
        "  \\ V /| (_) |",
        "   |_|  \\___/ ",
        "             ",
    ],
    "system": [
        "  ___ ___  ",
        " / __/ __| ",
        " \\__ \\__ \\ ",
        " |___/___/ ",
        "           ",
    ],
}

BANNER_HERO: dict[str, list[str]] = {
    "default": [
        "╔══════════════════════════════════════╗",
        "║   AgentHarness — AI Agent CLI       ║",
        "║   Type /help for commands           ║",
        "╚══════════════════════════════════════╝",
    ],
    "gold": [
        "╔══════════════════════════════════════╗",
        "║   AgentHarness — AI Agent CLI       ║",
        "║   Type /help for commands           ║",
        "╚══════════════════════════════════════╝",
    ],
    "crimson": [
        "╔══════════════════════════════════════╗",
        "║   AgentHarness — Crimson Edition    ║",
        "║   Type /help for commands           ║",
        "╚══════════════════════════════════════╝",
    ],
    "ocean": [
        "╔══════════════════════════════════════╗",
        "║   AgentHarness — Ocean Edition      ║",
        "║   Type /help for commands           ║",
        "╚══════════════════════════════════════╝",
    ],
    "forest": [
        "╔══════════════════════════════════════╗",
        "║   AgentHarness — Forest Edition     ║",
        "║   Type /help for commands           ║",
        "╚══════════════════════════════════════╝",
    ],
    "sunset": [
        "╔══════════════════════════════════════╗",
        "║   AgentHarness — Sunset Edition     ║",
        "║   Type /help for commands           ║",
        "╚══════════════════════════════════════╝",
    ],
    "midnight": [
        "╔══════════════════════════════════════╗",
        "║   AgentHarness — Midnight Edition   ║",
        "║   Type /help for commands           ║",
        "╚══════════════════════════════════════╝",
    ],
    "arctic": [
        "╔══════════════════════════════════════╗",
        "║   AgentHarness — Arctic Edition     ║",
        "║   Type /help for commands           ║",
        "╚══════════════════════════════════════╝",
    ],
    "volcanic": [
        "╔══════════════════════════════════════╗",
        "║   AgentHarness — Volcanic Edition   ║",
        "║   Type /help for commands           ║",
        "╚══════════════════════════════════════╝",
    ],
    "system": [
        "╔══════════════════════════════════════╗",
        "║   AgentHarness — System Theme       ║",
        "║   Type /help for commands           ║",
        "╚══════════════════════════════════════╝",
    ],
}


# ─── prompt_toolkit Style Generation ────────────────────────────────────────


def generate_prompt_toolkit_style(
    scheme: ColorScheme,
    light: bool = False,
) -> dict[str, str]:
    """Generate a prompt_toolkit style dictionary from a ColorScheme.

    Creates a mapping of prompt_toolkit style names to color strings
    that can be used with prompt_toolkit's Style.from_dict().

    Args:
        scheme: The ColorScheme to derive styles from.
        light: Whether to use light-mode colors.

    Returns:
        Dictionary mapping style names to color strings.
    """
    return {
        "prompt": scheme.get("primary", light=light),
        "prompt-scheme": scheme.get("secondary", light=light),
        "prompt-session": scheme.get("secondary", light=light),
        "prompt-separator": scheme.get("text", light=light),
        "prompt-input": scheme.get("text", light=light),
        "prompt-suggestion": scheme.get("muted", light=light),
        "prompt-continuation": scheme.get("muted", light=light),
        "prompt-banner-border": scheme.get("primary", light=light),
        "prompt-banner-title": scheme.get("primary", light=light),
        "prompt-banner-text": scheme.get("text", light=light),
        "status-success": scheme.get("success", light=light),
        "status-warning": scheme.get("warning", light=light),
        "status-error": scheme.get("error", light=light),
        "status-info": scheme.get("info", light=light),
        "status-muted": scheme.get("muted", light=light),
        "completion-menu": scheme.get("text", light=light),
        "completion-menu.completion.current": scheme.get("highlight", light=light),
        "completion-menu.meta.completion.current": scheme.get("muted", light=light),
        "scrollbar.background": scheme.get("muted", light=light),
        "scrollbar.button": scheme.get("primary", light=light),
    }


# ─── Syntax Highlighting Rules ───────────────────────────────────────────────


def generate_syntax_highlight_rules(
    scheme: ColorScheme,
    light: bool = False,
) -> dict[str, str]:
    """Generate syntax highlighting rules from a ColorScheme.

    Creates a mapping of Pygments token types to style strings
    that can be used with Rich's Syntax or Pygments directly.

    Args:
        scheme: The ColorScheme to derive colors from.
        light: Whether to use light-mode colors.

    Returns:
        Dictionary mapping token type names to style strings.
    """
    return {
        "Token": scheme.get("text", light=light),
        "Token.Keyword": scheme.get("secondary", light=light),
        "Token.Keyword.Constant": scheme.get("accent", light=light),
        "Token.Keyword.Declaration": scheme.get("secondary", light=light),
        "Token.Keyword.Namespace": scheme.get("secondary", light=light),
        "Token.Keyword.Pseudo": scheme.get("muted", light=light),
        "Token.Keyword.Reserved": scheme.get("secondary", light=light),
        "Token.Keyword.Type": scheme.get("info", light=light),
        "Token.Name": scheme.get("text", light=light),
        "Token.Name.Attribute": scheme.get("info", light=light),
        "Token.Name.Builtin": scheme.get("info", light=light),
        "Token.Name.Builtin.Pseudo": scheme.get("muted", light=light),
        "Token.Name.Class": scheme.get("accent", light=light),
        "Token.Name.Constant": scheme.get("accent", light=light),
        "Token.Name.Decorator": scheme.get("warning", light=light),
        "Token.Name.Entity": scheme.get("info", light=light),
        "Token.Name.Exception": scheme.get("error", light=light),
        "Token.Name.Function": scheme.get("primary", light=light),
        "Token.Name.Function.Magic": scheme.get("primary", light=light),
        "Token.Name.Label": scheme.get("muted", light=light),
        "Token.Name.Namespace": scheme.get("text", light=light),
        "Token.Name.Other": scheme.get("text", light=light),
        "Token.Name.Tag": scheme.get("secondary", light=light),
        "Token.Name.Variable": scheme.get("text", light=light),
        "Token.Name.Variable.Class": scheme.get("text", light=light),
        "Token.Name.Variable.Global": scheme.get("text", light=light),
        "Token.Name.Variable.Instance": scheme.get("text", light=light),
        "Token.Name.Variable.Magic": scheme.get("accent", light=light),
        "Token.Literal": scheme.get("success", light=light),
        "Token.Literal.Date": scheme.get("success", light=light),
        "Token.String": scheme.get("success", light=light),
        "Token.String.Affix": scheme.get("success", light=light),
        "Token.String.Backtick": scheme.get("success", light=light),
        "Token.String.Char": scheme.get("success", light=light),
        "Token.String.Delimiter": scheme.get("success", light=light),
        "Token.String.Doc": scheme.get("muted", light=light),
        "Token.String.Double": scheme.get("success", light=light),
        "Token.String.Escape": scheme.get("warning", light=light),
        "Token.String.Heredoc": scheme.get("success", light=light),
        "Token.String.Interpol": scheme.get("warning", light=light),
        "Token.String.Other": scheme.get("success", light=light),
        "Token.String.Regex": scheme.get("warning", light=light),
        "Token.String.Single": scheme.get("success", light=light),
        "Token.String.Symbol": scheme.get("accent", light=light),
        "Token.Number": scheme.get("warning", light=light),
        "Token.Number.Bin": scheme.get("warning", light=light),
        "Token.Number.Float": scheme.get("warning", light=light),
        "Token.Number.Hex": scheme.get("warning", light=light),
        "Token.Number.Integer": scheme.get("warning", light=light),
        "Token.Number.Integer.Long": scheme.get("warning", light=light),
        "Token.Number.Oct": scheme.get("warning", light=light),
        "Token.Operator": scheme.get("secondary", light=light),
        "Token.Operator.Word": scheme.get("secondary", light=light),
        "Token.Punctuation": scheme.get("text", light=light),
        "Token.Comment": scheme.get("muted", light=light),
        "Token.Comment.Hashbang": scheme.get("muted", light=light),
        "Token.Comment.Multiline": scheme.get("muted", light=light),
        "Token.Comment.Preproc": scheme.get("warning", light=light),
        "Token.Comment.PreprocFile": scheme.get("warning", light=light),
        "Token.Comment.Single": scheme.get("muted", light=light),
        "Token.Comment.Special": scheme.get("muted", light=light),
        "Token.Generic": scheme.get("text", light=light),
        "Token.Generic.Deleted": scheme.get("error", light=light),
        "Token.Generic.Emph": scheme.get("text", light=light),
        "Token.Generic.Error": scheme.get("error", light=light),
        "Token.Generic.Heading": scheme.get("primary", light=light),
        "Token.Generic.Inserted": scheme.get("success", light=light),
        "Token.Generic.Output": scheme.get("info", light=light),
        "Token.Generic.Prompt": scheme.get("primary", light=light),
        "Token.Generic.Strong": scheme.get("text", light=light),
        "Token.Generic.Subheading": scheme.get("secondary", light=light),
        "Token.Generic.Traceback": scheme.get("error", light=light),
        "Token.Whitespace": scheme.get("text", light=light),
        "Token.Error": scheme.get("error", light=light),
    }


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
