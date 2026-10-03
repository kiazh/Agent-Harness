"""Keyboard input matching for the TUI.

Adapted from earendil-works/pi (MIT). See LICENSE.pi.

Parses raw terminal input into normalized key identifiers like ``"enter"``,
``"ctrl+c"``, ``"shift+tab"``, ``"up"`` and matches them with ``matches_key``.
Covers the common VT/xterm sequences used by the editor and menus (not the full
Kitty keyboard protocol).
"""
from __future__ import annotations

__all__ = ["Key", "parse_key", "matches_key"]


class Key:
    """Helpers producing canonical key-id strings."""

    enter = "enter"
    escape = "escape"
    tab = "tab"
    space = "space"
    backspace = "backspace"
    delete = "delete"
    home = "home"
    end = "end"
    up = "up"
    down = "down"
    left = "left"
    right = "right"
    page_up = "pageup"
    page_down = "pagedown"

    @staticmethod
    def ctrl(key: str) -> str:
        return f"ctrl+{key.lower()}"

    @staticmethod
    def shift(key: str) -> str:
        return f"shift+{key.lower()}"

    @staticmethod
    def alt(key: str) -> str:
        return f"alt+{key.lower()}"

    @staticmethod
    def ctrl_shift(key: str) -> str:
        return f"ctrl+shift+{key.lower()}"


# Raw escape sequences -> canonical ids.
_SEQUENCES: dict[str, str] = {
    "\r": "enter",
    "\n": "enter",
    "\x1b": "escape",
    "\t": "tab",
    "\x7f": "backspace",
    "\x08": "backspace",
    " ": "space",
    # CSI arrows / nav
    "\x1b[A": "up",
    "\x1b[B": "down",
    "\x1b[C": "right",
    "\x1b[D": "left",
    "\x1bOA": "up",
    "\x1bOB": "down",
    "\x1bOC": "right",
    "\x1bOD": "left",
    "\x1b[H": "home",
    "\x1b[F": "end",
    "\x1b[1~": "home",
    "\x1b[4~": "end",
    "\x1b[3~": "delete",
    "\x1b[5~": "pageup",
    "\x1b[6~": "pagedown",
    "\x1b[Z": "shift+tab",
    # Ctrl+arrows (xterm modifier 5) and Alt+arrows (modifier 3)
    "\x1b[1;5A": "ctrl+up",
    "\x1b[1;5B": "ctrl+down",
    "\x1b[1;5C": "ctrl+right",
    "\x1b[1;5D": "ctrl+left",
    "\x1b[1;3A": "alt+up",
    "\x1b[1;3B": "alt+down",
    "\x1b[1;3C": "alt+right",
    "\x1b[1;3D": "alt+left",
}


def parse_key(data: str) -> str:
    """Normalize raw terminal input *data* into a canonical key id.

    Returns the id (e.g. ``"ctrl+c"``, ``"enter"``, ``"up"``) or, for a plain
    printable character, the character itself.
    """
    if data in _SEQUENCES:
        return _SEQUENCES[data]

    # Alt+<char>: ESC followed by a single printable char.
    if len(data) == 2 and data[0] == "\x1b" and data[1].isprintable():
        return f"alt+{data[1].lower()}"

    # Ctrl+<letter>: bytes 0x01-0x1a map to ctrl+a .. ctrl+z.
    if len(data) == 1:
        o = ord(data)
        if 1 <= o <= 26 and data not in ("\r", "\n", "\t", "\x08"):
            return f"ctrl+{chr(o + 96)}"
        if data.isprintable():
            return data

    return data


def matches_key(data: str, key_id: str) -> bool:
    """True if raw input *data* matches canonical *key_id* (case-insensitive)."""
    return parse_key(data) == key_id.lower()
