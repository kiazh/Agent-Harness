"""Terminal I/O for the TUI.

Adapted from earendil-works/pi (MIT). See LICENSE.pi.

Provides a ``Terminal`` protocol and a ``ProcessTerminal`` backed by
stdin/stdout. Supports synchronized output (CSI 2026), the alternate screen
buffer, bracketed paste, cursor show/hide, and size queries. Raw-mode input
uses ``msvcrt`` on Windows and ``termios``/``tty`` on POSIX.
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Protocol, runtime_checkable

__all__ = ["Terminal", "ProcessTerminal", "SYNC_BEGIN", "SYNC_END"]

# Synchronized output: terminal buffers all writes between these and repaints
# atomically, eliminating flicker.
SYNC_BEGIN = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"

_ALT_SCREEN_ON = "\x1b[?1049h"
_ALT_SCREEN_OFF = "\x1b[?1049l"
_BRACKETED_PASTE_ON = "\x1b[?2004h"
_BRACKETED_PASTE_OFF = "\x1b[?2004l"
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"


def _enable_windows_vt_mode() -> None:
    """Turn on ANSI escape processing for the Windows console.

    Windows Terminal handles escapes already, but the classic console host
    prints them literally unless ENABLE_VIRTUAL_TERMINAL_PROCESSING is set.
    No-op on other platforms or when stdout is not a console.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # VT processing
    except Exception:
        pass


@runtime_checkable
class Terminal(Protocol):
    def start(self, on_input: Callable[[str], None], on_resize: Callable[[], None]) -> None: ...
    def stop(self) -> None: ...
    def write(self, data: str) -> None: ...
    @property
    def columns(self) -> int: ...
    @property
    def rows(self) -> int: ...
    def hide_cursor(self) -> None: ...
    def show_cursor(self) -> None: ...
    def clear_line(self) -> None: ...
    def clear_from_cursor(self) -> None: ...
    def clear_screen(self) -> None: ...
    def move_by(self, lines: int) -> None: ...


class ProcessTerminal:
    """Terminal backed by ``sys.stdin``/``sys.stdout``."""

    def __init__(self, *, alt_screen: bool = False) -> None:
        self._out = sys.stdout
        self._alt_screen = alt_screen
        self._started = False
        self._prev_mode = None  # saved termios state (POSIX)
        _enable_windows_vt_mode()

    # ─── size ────────────────────────────────────────────────────────────
    @property
    def columns(self) -> int:
        try:
            return os.get_terminal_size().columns
        except OSError:
            return 80

    @property
    def rows(self) -> int:
        try:
            return os.get_terminal_size().lines
        except OSError:
            return 24

    # ─── lifecycle ───────────────────────────────────────────────────────
    def start(self, on_input: Callable[[str], None], on_resize: Callable[[], None]) -> None:
        self._on_input = on_input
        self._on_resize = on_resize
        self._enter_raw_mode()
        if self._alt_screen:
            self.write(_ALT_SCREEN_ON)
        self.write(_BRACKETED_PASTE_ON)
        self.hide_cursor()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self.show_cursor()
        self.write(_BRACKETED_PASTE_OFF)
        if self._alt_screen:
            self.write(_ALT_SCREEN_OFF)
        self._exit_raw_mode()
        self._out.flush()
        self._started = False

    # ─── output ──────────────────────────────────────────────────────────
    def write(self, data: str) -> None:
        self._out.write(data)
        self._out.flush()

    def hide_cursor(self) -> None:
        self.write(_HIDE_CURSOR)

    def show_cursor(self) -> None:
        self.write(_SHOW_CURSOR)

    def clear_line(self) -> None:
        self.write("\x1b[2K")

    def clear_from_cursor(self) -> None:
        self.write("\x1b[0J")

    def clear_screen(self) -> None:
        self.write("\x1b[2J\x1b[H")

    def move_by(self, lines: int) -> None:
        """Move the cursor up (negative) or down (positive) by *lines* rows."""
        if lines < 0:
            self.write(f"\x1b[{-lines}A")
        elif lines > 0:
            self.write(f"\x1b[{lines}B")

    # ─── raw mode ──────────────────────────────────────────────────────────
    def _enter_raw_mode(self) -> None:
        if sys.platform == "win32":
            return  # msvcrt reads keys without needing termios raw mode
        try:
            import termios
            import tty

            self._prev_mode = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())
        except Exception:
            self._prev_mode = None

    def _exit_raw_mode(self) -> None:
        if sys.platform == "win32":
            return
        if self._prev_mode is not None:
            try:
                import termios

                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._prev_mode)
            except Exception:
                pass
            self._prev_mode = None

    # ─── input (blocking single read; the renderer drives the loop) ────────
    def read_key(self) -> str | None:
        """Read one key/sequence from stdin. Returns None if no TTY."""
        if not sys.stdin.isatty():
            return None
        if sys.platform == "win32":
            return self._read_key_windows()
        return self._read_key_posix()

    def _read_key_windows(self) -> str | None:
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # extended key prefix
            ch2 = msvcrt.getwch()
            return {
                "H": "\x1b[A", "P": "\x1b[B", "K": "\x1b[D", "M": "\x1b[C",
                "G": "\x1b[H", "O": "\x1b[F", "S": "\x1b[3~",
                "I": "\x1b[5~", "Q": "\x1b[6~",
            }.get(ch2, "")
        return ch

    def _read_key_posix(self) -> str | None:
        import select

        data = sys.stdin.read(1)
        if data == "\x1b":
            # Drain the rest of an escape sequence if present.
            while select.select([sys.stdin], [], [], 0.002)[0]:
                data += sys.stdin.read(1)
        return data
