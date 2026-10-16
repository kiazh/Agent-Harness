"""System now-playing feature handlers (the pinned music bar's data source).

Detection is blocking (ctypes + bounded osascript subprocesses), so handlers
run it off the event loop. Results are honest: `available` distinguishes "no
now-playing API" from "nothing is playing", and permission denials name the
app whose macOS Automation access is missing.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from machina.gateway.features._common import _str

if TYPE_CHECKING:
    from machina.gateway.server import Gateway

_ALLOWED_ACTIONS = ("toggle", "next", "previous")


async def nowplaying_get(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Read what is playing on this computer right now."""
    from machina.core.nowplaying import detect_now_playing

    return await asyncio.to_thread(detect_now_playing)


async def nowplaying_command(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Send a transport command (toggle/next/previous) to the detected source."""
    from machina.core.nowplaying import send_now_playing_command

    action = _str(params, "action", max_len=20).strip().lower()
    if action not in _ALLOWED_ACTIONS:
        return {"ok": False, "source": None, "error": f"unknown action {action!r}"}
    return await asyncio.to_thread(send_now_playing_command, action)
