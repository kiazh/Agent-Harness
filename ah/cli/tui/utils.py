"""ANSI-aware string utilities for the TUI.

Adapted from earendil-works/pi (MIT). See LICENSE.pi.
Reimplemented in Python; handles SGR/CSI/OSC sequences and East-Asian wide chars.
"""
from __future__ import annotations

import re
import unicodedata

__all__ = [
    "strip_ansi",
    "visible_width",
    "truncate_to_width",
    "wrap_text_with_ansi",
    "pad_to_width",
]

# Matches ANSI escape sequences: CSI (incl. SGR), OSC (terminated by BEL or ST),
# and the zero-width APC cursor marker used by Focusable components.
_ANSI_RE = re.compile(
    r"""
    \x1b\[[0-9;?]*[ -/]*[@-~]      # CSI ... final byte
    | \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)   # OSC ... BEL or ST
    | \x1b_[^\x1b]*\x1b\\          # APC ... ST  (CURSOR_MARKER lives here)
    | \x1b[@-Z\\-_]                # 2-byte escapes
    """,
    re.VERBOSE,
)


def strip_ansi(text: str) -> str:
    """Return *text* with all ANSI escape sequences removed."""
    return _ANSI_RE.sub("", text)


def _char_width(ch: str) -> int:
    """Visible column width of a single character (0, 1, or 2)."""
    if not ch:
        return 0
    # Zero-width: combining marks, zero-width space/joiner.
    if unicodedata.combining(ch):
        return 0
    if ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        return 0
    # Wide / fullwidth East-Asian characters occupy two columns.
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def visible_width(text: str) -> int:
    """Visible column width of *text*, ignoring ANSI codes and counting wide chars as 2."""
    stripped = strip_ansi(text)
    return sum(_char_width(ch) for ch in stripped)


def _split_ansi(text: str) -> list[tuple[str, bool]]:
    """Split *text* into (chunk, is_ansi) segments preserving order."""
    segments: list[tuple[str, bool]] = []
    last = 0
    for m in _ANSI_RE.finditer(text):
        if m.start() > last:
            segments.append((text[last:m.start()], False))
        segments.append((m.group(0), True))
        last = m.end()
    if last < len(text):
        segments.append((text[last:], False))
    return segments


def truncate_to_width(text: str, width: int, ellipsis: str = "...") -> str:
    """Truncate *text* to a visible *width*, preserving ANSI codes.

    Appends *ellipsis* when truncation occurs (pass "" to disable). Always
    closes styles with an SGR reset so truncation never bleeds into later text.
    """
    if width <= 0:
        return ""
    if visible_width(text) <= width:
        return text

    ell_w = visible_width(ellipsis)
    budget = max(0, width - ell_w)

    out: list[str] = []
    used = 0
    had_style = False
    for chunk, is_ansi in _split_ansi(text):
        if is_ansi:
            out.append(chunk)
            if chunk.startswith("\x1b["):
                had_style = True
            continue
        for ch in chunk:
            w = _char_width(ch)
            if used + w > budget:
                out.append(ellipsis)
                if had_style:
                    out.append("\x1b[0m")
                return "".join(out)
            out.append(ch)
            used += w
    # Exactly fit (shouldn't happen given the early return, but be safe).
    out.append(ellipsis)
    if had_style:
        out.append("\x1b[0m")
    return "".join(out)


def pad_to_width(text: str, width: int, fill: str = " ") -> str:
    """Right-pad *text* with *fill* to an exact visible *width* (no truncation)."""
    w = visible_width(text)
    if w >= width:
        return text
    return text + fill * (width - w)


def wrap_text_with_ansi(text: str, width: int) -> list[str]:
    """Word-wrap *text* to *width* columns, preserving ANSI styling per line.

    Active SGR style is reopened at the start of each wrapped line and closed
    with a reset at the end, so styles never carry across line breaks. Explicit
    newlines in *text* are honored as hard breaks.
    """
    if width <= 0:
        return [text]

    lines: list[str] = []
    for raw_line in text.split("\n"):
        lines.extend(_wrap_single_line(raw_line, width))
    return lines


def _wrap_single_line(line: str, width: int) -> list[str]:
    if visible_width(line) <= width:
        return [line]

    # Tokenize into words while tracking the ANSI style prefix active at each word.
    result: list[str] = []
    cur: list[str] = []
    cur_w = 0
    active_style = ""  # accumulated SGR codes currently in effect

    def flush() -> None:
        nonlocal cur, cur_w
        if cur:
            piece = "".join(cur)
            if active_style and not piece.endswith("\x1b[0m"):
                piece = piece + "\x1b[0m"
            result.append(piece)
        cur = [active_style] if active_style else []
        cur_w = 0

    segments = _split_ansi(line)
    # Build a flat list of (word, leading_ansi) by walking segments.
    pending_ansi = ""
    for chunk, is_ansi in segments:
        if is_ansi:
            pending_ansi += chunk
            if chunk.startswith("\x1b[") and chunk.endswith("m"):
                if chunk in ("\x1b[0m", "\x1b[m"):
                    active_style = ""
                else:
                    active_style += chunk
            cur.append(chunk)
            continue
        words = re.split(r"(\s+)", chunk)
        for word in words:
            if word == "":
                continue
            w = visible_width(word)
            if cur_w + w > width and cur_w > 0:
                flush()
                if word.isspace():
                    continue
            cur.append(word)
            cur_w += w
        pending_ansi = ""
    flush()
    return [ln for ln in result if ln != ""] or [""]
