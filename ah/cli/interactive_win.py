"""
Windows-compatible Interactive REPL for AgentHarness.

Uses rich for all visual output and msvcrt for keyboard input.
No prompt_toolkit dependency — works on Windows cmd.exe and PowerShell 7.

Features:
    - Rich Live for animations and streaming output
    - msvcrt for keyboard input (arrow keys, enter, escape, backspace)
    - KawaiiSpinner with kawaii faces and thinking verbs
    - Unicode response box (╭─...╮/╰...╯) for beautiful output
    - Skin-configurable color system with 8 themes
    - Interactive help menu with arrow key navigation
    - Autocomplete dropdown for slash commands
    - Status bar at bottom with session info
    - All animations from ah/cli/animations.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

# Windows-specific imports
if sys.platform == "win32":
    import msvcrt
else:
    # Fallback for non-Windows — provide stubs so the module imports
    class _MsvcrtStub:
        @staticmethod
        def kbhit() -> bool:
            return False

        @staticmethod
        def getch() -> bytes:
            return b"\x00"

    msvcrt = _MsvcrtStub()  # type: ignore[assignment]

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown
from rich.table import Table
from rich import box

from ah import __version__
from ah.core.agent import ReActAgent
from ah.core.config import config
from ah.core.context import context_manager
from ah.core.models import StreamEvent
from ah.core.provider import get_provider
from ah.core.session import Session, session_manager
from ah.db.connection import db
from ah.tools import builtins  # noqa: F401 — registers built-in tools

# Visual system and animation library
from ah.cli.visual import (
    VisualContext,
    ThemeName,
    StatusLevel,
    get_visual_context,
    THEMES,
)
from ah.cli.animations import (
    Spinner,
    SquareLoader,
    ThinkingAnimation,
    StreamingAnimation,
    ToolExecutionAnimation,
    ErrorAnimation,
    SuccessAnimation,
    FadeTransition,
    AnimationRunner,
    get_spinner,
    get_thinking_animation,
    get_streaming_animation,
    get_tool_animation,
    get_error_animation,
    get_success_animation,
    get_fade_transition,
    get_animation_runner,
    should_animate,
)

logger = logging.getLogger(__name__)

# ─── ASCII Art Banner ────────────────────────────────────────────────────────

ASCII_BANNER = r"""
  █████╗  ██████╗ ███████╗███╗   ██╗████████╗
 ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝
 ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║
 ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║
 ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║
 ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝

  ██╗  ██╗ █████╗ ██████╗ ███╗   ██╗███████╗███████╗███████╗
  ██║  ██║██╔══██╗██╔══██╗████╗  ██║██╔════╝██╔════╝██╔════╝
  ███████║███████║██████╔╝██╔██╗ ██║█████╗  ███████╗███████╗
  ██╔══██║██╔══██║██╔══██╗██║╚██╗██║██╔══╝  ╚════██║╚════██║
  ██║  ██║██║  ██║██║  ██║██║ ╚████║███████╗███████║███████║
  ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝╚══════╝
"""

# ─── Command registry ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandDef:
    """Definition of a slash command."""
    name: str
    description: str
    usage: str = ""
    category: str = "general"
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def display_name(self) -> str:
        return f"/{self.name}"


# All slash commands, ordered for display
COMMAND_REGISTRY: dict[str, CommandDef] = {
    "help": CommandDef(
        name="help",
        description="Show available slash commands",
        usage="/help",
        category="general",
    ),
    "status": CommandDef(
        name="status",
        description="Show current session and agent status",
        usage="/status",
        category="session",
    ),
    "sessions": CommandDef(
        name="sessions",
        description="List recent sessions",
        usage="/sessions",
        category="session",
    ),
    "new": CommandDef(
        name="new",
        description="Create a new session",
        usage="/new [title]",
        category="session",
    ),
    "switch": CommandDef(
        name="switch",
        description="Switch to a different session",
        usage="/switch <session_id>",
        category="session",
    ),
    "context": CommandDef(
        name="context",
        description="Show context for current session",
        usage="/context",
        category="session",
    ),
    "model": CommandDef(
        name="model",
        description="Show or set the model",
        usage="/model [model_name]",
        category="config",
    ),
    "provider": CommandDef(
        name="provider",
        description="Show or set the provider",
        usage="/provider [openrouter|ollama]",
        category="config",
    ),
    "budget": CommandDef(
        name="budget",
        description="Show or set context budget",
        usage="/budget <tokens>",
        category="config",
    ),
    "verbose": CommandDef(
        name="verbose",
        description="Toggle verbose mode on/off",
        usage="/verbose",
        category="config",
    ),
    "config": CommandDef(
        name="config",
        description="Show current configuration",
        usage="/config",
        category="config",
    ),
    "skin": CommandDef(
        name="skin",
        description="Show or set the color skin",
        usage="/skin [skin_name]",
        category="config",
    ),
    "clear": CommandDef(
        name="clear",
        description="Clear the screen",
        usage="/clear",
        category="general",
    ),
    "compress": CommandDef(
        name="compress",
        description="Compress context for current session",
        usage="/compress",
        category="session",
    ),
    "exit": CommandDef(
        name="exit",
        description="Exit the REPL",
        usage="/exit",
        category="general",
        aliases=("quit",),
    ),
}

# Backwards-compatible alias
SLASH_COMMANDS: dict[str, str] = {
    cmd.name: cmd.description for cmd in COMMAND_REGISTRY.values()
}


# ─── Windows Input Handler ───────────────────────────────────────────────────


class WindowsInputHandler:
    """Handles keyboard input on Windows using msvcrt.

    Supports:
        - Regular character input
        - Arrow keys (up/down/left/right)
        - Enter, Escape, Backspace, Delete
        - Ctrl+C, Ctrl+D
        - Tab (for autocomplete)
    """

    # Special key codes from msvcrt.getch()
    VK_BACKSPACE = b"\x08"
    VK_TAB = b"\x09"
    VK_ENTER = b"\r"
    VK_ESCAPE = b"\x1b"
    VK_DELETE = b"\x53"
    VK_CTRL_C = b"\x03"
    VK_CTRL_D = b"\x04"

    # Arrow keys are escape sequences: \xe0 or \x00 followed by a code
    ARROW_PREFIXES = (b"\xe0", b"\x00")
    VK_UP = b"\x48"
    VK_DOWN = b"\x50"
    VK_LEFT = b"\x4b"
    VK_RIGHT = b"\x4d"

    def __init__(self) -> None:
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._input_event = threading.Event()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background input reader thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background input reader thread."""
        self._running = False
        self._input_event.set()

    def _read_loop(self) -> None:
        """Background thread that reads keyboard input via msvcrt."""
        while self._running:
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    self._handle_key(ch)
                else:
                    time.sleep(0.01)
            except Exception:
                time.sleep(0.05)

    def _handle_key(self, ch: bytes) -> None:
        """Process a key press from msvcrt."""
        with self._lock:
            if ch == self.VK_BACKSPACE:
                if self._buffer:
                    self._buffer.pop()
            elif ch == self.VK_ENTER:
                self._buffer.append("\n")
                self._input_event.set()
            elif ch == self.VK_ESCAPE:
                self._buffer.append("\x1b")
                self._input_event.set()
            elif ch == self.VK_CTRL_C:
                self._buffer.append("\x03")
                self._input_event.set()
            elif ch == self.VK_CTRL_D:
                self._buffer.append("\x04")
                self._input_event.set()
            elif ch == self.VK_TAB:
                self._buffer.append("\t")
                self._input_event.set()
            elif ch in self.ARROW_PREFIXES:
                # Arrow key — read the second byte
                if msvcrt.kbhit():
                    ch2 = msvcrt.getch()
                    if ch2 == self.VK_UP:
                        self._buffer.append("\x1b[A")
                    elif ch2 == self.VK_DOWN:
                        self._buffer.append("\x1b[B")
                    elif ch2 == self.VK_LEFT:
                        self._buffer.append("\x1b[D")
                    elif ch2 == self.VK_RIGHT:
                        self._buffer.append("\x1b[C")
                    self._input_event.set()
            elif ch == self.VK_DELETE:
                self._buffer.append("\x1b[3~")
                self._input_event.set()
            else:
                # Regular character
                try:
                    char = ch.decode("utf-8", errors="ignore")
                    if char:
                        self._buffer.append(char)
                except Exception:
                    pass

    def get_input(self, timeout: float | None = None) -> str | None:
        """Get a line of input from the buffer.

        Returns the input string when Enter is pressed, or None on timeout.
        """
        if timeout is not None:
            self._input_event.wait(timeout)
        else:
            self._input_event.wait()

        with self._lock:
            if self._buffer:
                result = "".join(self._buffer)
                self._buffer.clear()
                self._input_event.clear()
                return result
            self._input_event.clear()
            return None

    def has_input(self) -> bool:
        """Check if there's input available in the buffer."""
        with self._lock:
            return len(self._buffer) > 0

    def get_char(self) -> str | None:
        """Get a single character from the buffer without waiting for Enter."""
        with self._lock:
            if self._buffer:
                result = "".join(self._buffer)
                self._buffer.clear()
                return result
            return None

    def clear_buffer(self) -> None:
        """Clear the input buffer."""
        with self._lock:
            self._buffer.clear()
            self._input_event.clear()


