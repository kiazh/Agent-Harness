"""
Color space utilities for AgentHarness visual system.

OKLCH/OKHSL color space support, gamut mapping, color blending,
luminance/contrast calculations, and color parsing.
"""
from __future__ import annotations

import math
from typing import Any

from rich.style import Style


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
