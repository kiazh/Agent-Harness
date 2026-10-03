"""Light-mode and no-color detection."""
from __future__ import annotations

import os
import re
import sys
from typing import Optional

from ah.cli.visual.colors import parse_color, relative_luminance, rgb_to_hex

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