# ─── SkinConfig ──────────────────────────────────────────────────────────────


class SkinConfig:
    """Skin-configurable color system with 8 themes.

    Wraps the existing visual system to provide a unified skin interface.
    Each skin defines a complete color palette for the REPL.

    Available skins:
        - default: Dark background, bright colors (cyan/purple)
        - gold: Golden/amber tones
        - crimson: Bold reds and warm accents
        - ocean: Deep blues and teals
        - forest: Greens and earth tones
        - sunset: Warm oranges, pinks, and purples
        - midnight: Deep purples and night-sky blues
        - arctic: Ice blues, whites, and cool grays
        - volcanic: Dark reds, oranges, and ash grays
    """

    SKINS = {
        "default": ThemeName.DEFAULT,
        "gold": ThemeName.GOLD,
        "crimson": ThemeName.CRIMSON,
        "ocean": ThemeName.OCEAN,
        "forest": ThemeName.FOREST,
        "sunset": ThemeName.SUNSET,
        "midnight": ThemeName.MIDNIGHT,
        "arctic": ThemeName.ARCTIC,
        "volcanic": ThemeName.VOLCANIC,
    }

    def __init__(self, skin: str = "default", console: Console | None = None) -> None:
        self._skin_name = skin
        self._console = console or Console()
        self._viz = get_visual_context(skin)

    @property
    def skin_name(self) -> str:
        return self._skin_name

    @property
    def console(self) -> Console:
        return self._console

    @property
    def viz(self) -> VisualContext:
        return self._viz

    def set_skin(self, skin: str) -> None:
        """Switch to a different skin."""
        if skin in self.SKINS:
            self._skin_name = skin
            self._viz = VisualContext(self.SKINS[skin], console=self._console)

    def get_color(self, role: str) -> str:
        """Get a color for the given role in the current skin."""
        return self._viz._color(role)

    def style(self, role: str, **kwargs) -> str:
        """Get a style string for the given role."""
        return self._viz.style(role, **kwargs)

    def panel(self, content, **kwargs) -> Panel:
        """Create a themed panel."""
        return self._viz.panel(content, **kwargs)

    def print_panel(self, content, **kwargs) -> None:
        """Print a themed panel."""
        self._viz.print_panel(content, **kwargs)

    @staticmethod
    def available_skins() -> list[str]:
        """Get list of available skin names."""
        return list(SkinConfig.SKINS.keys())


# ─── StatusBar ───────────────────────────────────────────────────────────────


