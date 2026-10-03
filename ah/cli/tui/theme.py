"""Theme system for the TUI.

Adapted from earendil-works/pi (MIT). See LICENSE.pi.

Carries pi's ``dark`` and ``light`` theme token values (OKHSL) verbatim and
resolves them through the ``vars`` + ``colors`` model, including the schema's
optional-role fallbacks. Colors resolve to concrete :class:`~ah.cli.tui.colors.Color`
values via the OKHSL/OKLCH engine in ``colors.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ah.cli.tui.colors import Color, TextStyle, parse_color, style_text, TerminalColorMode

__all__ = ["Theme", "DARK_THEME", "LIGHT_THEME", "get_theme", "list_themes"]


# ─── pi theme token data (verbatim from earendil-works/pi, MIT) ──────────────

DARK_THEME: dict[str, Any] = {
    "name": "dark",
    "appearance": "dark",
    "vars": {
        "text": "okhsl(234 3% 89%)",
        "muted": "okhsl(229 6% 67%)",
        "violet": "okhsl(295 50% 67%)",
        "blue": "okhsl(232 54% 67%)",
        "green": "okhsl(159 59% 67%)",
        "red": "okhsl(20 72% 67%)",
        "yellow": "okhsl(83 88% 67%)",
        "blueBg": "okhsl(233 41% 24%)",
    },
    "colors": {
        "accent": "violet",
        "border": "okhsl(231 57% 65%)",
        "borderAccent": "okhsl(295 53% 64%)",
        "borderMuted": "okhsl(229 8% 53%)",
        "success": "green",
        "error": "red",
        "warning": "yellow",
        "muted": "muted",
        "dim": "okhsl(229 8% 56%)",
        "text": "text",
        "thinkingText": "okhsl(226 7% 65%)",
        "selectedBg": "blueBg",
        "scrollbarTrack": "okhsl(237 7% 33%)",
        "scrollbarThumb": "okhsl(232 7% 65%)",
        "searchMatchBg": "okhsl(53 51% 24%)",
        "searchMatchText": "muted",
        "userMessageBg": "blueBg",
        "userMessageText": "text",
        "customMessageBg": "okhsl(295 42% 24%)",
        "customMessageText": "muted",
        "customMessageLabel": "violet",
        "toolPendingBg": "okhsl(229 5% 24%)",
        "toolSuccessBg": "okhsl(158 46% 25%)",
        "toolErrorBg": "okhsl(19 54% 25%)",
        "toolTitle": "text",
        "toolOutput": "muted",
        "mdHeading": "yellow",
        "mdLink": "blue",
        "mdLinkUrl": "muted",
        "mdCode": "violet",
        "mdCodeBlock": "green",
        "mdCodeBlockBorder": "muted",
        "mdQuote": "muted",
        "mdQuoteBorder": "muted",
        "mdHr": "muted",
        "mdListBullet": "violet",
        "toolDiffAdded": "green",
        "toolDiffRemoved": "red",
        "toolDiffContext": "muted",
        "syntaxComment": "muted",
        "syntaxKeyword": "blue",
        "syntaxFunction": "yellow",
        "syntaxVariable": "okhsl(202 58% 67%)",
        "syntaxString": "okhsl(52 67% 67%)",
        "syntaxNumber": "green",
        "syntaxType": "violet",
        "syntaxOperator": "muted",
        "syntaxPunctuation": "muted",
        "thinkingOff": "okhsl(229 8% 49%)",
        "thinkingMinimal": "okhsl(232 20% 52%)",
        "thinkingLow": "okhsl(232 45% 54%)",
        "thinkingMedium": "okhsl(263 59% 56%)",
        "thinkingHigh": "okhsl(295 73% 59%)",
        "thinkingXhigh": "okhsl(337 81% 61%)",
        "thinkingMax": "okhsl(20 99% 63%)",
        "bashMode": "okhsl(159 64% 65%)",
    },
}

LIGHT_THEME: dict[str, Any] = {
    "name": "light",
    "appearance": "light",
    "vars": {
        "text": "okhsl(225 5% 27%)",
        "muted": "okhsl(229 8% 47%)",
        "violet": "okhsl(295 60% 46%)",
        "blue": "okhsl(231 68% 47%)",
        "green": "okhsl(159 75% 46%)",
        "red": "okhsl(20 91% 47%)",
        "yellow": "okhsl(83 99% 47%)",
        "blueBg": "okhsl(235 19% 91%)",
    },
    "colors": {
        "accent": "violet",
        "border": "okhsl(231 67% 55%)",
        "borderAccent": "okhsl(295 59% 55%)",
        "borderMuted": "okhsl(235 7% 66%)",
        "success": "green",
        "error": "red",
        "warning": "yellow",
        "muted": "muted",
        "dim": "okhsl(229 7% 59%)",
        "text": "text",
        "thinkingText": "okhsl(234 8% 55%)",
        "selectedBg": "blueBg",
        "scrollbarTrack": "okhsl(248 3% 90%)",
        "scrollbarThumb": "okhsl(226 7% 65%)",
        "searchMatchBg": "okhsl(56 22% 91%)",
        "searchMatchText": "muted",
        "userMessageBg": "blueBg",
        "userMessageText": "text",
        "customMessageBg": "okhsl(295 25% 91%)",
        "customMessageText": "muted",
        "customMessageLabel": "violet",
        "toolPendingBg": "okhsl(248 3% 91%)",
        "toolSuccessBg": "okhsl(156 21% 91%)",
        "toolErrorBg": "okhsl(24 23% 91%)",
        "toolTitle": "text",
        "toolOutput": "muted",
        "mdHeading": "yellow",
        "mdLink": "blue",
        "mdLinkUrl": "muted",
        "mdCode": "violet",
        "mdCodeBlock": "green",
        "mdCodeBlockBorder": "muted",
        "mdQuote": "muted",
        "mdQuoteBorder": "muted",
        "mdHr": "muted",
        "mdListBullet": "violet",
        "toolDiffAdded": "green",
        "toolDiffRemoved": "red",
        "toolDiffContext": "muted",
        "syntaxComment": "muted",
        "syntaxKeyword": "blue",
        "syntaxFunction": "yellow",
        "syntaxVariable": "okhsl(203 73% 46%)",
        "syntaxString": "okhsl(52 84% 46%)",
        "syntaxNumber": "green",
        "syntaxType": "violet",
        "syntaxOperator": "muted",
        "syntaxPunctuation": "muted",
        "thinkingOff": "okhsl(223 5% 80%)",
        "thinkingMinimal": "okhsl(229 14% 78%)",
        "thinkingLow": "okhsl(232 33% 76%)",
        "thinkingMedium": "okhsl(264 48% 74%)",
        "thinkingHigh": "okhsl(295 62% 72%)",
        "thinkingXhigh": "okhsl(337 74% 70%)",
        "thinkingMax": "okhsl(20 98% 68%)",
        "bashMode": "okhsl(159 74% 55%)",
    },
}

# Optional roles inherit another role when omitted (from theme-schema.json).
_FALLBACKS = {
    "scrollbarTrack": "muted",
    "scrollbarThumb": "text",
    "searchMatchBg": "selectedBg",
    "searchMatchText": "text",
    "thinkingMax": "thinkingXhigh",
}


@dataclass
class Theme:
    """A resolved color theme. Role names map to concrete Colors."""

    name: str
    appearance: str
    _vars: dict[str, str] = field(default_factory=dict)
    _colors: dict[str, str] = field(default_factory=dict)
    mode: TerminalColorMode = "truecolor"
    _cache: dict[str, Color] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any], mode: TerminalColorMode = "truecolor") -> "Theme":
        return cls(
            name=data["name"],
            appearance=data.get("appearance", "dark"),
            _vars=dict(data.get("vars", {})),
            _colors=dict(data.get("colors", {})),
            mode=mode,
        )

    def _resolve_value(self, value: Any, _depth: int = 0) -> Color:
        """Resolve a raw token (var ref, okhsl/oklch/hex, or int) to a Color."""
        if _depth > 16:
            raise ValueError("circular theme variable reference")
        if isinstance(value, int):
            return parse_color(value)
        v = str(value).strip()
        if v in self._vars:
            return self._resolve_value(self._vars[v], _depth + 1)
        return parse_color(v)

    def color(self, role: str) -> Color:
        """Return the resolved Color for a theme *role* (with fallbacks)."""
        if role in self._cache:
            return self._cache[role]
        raw = self._colors.get(role)
        if raw is None and role in _FALLBACKS:
            return self.color(_FALLBACKS[role])
        if raw is None:
            raw = self._colors.get("text", "okhsl(0 0% 90%)")
        c = self._resolve_value(raw)
        self._cache[role] = c
        return c

    def style(self, text: str, role: str, **attrs: Any) -> str:
        """Style *text* with the foreground color of *role* plus SGR *attrs*."""
        return style_text(text, TextStyle(fg=self.color(role), **attrs), self.mode)

    def style_bg(self, text: str, role: str, **attrs: Any) -> str:
        """Style *text* with the background color of *role*."""
        return style_text(text, TextStyle(bg=self.color(role), **attrs), self.mode)


_THEMES = {"dark": DARK_THEME, "light": LIGHT_THEME}


def list_themes() -> list[str]:
    return list(_THEMES.keys())


def get_theme(name: str = "dark", mode: TerminalColorMode = "truecolor") -> Theme:
    data = _THEMES.get(name, DARK_THEME)
    return Theme.from_dict(data, mode=mode)
