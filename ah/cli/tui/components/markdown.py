"""Themed Markdown renderer.

Adapted from earendil-works/pi (MIT). See ../LICENSE.pi.

Renders a useful subset of Markdown to styled terminal lines using a theme's
``md*`` color roles: headings, bold/italic/strikethrough inline spans, inline
code, fenced code blocks with a bordered frame, blockquotes with a border,
unordered/ordered lists, and horizontal rules. Output is cached per width.
"""
from __future__ import annotations

import re

from ah.cli.tui.theme import Theme
from ah.cli.tui.utils import pad_to_width, wrap_text_with_ansi

__all__ = ["Markdown"]

_FENCE_RE = re.compile(r"^(```|~~~)(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_ULIST_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OLIST_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")

# Inline spans. Order matters: code first so ** inside `code` is left alone.
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_ITALIC_RE = re.compile(r"\*([^*]+)\*|_([^_]+)_")
_STRIKE_RE = re.compile(r"~~([^~]+)~~")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


class Markdown:
    """Render Markdown *text* with a :class:`Theme`."""

    def __init__(self, text: str, theme: Theme, padding_x: int = 0, padding_y: int = 0) -> None:
        self._text = text
        self._theme = theme
        self._px = padding_x
        self._py = padding_y
        self._cache_w: int | None = None
        self._cache: list[str] | None = None

    def set_text(self, text: str) -> None:
        self._text = text
        self.invalidate()

    def render(self, width: int) -> list[str]:
        if self._cache is not None and self._cache_w == width:
            return self._cache
        inner = max(1, width - 2 * self._px)
        lines = self._render_blocks(inner)
        pad = " " * self._px
        out = [pad + pad_to_width(l, inner) + pad for l in lines]
        if self._py:
            blank = " " * width
            out = [blank] * self._py + out + [blank] * self._py
        self._cache, self._cache_w = out, width
        return out

    # ─── block parsing ─────────────────────────────────────────────────────
    def _render_blocks(self, width: int) -> list[str]:
        t = self._theme
        out: list[str] = []
        lines = self._text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]

            # Fenced code block
            m = _FENCE_RE.match(line.strip())
            if m:
                i += 1
                code: list[str] = []
                while i < len(lines) and not _FENCE_RE.match(lines[i].strip()):
                    code.append(lines[i])
                    i += 1
                i += 1  # consume closing fence
                out.extend(self._render_code_block(code, width))
                continue

            # Horizontal rule
            if _HR_RE.match(line):
                out.append(t.style("─" * width, "mdHr"))
                i += 1
                continue

            # Heading
            m = _HEADING_RE.match(line)
            if m:
                text = self._render_inline(m.group(2))
                out.extend(wrap_text_with_ansi(t.style(text, "mdHeading", bold=True), width))
                i += 1
                continue

            # Blockquote (consecutive > lines)
            m = _QUOTE_RE.match(line)
            if m:
                quote_lines: list[str] = []
                while i < len(lines) and _QUOTE_RE.match(lines[i]):
                    quote_lines.append(_QUOTE_RE.match(lines[i]).group(1))
                    i += 1
                border = t.style("│", "mdQuoteBorder")
                for q in quote_lines:
                    body = t.style(self._render_inline(q), "mdQuote")
                    for wl in wrap_text_with_ansi(body, max(1, width - 2)):
                        out.append(f"{border} {wl}")
                continue

            # Lists
            m = _ULIST_RE.match(line)
            if m:
                bullet = t.style("•", "mdListBullet")
                body = self._render_inline(m.group(2))
                indent = m.group(1)
                wrapped = wrap_text_with_ansi(body, max(1, width - len(indent) - 2))
                out.append(f"{indent}{bullet} {wrapped[0] if wrapped else ''}")
                for cont in wrapped[1:]:
                    out.append(f"{indent}  {cont}")
                i += 1
                continue
            m = _OLIST_RE.match(line)
            if m:
                num = t.style(f"{m.group(2)}.", "mdListBullet")
                body = self._render_inline(m.group(3))
                indent = m.group(1)
                wrapped = wrap_text_with_ansi(body, max(1, width - len(indent) - 3))
                out.append(f"{indent}{num} {wrapped[0] if wrapped else ''}")
                for cont in wrapped[1:]:
                    out.append(f"{indent}   {cont}")
                i += 1
                continue

            # Blank line
            if line.strip() == "":
                out.append("")
                i += 1
                continue

            # Paragraph
            para = self._render_inline(line)
            out.extend(wrap_text_with_ansi(para, width))
            i += 1
        return out

    def _render_code_block(self, code: list[str], width: int) -> list[str]:
        t = self._theme
        top = t.style("╭" + "─" * (width - 2) + "╮", "mdCodeBlockBorder")
        bot = t.style("╰" + "─" * (width - 2) + "╯", "mdCodeBlockBorder")
        side = t.style("│", "mdCodeBlockBorder")
        out = [top]
        for c in code:
            body = t.style(pad_to_width(c, width - 4), "mdCodeBlock")
            out.append(f"{side} {body} {side}")
        out.append(bot)
        return out

    # ─── inline spans ────────────────────────────────────────────────────────
    def _render_inline(self, text: str) -> str:
        t = self._theme
        # Links -> styled text (url dropped to keep lines tight; shown dim after).
        text = _LINK_RE.sub(lambda m: t.style(m.group(1), "mdLink") + t.style(f" ({m.group(2)})", "mdLinkUrl"), text)
        text = _INLINE_CODE_RE.sub(lambda m: t.style(m.group(1), "mdCode"), text)
        text = _BOLD_RE.sub(lambda m: t.style(m.group(1) or m.group(2), "text", bold=True), text)
        text = _STRIKE_RE.sub(lambda m: t.style(m.group(1), "text", strikethrough=True), text)
        text = _ITALIC_RE.sub(lambda m: t.style(m.group(1) or m.group(2), "text", italic=True), text)
        return text

    def invalidate(self) -> None:
        self._cache = None
        self._cache_w = None