class StatusBar:
    """Bottom status bar with session info.

    Displays a beautiful status bar at the bottom of the REPL:
    ─────────────────────────────────────────────
     Session: abc123 | Model: gpt-4o | Tokens: 1,234
    """

    def __init__(self, skin: SkinConfig) -> None:
        self.skin = skin

    def render(
        self,
        session: Session | None = None,
        model: str = "",
        provider: str = "",
        tokens: int = 0,
        verbose: bool = False,
    ) -> Text:
        """Render the status bar."""
        text = Text()

        # Separator line
        text.append("─" * 40, style=self.skin.style("muted"))
        text.append("\n")

        # Status content
        text.append(" Session: ", style=self.skin.style("muted"))
        if session:
            text.append(f"{str(session.id)[:8]}", style=self.skin.style("secondary"))
        else:
            text.append("None", style=self.skin.style("error"))

        text.append(" | Model: ", style=self.skin.style("muted"))
        text.append(f"{model}", style=self.skin.style("info"))

        text.append(" | Provider: ", style=self.skin.style("muted"))
        text.append(f"{provider}", style=self.skin.style("info"))

        text.append(" | Tokens: ", style=self.skin.style("muted"))
        text.append(f"{tokens:,}", style=self.skin.style("warning"))

        if verbose:
            text.append(" | Verbose: ", style=self.skin.style("muted"))
            text.append("on", style=self.skin.style("success"))

        return text

    def print(self, **kwargs) -> None:
        """Print the status bar."""
        self.skin.console.print(self.render(**kwargs))
        self.skin.console.print()


# ─── UnicodeResponseBox ──────────────────────────────────────────────────────


class UnicodeResponseBox:
    """Beautiful Unicode box for response display.

    Creates a stunning box with rounded corners and styled content:
        ╭──────────────────────────────────────╮
        │ Response text here...                │
        ╰──────────────────────────────────────╯
    """

    # Box-drawing characters
    TOP_LEFT = "╭"
    TOP_RIGHT = "╮"
    BOTTOM_LEFT = "╰"
    BOTTOM_RIGHT = "╯"
    HORIZONTAL = "─"
    VERTICAL = "│"

    def __init__(
        self,
        console: Console,
        skin: str = "default",
        title: str = "",
        style: str = "primary",
    ) -> None:
        self.console = console
        self.skin = skin
        self.title = title
        self.style = style

    def render(self, content: str, width: int | None = None) -> Text:
        """Render content in a beautiful Unicode box."""
        if width is None:
            width = self.console.width - 4

        # Calculate content width
        content_width = width - 4  # Account for borders and padding

        # Build the box
        text = Text()

        # Top border with title
        if self.title:
            title_str = f" {self.title} "
            title_len = len(title_str)
            remaining = width - 2 - title_len
            left_fill = remaining // 2
            right_fill = remaining - left_fill
            text.append(self.TOP_LEFT + self.HORIZONTAL * left_fill, style=self.style)
            text.append(title_str, style=f"bold {self.style}")
            text.append(self.HORIZONTAL * right_fill + self.TOP_RIGHT + "\n", style=self.style)
        else:
            text.append(self.TOP_LEFT + self.HORIZONTAL * (width - 2) + self.TOP_RIGHT + "\n", style=self.style)

        # Content lines
        lines = content.split("\n")
        for line in lines:
            # Truncate or pad line to fit
            if len(line) > content_width:
                line = line[:content_width - 3] + "..."
            padded = line.ljust(content_width)
            text.append(f" {self.VERTICAL} ", style=self.style)
            text.append(padded, style="text")
            text.append(f" {self.VERTICAL}\n", style=self.style)

        # Bottom border
        text.append(self.BOTTOM_LEFT + self.HORIZONTAL * (width - 2) + self.BOTTOM_RIGHT, style=self.style)

        return text

    def print(self, content: str, width: int | None = None) -> None:
        """Print content in a Unicode box."""
        self.console.print(self.render(content, width))


# ─── Interactive help menu ──────────────────────────────────────────────────


def _build_help_text() -> str:
    """Build the help text with all commands grouped by category."""
    lines: list[str] = []
    lines.append("AgentHarness — Slash Commands")
    lines.append("=" * 40)
    lines.append("")

    # Group commands by category
    categories: dict[str, list[CommandDef]] = {}
    for cmd in COMMAND_REGISTRY.values():
        categories.setdefault(cmd.category, []).append(cmd)

    category_order = ["general", "session", "config"]
    for cat in category_order:
        cmds = categories.get(cat, [])
        if not cmds:
            continue
        lines.append(f"  {cat.upper()}")
        lines.append("  " + "-" * 38)
        for cmd in cmds:
            lines.append(f"    {cmd.display_name:<16} {cmd.description}")
            if cmd.usage:
                lines.append(f"    {'':<16} Usage: {cmd.usage}")
        lines.append("")

    lines.append("=" * 40)
    lines.append("↑/↓ navigate  •  Enter select  •  Esc close")
    return "\n".join(lines)


