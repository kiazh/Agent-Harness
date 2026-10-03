"""Color model for the TUI: indexed / sRGB / OKLCH, OKHSL parsing, gamut
mapping, mixing, and ANSI styling.

Adapted from earendil-works/pi (MIT). See LICENSE.pi.
The color *model* (indexed|rgb|oklch union, OKHSL theme tokens, OKLCH mixing,
chroma-bisection gamut mapping, 256-color quantization, SGR nesting) mirrors
pi's `colors.ts`/`oklab.ts`; the implementation is original Python.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal, Union

__all__ = [
    "Color",
    "IndexedColor",
    "RgbColor",
    "OklchColor",
    "parse_color",
    "color_to_rgb",
    "color_to_hex",
    "mix_colors",
    "foreground_ansi",
    "background_ansi",
    "style_text",
    "TextStyle",
    "TerminalColorMode",
]

TerminalColorMode = Literal["256color", "truecolor"]


@dataclass(frozen=True)
class IndexedColor:
    index: int  # 0-255


@dataclass(frozen=True)
class RgbColor:
    r: float  # 0-255
    g: float
    b: float


@dataclass(frozen=True)
class OklchColor:
    l: float  # 0-1
    c: float  # >= 0
    h: float  # degrees


Color = Union[IndexedColor, RgbColor, OklchColor]


# ─── sRGB <-> linear ────────────────────────────────────────────────────────

def _srgb_to_linear(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    v = c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
    return max(0.0, min(255.0, v * 255.0))


# ─── OKLab <-> linear sRGB ──────────────────────────────────────────────────

def _linear_srgb_to_oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = l ** (1 / 3), m ** (1 / 3), s ** (1 / 3)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _oklab_to_linear_srgb(L: float, a: float, b: float) -> tuple[float, float, float]:
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    return (
        +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def _in_gamut(lin: tuple[float, float, float], eps: float = 1e-7) -> bool:
    return all(-eps <= c <= 1 + eps for c in lin)


def _oklch_to_rgb(l: float, c: float, h: float) -> RgbColor:
    rad = math.radians(h)
    cos, sin = math.cos(rad), math.sin(rad)

    def at(chroma: float) -> tuple[float, float, float]:
        return _oklab_to_linear_srgb(l, chroma * cos, chroma * sin)

    direct = at(c)
    if _in_gamut(direct):
        lin = direct
    else:
        # Reduce chroma by bisection until in gamut (achromatic always fits).
        lin = at(0.0)
        lo, hi = 0.0, c
        for _ in range(20):
            mid = (lo + hi) / 2
            cand = at(mid)
            if _in_gamut(cand):
                lo, lin = mid, cand
            else:
                hi = mid
    return RgbColor(_linear_to_srgb(lin[0]), _linear_to_srgb(lin[1]), _linear_to_srgb(lin[2]))


# ─── OKHSL -> sRGB (Björn Ottosson's model, as pi uses for theme tokens) ─────

def _toe(x: float) -> float:
    k1, k2 = 0.206, 0.03
    k3 = (1 + k1) / (1 + k2)
    return 0.5 * (k3 * x - k1 + math.sqrt((k3 * x - k1) ** 2 + 4 * k2 * k3 * x))


def _toe_inv(x: float) -> float:
    k1, k2 = 0.206, 0.03
    k3 = (1 + k1) / (1 + k2)
    return (x * x + k1 * x) / (k3 * (x + k2))


def _compute_max_saturation(a: float, b: float) -> float:
    if -1.88170328 * a - 0.80936493 * b > 1:
        k0, k1, k2, k3, k4 = 1.19086277, 1.76576728, 0.59662641, 0.75515197, 0.56771245
        wl, wm, ws = 4.0767416621, -3.3077115913, 0.2309699292
    elif 1.81444104 * a - 1.19445276 * b > 1:
        k0, k1, k2, k3, k4 = 0.73956515, -0.45954404, 0.08285427, 0.12541070, 0.14503204
        wl, wm, ws = -1.2684380046, 2.6097574011, -0.3413193965
    else:
        k0, k1, k2, k3, k4 = 1.35733652, -0.00915799, -1.15130210, -0.50559606, 0.00692167
        wl, wm, ws = -0.0041960863, -0.7034186147, 1.7076147010
    S = k0 + k1 * a + k2 * b + k3 * a * a + k4 * a * b
    k_l = 0.3963377774 * a + 0.2158037573 * b
    k_m = -0.1055613458 * a - 0.0638541728 * b
    k_s = -0.0894841775 * a - 1.2914855480 * b
    for _ in range(2):
        l_ = 1 + S * k_l
        m_ = 1 + S * k_m
        s_ = 1 + S * k_s
        l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
        l_ds = 3 * k_l * l_ * l_
        m_ds = 3 * k_m * m_ * m_
        s_ds = 3 * k_s * s_ * s_
        l_ds2 = 6 * k_l * k_l * l_
        m_ds2 = 6 * k_m * k_m * m_
        s_ds2 = 6 * k_s * k_s * s_
        f = wl * l + wm * m + ws * s
        f1 = wl * l_ds + wm * m_ds + ws * s_ds
        f2 = wl * l_ds2 + wm * m_ds2 + ws * s_ds2
        denom = f1 * f1 - 0.5 * f * f2
        if denom != 0:
            S = S - f * f1 / denom
    return S


def _find_cusp(a: float, b: float) -> tuple[float, float]:
    s_cusp = _compute_max_saturation(a, b)
    r, g, bb = _oklab_to_linear_srgb(1, s_cusp * a, s_cusp * b)
    l_cusp = (1 / max(max(r, g), bb)) ** (1 / 3)
    return l_cusp, l_cusp * s_cusp


def _get_st_mid(a: float, b: float) -> tuple[float, float]:
    s = 0.11516993 + 1 / (
        7.44778970 + 4.15901240 * b
        + a * (-2.19557347 + 1.75198401 * b
               + a * (-2.13704948 - 10.02301043 * b
                      + a * (-4.24894561 + 5.38770819 * b + 4.69891013 * a)))
    )
    t = 0.11239642 + 1 / (
        1.61320320 - 0.68124379 * b
        + a * (0.40370612 + 0.90148123 * b
               + a * (-0.27087943 + 0.61223990 * b
                      + a * (0.00299215 - 0.45399568 * b - 0.14661872 * a)))
    )
    return s, t


def _get_cs(big_l: float, a_: float, b_: float) -> tuple[float, float, float]:
    """Return (C_0, C_mid, C_max) for lightness *big_l* and hue direction (a_, b_).

    Reference OKHSL algorithm (Björn Ottosson, public domain reference impl).
    """
    cusp_l, cusp_c = _find_cusp(a_, b_)

    # C_max: distance to gamut boundary from the achromatic axis at this L.
    def find_gamut_intersection(l1: float, c1: float, l0: float) -> float:
        if ((l1 - l0) * cusp_c - (cusp_l - l0) * c1) <= 0:
            return cusp_c * l0 / (c1 * cusp_l + cusp_c * (l0 - l1)) if (c1 * cusp_l + cusp_c * (l0 - l1)) else 0.0
        return cusp_c * (l0 - 1) / (c1 * (cusp_l - 1) + cusp_c * (l0 - l1)) if (c1 * (cusp_l - 1) + cusp_c * (l0 - l1)) else 0.0

    c_max = find_gamut_intersection(big_l, 1.0, big_l)

    # C_mid: mid-saturation soft limit (reference uses a 1/4-power soft-min).
    s_mid, t_mid = _get_st_mid(a_, b_)
    c_a = big_l * s_mid
    c_b = (1 - big_l) * t_mid
    c_mid = 0.9 * (1.0 / math.sqrt(math.sqrt(1.0 / c_a**4 + 1.0 / c_b**4))) if c_a and c_b else 0.0

    # C_0: low-saturation soft limit (1/2-power soft-min).
    c_a = big_l * 0.4
    c_b = (1 - big_l) * 0.8
    c_0 = (1.0 / math.sqrt(1.0 / c_a**2 + 1.0 / c_b**2)) if c_a and c_b else 0.0

    return c_0, c_mid, c_max


def _okhsl_to_rgb(h: float, s: float, l: float) -> RgbColor:
    if l <= 0:
        return RgbColor(0, 0, 0)
    if l >= 1:
        return RgbColor(255, 255, 255)
    h_rad = math.radians(h)
    a_ = math.cos(h_rad)
    b_ = math.sin(h_rad)
    big_l = _toe_inv(l)

    c_0, c_mid, c_max = _get_cs(big_l, a_, b_)

    # Piecewise interpolation of chroma from saturation (reference algorithm).
    mid = 0.8
    mid_inv = 1.25
    if s < mid:
        t = mid_inv * s
        k_1 = mid * c_0
        k_2 = 1 - k_1 / c_mid if c_mid else 1.0
        c = t * k_1 / (1 - k_2 * t) if (1 - k_2 * t) else 0.0
    else:
        t = (s - mid) / (1 - mid)
        k_0 = c_mid
        k_1 = (1 - mid) * c_mid * c_mid * mid_inv * mid_inv / c_0 if c_0 else 0.0
        k_2 = 1 - k_1 / (c_max - c_mid) if (c_max - c_mid) else 1.0
        c = k_0 + t * k_1 / (1 - k_2 * t) if (1 - k_2 * t) else k_0

    r, g, bb = _oklab_to_linear_srgb(big_l, c * a_, c * b_)
    return RgbColor(_linear_to_srgb(r), _linear_to_srgb(g), _linear_to_srgb(bb))


# ─── Parsing ────────────────────────────────────────────────────────────────

_NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?"
_OKLCH_RE = re.compile(rf"^oklch\(\s*({_NUM})(%)?\s+({_NUM})\s+({_NUM})(?:deg)?\s*\)$", re.I)
_OKHSL_RE = re.compile(rf"^okhsl\(\s*({_NUM})(?:deg)?\s+({_NUM})(%)?\s+({_NUM})(%)?\s*\)$", re.I)
_HEX_RE = re.compile(r"^#([0-9a-f]{3}|[0-9a-f]{6})$", re.I)


def parse_color(value: "str | int") -> Color:
    """Parse a hex / oklch() / okhsl() / 256-index color into a Color."""
    if isinstance(value, int):
        return IndexedColor(value)
    v = value.strip()
    m = _HEX_RE.match(v)
    if m:
        d = m.group(1)
        if len(d) == 3:
            d = "".join(ch * 2 for ch in d)
        return RgbColor(int(d[0:2], 16), int(d[2:4], 16), int(d[4:6], 16))
    m = _OKLCH_RE.match(v)
    if m:
        l = float(m.group(1)) / (100 if m.group(2) else 1)
        return OklchColor(l, float(m.group(3)), float(m.group(4)))
    m = _OKHSL_RE.match(v)
    if m:
        h = float(m.group(1))
        s = float(m.group(2)) / (100 if m.group(3) else 1)
        l = float(m.group(4)) / (100 if m.group(5) else 1)
        return _okhsl_to_rgb(h, s, l)
    raise ValueError(f"Invalid color value: {value!r}")


# ─── Conversion / mixing ────────────────────────────────────────────────────

_BASIC16 = [
    (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0), (0, 0, 128),
    (128, 0, 128), (0, 128, 128), (192, 192, 192), (128, 128, 128),
    (255, 0, 0), (0, 255, 0), (255, 255, 0), (0, 0, 255), (255, 0, 255),
    (0, 255, 255), (255, 255, 255),
]
_CUBE = (0, 95, 135, 175, 215, 255)


def _indexed_to_rgb(i: int) -> RgbColor:
    if i < 16:
        return RgbColor(*_BASIC16[i])
    if i < 232:
        ci = i - 16
        return RgbColor(_CUBE[ci // 36], _CUBE[(ci % 36) // 6], _CUBE[ci % 6])
    g = 8 + (i - 232) * 10
    return RgbColor(g, g, g)


def color_to_rgb(color: Color) -> RgbColor:
    if isinstance(color, RgbColor):
        return color
    if isinstance(color, IndexedColor):
        return _indexed_to_rgb(color.index)
    return _oklch_to_rgb(color.l, color.c, color.h)


def _rgb_to_oklch(c: RgbColor) -> OklchColor:
    L, a, b = _linear_srgb_to_oklab(_srgb_to_linear(c.r), _srgb_to_linear(c.g), _srgb_to_linear(c.b))
    return OklchColor(L, math.hypot(a, b), (math.degrees(math.atan2(b, a)) + 360) % 360)


def color_to_hex(color: Color) -> str:
    c = color_to_rgb(color)
    return "#%02x%02x%02x" % (round(c.r), round(c.g), round(c.b))


def mix_colors(first: Color, second: Color, amount: float, space: str = "oklch") -> Color:
    amount = max(0.0, min(1.0, amount))
    if space == "srgb":
        a, b = color_to_rgb(first), color_to_rgb(second)
        return RgbColor(a.r + (b.r - a.r) * amount, a.g + (b.g - a.g) * amount, a.b + (b.b - a.b) * amount)
    a, b = _rgb_to_oklch(color_to_rgb(first)), _rgb_to_oklch(color_to_rgb(second))
    first_hue = b.h if a.c < 1e-7 else a.h
    second_hue = first_hue if b.c < 1e-7 else b.h
    dh = ((second_hue - first_hue + 540) % 360) - 180
    return OklchColor(a.l + (b.l - a.l) * amount, a.c + (b.c - a.c) * amount, first_hue + dh * amount)


# ─── ANSI output ────────────────────────────────────────────────────────────

def _rgb_to_ansi256(c: RgbColor) -> int:
    def closest(vals, t):
        return min(range(len(vals)), key=lambda i: abs(vals[i] - t))
    ri, gi, bi = closest(_CUBE, c.r), closest(_CUBE, c.g), closest(_CUBE, c.b)
    cube_i = 16 + 36 * ri + 6 * gi + bi
    gray = round(0.299 * c.r + 0.587 * c.g + 0.114 * c.b)
    gidx = min(23, max(0, round((gray - 8) / 10)))
    gval = 8 + gidx * 10
    spread = max(c.r, c.g, c.b) - min(c.r, c.g, c.b)
    cube = RgbColor(_CUBE[ri], _CUBE[gi], _CUBE[bi])

    def dist(x, y):
        return (x.r - y.r) ** 2 * 0.299 + (x.g - y.g) ** 2 * 0.587 + (x.b - y.b) ** 2 * 0.114
    if spread < 10 and dist(c, RgbColor(gval, gval, gval)) < dist(c, cube):
        return 232 + gidx
    return cube_i


def _color_ansi(color: Color, mode: TerminalColorMode, background: bool) -> str:
    lead = 48 if background else 38
    if isinstance(color, IndexedColor):
        return f"\x1b[{lead};5;{color.index}m"
    rgb = color_to_rgb(color)
    if mode == "truecolor":
        return f"\x1b[{lead};2;{round(rgb.r)};{round(rgb.g)};{round(rgb.b)}m"
    return f"\x1b[{lead};5;{_rgb_to_ansi256(rgb)}m"


def foreground_ansi(color: Color, mode: TerminalColorMode = "truecolor") -> str:
    return _color_ansi(color, mode, False)


def background_ansi(color: Color, mode: TerminalColorMode = "truecolor") -> str:
    return _color_ansi(color, mode, True)


@dataclass
class TextStyle:
    fg: "Color | None" = None
    bg: "Color | None" = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    inverse: bool = False
    strikethrough: bool = False


def style_text(text: str, style: TextStyle, mode: TerminalColorMode = "truecolor") -> str:
    """Wrap *text* in SGR codes per *style*, closing in reverse order."""
    prefix = ""
    suffix = ""
    if style.fg is not None:
        prefix += foreground_ansi(style.fg, mode)
        suffix = "\x1b[39m"
    if style.bg is not None:
        prefix += background_ansi(style.bg, mode)
        suffix = "\x1b[49m" + suffix
    if style.bold:
        prefix += "\x1b[1m"
    if style.dim:
        prefix += "\x1b[2m"
    if style.bold or style.dim:
        suffix = "\x1b[22m" + suffix
    if style.italic:
        prefix += "\x1b[3m"
        suffix = "\x1b[23m" + suffix
    if style.underline:
        prefix += "\x1b[4m"
        suffix = "\x1b[24m" + suffix
    if style.inverse:
        prefix += "\x1b[7m"
        suffix = "\x1b[27m" + suffix
    if style.strikethrough:
        prefix += "\x1b[9m"
        suffix = "\x1b[29m" + suffix
    return f"{prefix}{text}{suffix}"