async def run_interactive_help(console: Console, skin: SkinConfig) -> str | None:
    """Show an interactive, scrollable help menu using msvcrt for navigation.

    The user can navigate with arrow keys and select a command with Enter.
    Pressing Esc or Ctrl+C closes the menu.

    Returns the selected command name, or None if closed without selection.
    """
    help_text = _build_help_text()
    lines = help_text.split("\n")
    selected_idx = 0
    scroll_offset = 0
    max_visible = console.height - 4 if console.height else 20

    # Find all command lines (lines starting with /)
    command_lines: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("/"):
            command_lines.append((i, stripped))

    if not command_lines:
        console.print(help_text)
        return None

    selected_command: list[str | None] = [None]

    def render_help_view() -> Text:
        """Render the visible portion of the help menu."""
        text = Text()
        visible_lines = lines[scroll_offset:scroll_offset + max_visible]

        for i, line in enumerate(visible_lines):
            abs_idx = scroll_offset + i
            # Check if this line is a command line
            is_command = False
            cmd_name = None
            for cmd_idx, (line_idx, stripped) in enumerate(command_lines):
                if line_idx == abs_idx:
                    is_command = True
                    cmd_name = stripped.split()[0].lstrip("/").lower()
                    if cmd_idx == selected_idx:
                        # Highlight selected line
                        text.append(" ▶ ", style=skin.style("warning", bold=True))
                        text.append(line, style=skin.style("warning", bold=True))
                    else:
                        text.append("   ", style=skin.style("muted"))
                        text.append(line, style=skin.style("text"))
                    break

            if not is_command:
                text.append("   ", style=skin.style("muted"))
                text.append(line, style=skin.style("muted"))

            text.append("\n")

        # Navigation hint at bottom
        text.append("─" * 40, style=skin.style("muted"))
        text.append("\n")
        text.append(" ↑/↓ navigate  •  Enter select  •  Esc close", style=skin.style("muted"))

        return text

    # Use Live for the help menu
    with Live(
        render_help_view(),
        console=console,
        refresh_per_second=10,
        transient=False,
    ) as live:
        while True:
            # Wait for input
            char = await asyncio.get_event_loop().run_in_executor(
                None, _wait_for_char
            )
            if char is None:
                continue

            if char == "\x1b":  # Escape
                break
            elif char == "\x03":  # Ctrl+C
                break
            elif char == "\r":  # Enter
                # Select the current command
                if 0 <= selected_idx < len(command_lines):
                    _, stripped = command_lines[selected_idx]
                    cmd_name = stripped.split()[0].lstrip("/").lower()
                    if cmd_name in COMMAND_REGISTRY:
                        selected_command[0] = cmd_name
                    else:
                        for name, cmd in COMMAND_REGISTRY.items():
                            if cmd_name in cmd.aliases:
                                selected_command[0] = name
                                break
                break
            elif char == "\x1b[A":  # Up arrow
                if selected_idx > 0:
                    selected_idx -= 1
                    # Adjust scroll if needed
                    line_idx = command_lines[selected_idx][0]
                    if line_idx < scroll_offset:
                        scroll_offset = line_idx
            elif char == "\x1b[B":  # Down arrow
                if selected_idx < len(command_lines) - 1:
                    selected_idx += 1
                    # Adjust scroll if needed
                    line_idx = command_lines[selected_idx][0]
                    if line_idx >= scroll_offset + max_visible:
                        scroll_offset = line_idx - max_visible + 1
            elif char == "\t":  # Tab — cycle through commands
                selected_idx = (selected_idx + 1) % len(command_lines)
                line_idx = command_lines[selected_idx][0]
                if line_idx >= scroll_offset + max_visible:
                    scroll_offset = line_idx - max_visible + 1
                elif line_idx < scroll_offset:
                    scroll_offset = line_idx

            live.update(render_help_view())

    return selected_command[0]


def _wait_for_char() -> str | None:
    """Wait for a single character from msvcrt (runs in executor)."""
    if sys.platform != "win32":
        return None
    try:
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b"\xe0", b"\x00"):
                # Arrow key
                if msvcrt.kbhit():
                    ch2 = msvcrt.getch()
                    if ch2 == b"\x48":
                        return "\x1b[A"  # Up
                    elif ch2 == b"\x50":
                        return "\x1b[B"  # Down
                    elif ch2 == b"\x4b":
                        return "\x1b[D"  # Left
                    elif ch2 == b"\x4d":
                        return "\x1b[C"  # Right
            elif ch == b"\x1b":
                return "\x1b"  # Escape
            elif ch == b"\r":
                return "\r"  # Enter
            elif ch == b"\x03":
                return "\x03"  # Ctrl+C
            elif ch == b"\x04":
                return "\x04"  # Ctrl+D
            elif ch == b"\t":
                return "\t"  # Tab
            elif ch == b"\x08":
                return "\x08"  # Backspace
            else:
                return ch.decode("utf-8", errors="ignore")
        time.sleep(0.01)
        return None
    except Exception:
        return None


# ─── Autocomplete dropdown ──────────────────────────────────────────────────


class AutocompleteDropdown:
    """Dropdown for slash command autocomplete.

    Shows matching commands as the user types '/'.
    Arrow keys navigate the dropdown; Enter selects.
    """

    def __init__(self, console: Console, skin: SkinConfig) -> None:
        self.console = console
        self.skin = skin
        self._matches: list[CommandDef] = []
        self._selected_idx = 0
        self._visible = False

    def update_matches(self, text: str) -> None:
        """Update matching commands based on typed text."""
        if not text.startswith("/"):
            self._matches = []
            self._visible = False
            return

        prefix = text[1:].lower()
        self._matches = [
            cmd for name, cmd in COMMAND_REGISTRY.items()
            if name.startswith(prefix)
        ]
        self._selected_idx = 0
        self._visible = len(self._matches) > 0

    def render(self) -> Text | None:
        """Render the autocomplete dropdown."""
        if not self._visible or not self._matches:
            return None

        text = Text()
        text.append("┌" + "─" * 38 + "┐\n", style=self.skin.style("muted"))

        for i, cmd in enumerate(self._matches[:8]):  # Show max 8 matches
            if i == self._selected_idx:
                text.append("│ ", style=self.skin.style("muted"))
                text.append(f"{cmd.display_name:<16}", style=self.skin.style("warning", bold=True))
                text.append(f" {cmd.description:<20}", style=self.skin.style("warning"))
                text.append(" │\n", style=self.skin.style("muted"))
            else:
                text.append("│ ", style=self.skin.style("muted"))
                text.append(f"{cmd.display_name:<16}", style=self.skin.style("text"))
                text.append(f" {cmd.description:<20}", style=self.skin.style("muted"))
                text.append(" │\n", style=self.skin.style("muted"))

        text.append("└" + "─" * 38 + "┘", style=self.skin.style("muted"))
        return text

    def next_match(self) -> None:
        """Select the next match in the dropdown."""
        if self._matches:
            self._selected_idx = (self._selected_idx + 1) % len(self._matches)

    def prev_match(self) -> None:
        """Select the previous match in the dropdown."""
        if self._matches:
            self._selected_idx = (self._selected_idx - 1) % len(self._matches)

    def get_selected(self) -> CommandDef | None:
        """Get the currently selected match."""
        if self._matches and 0 <= self._selected_idx < len(self._matches):
            return self._matches[self._selected_idx]
        return None

    def hide(self) -> None:
        """Hide the dropdown."""
        self._visible = False
        self._matches = []


# ─── Interactive REPL ────────────────────────────────────────────────────────


class InteractiveREPL:
    """Interactive REPL for AgentHarness — Windows-compatible.

    Uses rich Live for animations and msvcrt for keyboard input.
    No prompt_toolkit dependency.

    Features:
    - KawaiiSpinner with kawaii faces and thinking verbs
    - Unicode response box for beautiful output
    - Skin-configurable color system with 8 themes
    - Interactive help menu with arrow key navigation
    - Autocomplete dropdown for slash commands
    - Status bar at bottom with session info
    - All animations from ah/cli/animations.py

    Usage:
        repl = InteractiveREPL()
        await repl.run()
    """

    def __init__(
        self,
        model: str | None = None,
        provider: str | None = None,
        verbose: bool | None = None,
        session_id: str | None = None,
        skin: str = "default",
    ) -> None:
        self.skin = SkinConfig(skin)
        self.console = self.skin.console
        self.model = model or config.get("model")
        self.provider = provider or config.get("provider")
        self.verbose = config.get("verbose") if verbose is None else verbose
        self.context_budget = config.get("context_budget")
        self.agent_id = config.get("agent_id")

        # Session state
        self.session: Session | None = None
        if session_id:
            try:
                sid = uuid.UUID(session_id)
                self._initial_session_id = sid
            except ValueError:
                self.console.print(f"[red]Invalid session ID: {session_id}[/red]")
                self._initial_session_id = None
        else:
            self._initial_session_id = None

        # Windows input handler
        self._input_handler = WindowsInputHandler()

        # Agent
        self._agent: ReActAgent | None = None

        # Running flag
        self._running = False

        # Animation state
        self._current_animation: asyncio.Task | None = None
        self._live: Live | None = None

        # KawaiiSpinner
        self._kawaii_spinner: Spinner | None = None

        # Status bar
        self._status_bar = StatusBar(self.skin)

        # Unicode response box
        self._response_box = UnicodeResponseBox(self.console, skin=skin)

        # Autocomplete
        self._autocomplete = AutocompleteDropdown(self.console, self.skin)

        # Fade transition
        self._fade = FadeTransition(self.console, duration=0.3)

        # Token counter
        self._tokens_used = 0

        # Input state
        self._input_buffer = ""
        self._cursor_pos = 0
        self._history: list[str] = []
        self._history_idx = -1

    def _get_history_file(self):
        """Get the path to the history file."""
        from pathlib import Path
        history_dir = Path.home() / ".agent-harness"
        history_dir.mkdir(parents=True, exist_ok=True)
        return history_dir / "repl_history_win"

    async def run(self) -> None:
        """Run the interactive REPL."""
        self._running = True

        # Start the input handler
        self._input_handler.start()

        # Connect to database
        await db.connect()
        try:
            # Load or create initial session
            if self._initial_session_id:
                self.session = await session_manager.get(self._initial_session_id)
                if not self.session:
                    self.console.print(f"[red]Session {self._initial_session_id} not found[/red]")
                    self.session = None

            if not self.session:
                self.session = await session_manager.create(
                    title="Interactive REPL",
                    goal=None,
                    model=self.model,
                    provider=self.provider,
                    context_budget=self.context_budget,
                )

            # Show ASCII art banner
            self._show_banner()

            # Main REPL loop
            while self._running:
                try:
                    # Get user input
                    user_input = await self._get_input()
                except (EOFError, KeyboardInterrupt):
                    self.console.print("\n[dim]Use /exit to quit.[/dim]")
                    continue

                if user_input is None:
                    continue

                if not user_input.strip():
                    continue

                # Handle slash commands
                if user_input.strip().startswith("/"):
                    should_continue = await self._handle_slash_command(user_input.strip())
                    if not should_continue:
                        break
                    continue

                # Send message to agent
                await self._handle_message(user_input.strip())

        finally:
            self._running = False
            self._input_handler.stop()
            await db.close()

    def _show_banner(self) -> None:
        """Display ASCII art banner with per-skin colors."""
        # Print ASCII banner with skin colors
        banner_text = Text()
        banner_text.append(ASCII_BANNER, style=self.skin.style("primary", bold=True))
        banner_text.append("\n")
        banner_text.append("  Self-hosted Multi-Agent AI Orchestration\n", style=self.skin.style("muted"))
        banner_text.append(f"  v{__version__}", style=self.skin.style("muted"))
        self.console.print(banner_text)
        self.console.print()

        # Welcome panel with Unicode box
        session_id_str = str(self.session.id) if self.session else "None"
        welcome_text = Text()
        welcome_text.append("AgentHarness Interactive REPL", style=self.skin.style("primary", bold=True))
        welcome_text.append(f" v{__version__}\n\n", style=self.skin.style("muted"))
        welcome_text.append("Session: ", style=self.skin.style("muted"))
        welcome_text.append(f"{session_id_str}\n", style=self.skin.style("secondary"))
        welcome_text.append("Model: ", style=self.skin.style("muted"))
        welcome_text.append(f"{self.model}", style=self.skin.style("success"))
        welcome_text.append(" | Provider: ", style=self.skin.style("muted"))
        welcome_text.append(f"{self.provider}\n", style=self.skin.style("success"))
        welcome_text.append("Skin: ", style=self.skin.style("muted"))
        welcome_text.append(f"{self.skin.skin_name}", style=self.skin.style("info"))
        welcome_text.append("\n\nType ", style=self.skin.style("muted"))
        welcome_text.append("/help", style=self.skin.style("warning", bold=True))
        welcome_text.append(" for commands, ", style=self.skin.style("muted"))
        welcome_text.append("/exit", style=self.skin.style("error", bold=True))
        welcome_text.append(" to quit.", style=self.skin.style("muted"))

        self.console.print(
            self.skin.panel(welcome_text, style="primary", title="Welcome")
        )
        self.console.print()

    def _get_prompt_text(self) -> str:
        """Get the prompt text with session info."""
        if self.session:
            short_id = str(self.session.id)[:8]
            return f"ah ({short_id}) > "
        return "ah > "

    def _get_status_bar(self) -> Text:
        """Build the status bar text."""
        return self._status_bar.render(
            session=self.session,
            model=self.model,
            provider=self.provider,
            tokens=self._tokens_used,
            verbose=self.verbose,
        )

    def _print_status_bar(self) -> None:
        """Print the status bar at the bottom."""
        self.console.print(self._get_status_bar())
        self.console.print()

    async def _get_input(self) -> str | None:
        """Get user input using msvcrt for keyboard handling.

        Supports:
        - Regular character input
        - Arrow keys for history navigation
        - Backspace for deletion
        - Tab for autocomplete
        - Enter to submit
        - Escape to clear input
        """
        self._input_buffer = ""
        self._cursor_pos = 0
        self._history_idx = -1
        self._autocomplete.hide()

        prompt = self._get_prompt_text()

        # Fallback to regular input() when stdin is not a TTY (piped/redirected)
        if not sys.stdin.isatty():
            try:
                return await asyncio.get_event_loop().run_in_executor(
                    None, input, f"{prompt} "
                )
            except (EOFError, KeyboardInterrupt):
                return None

        while True:
            # Render the current input line
            self._render_input_line(prompt)

            # Wait for a key press
            char = await asyncio.get_event_loop().run_in_executor(
                None, _wait_for_char
            )
            if char is None:
                continue

            if char == "\r":  # Enter
                # If autocomplete is visible, select the current match and submit
                if self._autocomplete._visible:
                    selected = self._autocomplete.get_selected()
                    if selected:
                        result = selected.display_name
                        if result.strip():
                            self._history.append(result)
                        self.console.print()
                        return result

                # Submit the input
                result = self._input_buffer
                if result.strip():
                    self._history.append(result)
                self.console.print()  # Move to next line
                return result

            elif char == "\x1b":  # Escape
                self._input_buffer = ""
                self._cursor_pos = 0
                self._autocomplete.hide()
                continue

            elif char == "\x03":  # Ctrl+C
                self.console.print()
                raise KeyboardInterrupt

            elif char == "\x04":  # Ctrl+D
                self.console.print()
                raise EOFError

            elif char == "\x08":  # Backspace
                if self._cursor_pos > 0:
                    self._input_buffer = (
                        self._input_buffer[:self._cursor_pos - 1]
                        + self._input_buffer[self._cursor_pos:]
                    )
                    self._cursor_pos -= 1
                    self._update_autocomplete()

            elif char == "\x1b[A":  # Up arrow — history
                if self._history:
                    if self._history_idx < len(self._history) - 1:
                        self._history_idx += 1
                        self._input_buffer = self._history[-(self._history_idx + 1)]
                        self._cursor_pos = len(self._input_buffer)
                        self._autocomplete.hide()

            elif char == "\x1b[B":  # Down arrow — history
                if self._history_idx > 0:
                    self._history_idx -= 1
                    self._input_buffer = self._history[-(self._history_idx + 1)]
                    self._cursor_pos = len(self._input_buffer)
                elif self._history_idx == 0:
                    self._history_idx = -1
                    self._input_buffer = ""
                    self._cursor_pos = 0
                self._autocomplete.hide()

            elif char == "\x1b[C":  # Right arrow
                if self._cursor_pos < len(self._input_buffer):
                    self._cursor_pos += 1

            elif char == "\x1b[D":  # Left arrow
                if self._cursor_pos > 0:
                    self._cursor_pos -= 1

            elif char == "\t":  # Tab — autocomplete
                if self._autocomplete._visible:
                    self._autocomplete.next_match()
                else:
                    self._update_autocomplete()

            elif char == "\x1b[3~":  # Delete
                if self._cursor_pos < len(self._input_buffer):
                    self._input_buffer = (
                        self._input_buffer[:self._cursor_pos]
                        + self._input_buffer[self._cursor_pos + 1:]
                    )
                    self._update_autocomplete()

            elif char and char.isprintable():
                # Regular character input
                self._input_buffer = (
                    self._input_buffer[:self._cursor_pos]
                    + char
                    + self._input_buffer[self._cursor_pos:]
                )
                self._cursor_pos += 1
                self._update_autocomplete()

    def _update_autocomplete(self) -> None:
        """Update autocomplete matches based on current input."""
        # Only show autocomplete if input starts with /
        if self._input_buffer.startswith("/"):
            self._autocomplete.update_matches(self._input_buffer)
        else:
            self._autocomplete.hide()

    def _render_input_line(self, prompt: str) -> None:
        """Render the current input line with prompt and autocomplete."""
        # Clear the current line and re-render
        # Use rich console to print the prompt and input
        text = Text()
        text.append(prompt, style=self.skin.style("primary", bold=True))

        # Show the input buffer with cursor
        before_cursor = self._input_buffer[:self._cursor_pos]
        at_cursor = self._input_buffer[self._cursor_pos:self._cursor_pos + 1]
        after_cursor = self._input_buffer[self._cursor_pos + 1:]

        text.append(before_cursor, style=self.skin.style("text"))
        if at_cursor:
            text.append(at_cursor, style=self.skin.style("highlight", reverse=True))
        else:
            text.append(" ", style=self.skin.style("highlight", reverse=True))
        text.append(after_cursor, style=self.skin.style("text"))

        # Print the input line
        self.console.print(text, end="\r")

        # Show autocomplete dropdown if visible
        dropdown = self._autocomplete.render()
        if dropdown:
            self.console.print()
            self.console.print(dropdown)

    async def _handle_slash_command(self, command: str) -> bool:
        """Handle a slash command. Returns False if the REPL should exit."""
        parts = command.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # Strip leading slash for lookup
        cmd_name = cmd.lstrip("/")

        if cmd_name in ("exit", "quit"):
            self.console.print(self.skin.viz.success("Goodbye!"))
            return False

        elif cmd_name == "help":
            await self._show_help()

        elif cmd_name == "status":
            await self._show_status()

        elif cmd_name == "sessions":
            await self._list_sessions()

        elif cmd_name == "new":
            await self._new_session(args)

        elif cmd_name == "switch":
            await self._switch_session(args)

        elif cmd_name == "context":
            await self._show_context()

        elif cmd_name == "model":
            self._set_model(args)

        elif cmd_name == "provider":
            self._set_provider(args)

        elif cmd_name == "budget":
            self._set_budget(args)

        elif cmd_name == "verbose":
            self._toggle_verbose()

        elif cmd_name == "config":
            self._show_config()

        elif cmd_name == "skin":
            self._set_skin(args)

        elif cmd_name == "clear":
            self.console.clear()

        elif cmd_name == "compress":
            await self._compress_context()

        else:
            self.console.print(self.skin.viz.error(f"Unknown command: {cmd}"))
            self.console.print(self.skin.viz.muted("Type /help for available commands."))

        return True

    async def _show_help(self) -> None:
        """Show interactive help menu."""
        selected = await run_interactive_help(self.console, self.skin)
        if selected:
            # Execute the selected command
            await self._handle_slash_command(selected)

    async def _show_status(self) -> None:
        """Show current session and agent status."""
        if not self.session:
            self.console.print(self.skin.viz.warning("No active session."))
            return

        # Build status content
        status_text = Text()
        status_text.append("Session ID: ", style=self.skin.style("muted"))
        status_text.append(f"{self.session.id}\n", style=self.skin.style("secondary"))
        status_text.append("Title: ", style=self.skin.style("muted"))
        status_text.append(f"{self.session.title or '(untitled)'}\n", style=self.skin.style("text"))
        status_text.append("Status: ", style=self.skin.style("muted"))
        status_text.append(f"{self.session.status}\n", style=self.skin.style("success"))
        status_text.append("Model: ", style=self.skin.style("muted"))
        status_text.append(f"{self.model}\n", style=self.skin.style("info"))
        status_text.append("Provider: ", style=self.skin.style("muted"))
        status_text.append(f"{self.provider}\n", style=self.skin.style("info"))
        status_text.append("Context Budget: ", style=self.skin.style("muted"))
        status_text.append(f"{self.context_budget}\n", style=self.skin.style("warning"))
        status_text.append("Verbose: ", style=self.skin.style("muted"))
        status_text.append(f"{'on' if self.verbose else 'off'}\n", style=self.skin.style("text"))
        status_text.append("Agent ID: ", style=self.skin.style("muted"))
        status_text.append(f"{self.agent_id}", style=self.skin.style("text"))

        self.console.print(
            self.skin.panel(status_text, style="info", title="Status")
        )
        self.console.print()

    async def _list_sessions(self) -> None:
        """List recent sessions."""
        sessions = await session_manager.list_sessions(limit=10)
        if not sessions:
            self.console.print(self.skin.viz.warning("No sessions found."))
            return

        table = self.skin.viz.table(style="standard", title="Recent Sessions")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Title", style="white")
        table.add_column("Status", style="green")
        table.add_column("Model", style="dim")
        table.add_column("Last Activity", style="dim")

        for s in sessions:
            table.add_row(
                str(s.id)[:8],
                s.title or "(untitled)",
                s.status,
                s.model or self.model,
                s.last_activity.strftime("%Y-%m-%d %H:%M"),
            )

        self.console.print(table)
        self.console.print()

    async def _new_session(self, title: str = "") -> None:
        """Create a new session."""
        self.session = await session_manager.create(
            title=title or "Interactive REPL",
            goal=None,
            model=self.model,
            provider=self.provider,
            context_budget=self.context_budget,
        )
        self.console.print(self.skin.viz.success(f"New session created: {self.session.id}"))
        self.console.print()

    async def _switch_session(self, session_id: str) -> None:
        """Switch to a different session."""
        if not session_id:
            self.console.print(self.skin.viz.warning("Usage: /switch <session_id>"))
            return

        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            self.console.print(self.skin.viz.error(f"Invalid session ID: {session_id}"))
            return

        session = await session_manager.get(sid)
        if not session:
            self.console.print(self.skin.viz.error(f"Session {session_id} not found"))
            return

        self.session = session
        self.console.print(self.skin.viz.success(f"Switched to session: {session.id}"))
        self.console.print()

    async def _show_context(self) -> None:
        """Show context for current session."""
        if not self.session:
            self.console.print(self.skin.viz.warning("No active session."))
            return

        chunks = await context_manager.get_chunks(self.session.id, limit=10)
        if not chunks:
            self.console.print(self.skin.viz.warning("No context chunks found."))
            return

        table = self.skin.viz.table(
            style="standard",
            title=f"Context (session {str(self.session.id)[:8]})"
        )
        table.add_column("Type", style="cyan")
        table.add_column("Agent", style="green")
        table.add_column("Tokens", style="yellow")
        table.add_column("Created", style="dim")

        for c in chunks:
            table.add_row(
                c.chunk_type,
                c.agent_id,
                str(c.token_count),
                c.created_at.strftime("%H:%M:%S"),
            )

        self.console.print(table)

        total_tokens = await context_manager.get_token_usage(self.session.id)
        self.console.print(f"\n[dim]Total tokens: {total_tokens}[/dim]")
        self.console.print()

    async def _compress_context(self) -> None:
        """Compress context for the current session."""
        from ah.core.compression import ContextCompressor, CompressionConfig

        if not self.session:
            self.console.print(self.skin.viz.warning("No active session."))
            return

        # Get all chunks for the session
        chunks = await context_manager.get_chunks(self.session.id, limit=1000)
        if not chunks:
            self.console.print(self.skin.viz.warning("No context chunks to compress."))
            return

        total_tokens = await context_manager.get_token_usage(self.session.id)
        self.console.print(f"[dim]Current context: {len(chunks)} chunks, {total_tokens} tokens[/dim]")

        # Build compression config from global config
        comp_config = CompressionConfig(
            enabled=config.get("compression_enabled"),
            threshold=config.get("compression_threshold"),
            target_ratio=config.get("compression_target_ratio"),
            preserve_recent=config.get("compression_preserve_recent"),
            llm_summarize=config.get("compression_llm_summarize"),
        )

        compressor = ContextCompressor(config=comp_config)

        # Try to get LLM provider for summarization
        llm_provider = None
        if comp_config.llm_summarize:
            try:
                llm_provider = get_provider(provider=self.provider, model=self.model)
            except Exception:
                llm_provider = None

        result = compressor.compress(
            chunks=chunks,
            session_id=self.session.id,
            agent_id=self.agent_id,
            llm_provider=llm_provider,
        )

        if result.original_count == 0:
            self.console.print(self.skin.viz.warning("Nothing to compress (not enough chunks)."))
            return

        # Delete old chunks and store compressed ones
        await context_manager.delete_chunks(self.session.id)

        # Store compressed chunks
        for chunk in result.compressed_chunks:
            await context_manager.add_chunk(
                session_id=chunk.session_id,
                agent_id=chunk.agent_id,
                chunk_type=chunk.chunk_type,
                payload=chunk.payload,
                token_count=chunk.token_count,
            )

        self.console.print(
            self.skin.viz.success(
                f"Context compressed: "
                f"{result.original_count} chunks → {len(result.compressed_chunks)} chunks, "
                f"{result.original_tokens} → {result.compressed_tokens} tokens "
                f"({result.compression_ratio:.1%} ratio, method: {result.method})"
            )
        )
        self.console.print()

    def _set_model(self, model: str) -> None:
        """Set the model."""
        if not model:
            self.console.print(f"Current model: {self.skin.viz.info(self.model)}")
            return
        self.model = model
        self.console.print(self.skin.viz.success(f"Model set to: {model}"))

    def _set_provider(self, provider: str) -> None:
        """Set the provider."""
        if not provider:
            self.console.print(f"Current provider: {self.skin.viz.info(self.provider)}")
            return
        if provider not in ("openrouter", "ollama"):
            self.console.print(self.skin.viz.error(f"Unknown provider: {provider}. Use 'openrouter' or 'ollama'."))
            return
        self.provider = provider
        self.console.print(self.skin.viz.success(f"Provider set to: {provider}"))

    def _set_budget(self, budget: str) -> None:
        """Set the context budget."""
        if not budget:
            self.console.print(f"Current context budget: {self.skin.viz.info(str(self.context_budget))}")
            return
        try:
            self.context_budget = int(budget)
            self.console.print(self.skin.viz.success(f"Context budget set to: {self.context_budget}"))
        except ValueError:
            self.console.print(self.skin.viz.error(f"Invalid budget: {budget}"))

    def _toggle_verbose(self) -> None:
        """Toggle verbose mode."""
        self.verbose = not self.verbose
        self.console.print(self.skin.viz.info(f"Verbose mode: {'on' if self.verbose else 'off'}"))

    def _set_skin(self, skin_name: str) -> None:
        """Set the color skin."""
        if not skin_name:
            self.console.print(f"Current skin: {self.skin.viz.info(self.skin.skin_name)}")
            self.console.print(f"Available skins: {', '.join(SkinConfig.available_skins())}")
            return
        if skin_name not in SkinConfig.SKINS:
            self.console.print(self.skin.viz.error(f"Unknown skin: {skin_name}"))
            self.console.print(f"Available skins: {', '.join(SkinConfig.available_skins())}")
            return
        self.skin.set_skin(skin_name)
        self.console.print(self.skin.viz.success(f"Skin set to: {skin_name}"))

    def _show_config(self) -> None:
        """Show current configuration."""
        table = self.skin.viz.table(style="standard", title="Configuration")
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="white")

        config_dict = config.to_dict()
        for key, value in config_dict.items():
            table.add_row(key, str(value))

        self.console.print(table)
        self.console.print()

    async def _handle_message(self, message: str) -> None:
        """Send a message to the agent and display the streaming response."""
        if not self.session:
            self.console.print(self.skin.viz.error("No active session. Use /new to create one."))
            return

        # Create provider and agent
        try:
            llm = get_provider(provider=self.provider, model=self.model)
        except ValueError as e:
            self.console.print(self.skin.viz.error(f"Provider error: {e}"))
            return

        agent = ReActAgent(
            provider=llm,
            max_iterations=config.get("max_iterations"),
            agent_id=self.agent_id,
        )

        # Stream response with animations
        response_text = ""
        tool_calls_count = 0
        tokens_used = 0

        try:
            # Create animation runner for this turn
            runner = get_animation_runner(self.console)

            # Start kawaii spinner
            self._kawaii_spinner = Spinner("dots", self.console, "Thinking...")
            self._kawaii_spinner.start()

            # Start streaming display
            stream = get_streaming_animation(self.console, style="green")
            runner.add_streaming("stream", stream)

            # Start the animations
            runner.start_all()

            async for event in agent.run_stream(
                self.session.id,
                message,
                verbose=self.verbose,
            ):
                if event.type == "text":
                    response_text += event.content
                    stream.add_token(event.content)
                elif event.type == "tool_call":
                    tool_calls_count += 1
                    if self.verbose:
                        # Show tool execution with inline spinner
                        tool_anim = get_tool_animation(self.console, event.tool_name)
                        tool_anim.start()
                        runner.add(f"tool_{tool_calls_count}", tool_anim)
                elif event.type == "tool_result":
                    if self.verbose:
                        # Complete tool animation
                        tool_key = f"tool_{tool_calls_count}"
                        if tool_key in runner._animations:
                            runner.complete(tool_key)
                        preview = str(event.tool_result)[:100].replace("\n", " ")
                        stream.add_token(f"\n  ← {preview}\n")
                elif event.type == "token_usage":
                    tokens_used = event.tokens_used
                elif event.type == "done":
                    response_text = event.response.content
                    tool_calls_count = len(event.response.tool_calls)
                    tokens_used = event.response.tokens_used
                    stream.set_text(response_text)

            # Stop all animations
            runner.stop_all()
            if self._kawaii_spinner:
                self._kawaii_spinner.stop()
                self._kawaii_spinner = None

            # Update token counter
            self._tokens_used = tokens_used

            # Final output in a beautiful Unicode box
            self.console.print()
            self._response_box.print(response_text)
            self.console.print()

            # Show metadata
            if self.verbose:
                meta_text = Text()
                meta_text.append("Iterations: ", style=self.skin.style("muted"))
                meta_text.append(f"{event.response.iterations}", style=self.skin.style("info"))
                meta_text.append(" | Tool calls: ", style=self.skin.style("muted"))
                meta_text.append(f"{tool_calls_count}", style=self.skin.style("warning"))
                meta_text.append(" | Tokens: ", style=self.skin.style("muted"))
                meta_text.append(f"{tokens_used}", style=self.skin.style("success"))
                self.console.print(meta_text)

            # Show session ID
            self.console.print(self.skin.viz.muted(f"Session ID: {self.session.id}"))
            self.console.print()

        except Exception as e:
            self.console.print(self.skin.viz.error(f"Agent error: {e}"))
            logger.exception("Agent run failed in REPL")


# ─── Entry point ─────────────────────────────────────────────────────────────


async def run_repl(
    model: str | None = None,
    provider: str | None = None,
    verbose: bool | None = None,
    session_id: str | None = None,
    skin: str = "default",
) -> None:
    """Convenience function to run the interactive REPL."""
    repl = InteractiveREPL(
        model=model,
        provider=provider,
        verbose=verbose,
        session_id=session_id,
        skin=skin,
    )
    await repl.run()
