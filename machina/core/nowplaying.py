"""System now-playing detection (macOS) — layered sources.

The pinned music bar shows whatever is playing on this computer. macOS has no
single public API for that, so this module tries layers in order and returns
the first honest answer:

1. **MediaRemote** (private framework, ctypes): universal when the OS allows
   it (title/artist/album/elapsed/duration/playback state + transport
   commands). Silent-empty on macOS builds that gate it for third parties.
2. **nowplaying-cli / media-control** binaries, when installed: they bundle
   the entitlement-carrying adapter for newer macOS.
3. **Spotify / Music** via AppleScript (only while actually running): full
   state, position, and transport control. The first query triggers the
   normal macOS Automation prompt; while it is unanswered the layer times
   out and an explicit denial is detected from osascript's stderr (-1743 /
   "Not authorized") — both report the named, cached `permission` state.
4. **Chromium / Safari front-window YouTube tab** via AppleScript: title and
   source are real; playback state is unknowable through this route and is
   reported as `unknown` rather than guessed.

Honesty rules: every layer is bounded (short subprocess timeouts), a timeout
or denial never fabricates data, `available` distinguishes "no now-playing
API" from "nothing is playing", and transport commands report whether the
source accepted them.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from machina.core.metrics import metrics

__all__ = [
    "detect_now_playing",
    "send_now_playing_command",
    "describe_sources",
    "NowPlayingUnavailable",
]

OSASCRIPT_TIMEOUT_S = 3.0
_CLI_TIMEOUT_S = 2.0
_CACHE_TTL_S = 0.75
# A pending macOS Automation prompt can soak the whole budget; cache the
# permission result briefly so polling never re-triggers dialogs or waits.
_PERMISSION_CACHE_TTL_S = 20.0
_LINE = "\x1f"  # ASCII unit separator: never appears in titles

# Chromium-family apps share the same scripting dictionary shape.
_CHROMIUM_APPS = (
    "Google Chrome",
    "Brave Browser",
    "Microsoft Edge",
    "Chromium",
    "Vivaldi",
    "Opera",
)

_RUN_LOCK = threading.Lock()
_cache: dict[str, Any] | None = None
_cache_at = 0.0

# MediaRemote callbacks are delivered asynchronously and the framework copies
# the block (raw invoke pointer included), so a callback can fire after the
# probing call returns. The thunks, blocks, and result slots therefore live at
# module level for the whole process — a late dispatch can never touch freed
# ctypes memory. Slots carry a monotonic stamp: a probe only accepts writes
# made after it started, so a late reply from the previous poll can never
# contaminate the next reading.
_mr_probe_lock = threading.Lock()
_mr_state_lock = threading.Lock()
_mr_last: dict[str, tuple[float, Any]] = {}


def _mr_record(key: str, value: Any) -> None:
    with _mr_state_lock:
        _mr_last[key] = (time.monotonic(), value)


def _mr_read(started: float) -> tuple[Any, Any, Any]:
    """Values recorded at/after *started* (stale late callbacks are ignored)."""
    with _mr_state_lock:

        def get(key: str) -> Any:
            entry = _mr_last.get(key)
            if entry is not None and entry[0] >= started:
                return entry[1]
            return None

        return get("info"), get("playing"), get("pid")


class NowPlayingUnavailable(RuntimeError):
    """The platform has no usable now-playing detection."""


def _run(command: list[str], timeout: float) -> tuple[int, str, str]:
    """Run *command* with a hard timeout; never raises."""
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as error:  # pragma: no cover - defensive
        return 1, "", f"{type(error).__name__}: {error}"


def _osascript(script: str, timeout: float = OSASCRIPT_TIMEOUT_S) -> tuple[int, str, str]:
    return _run(["osascript", "-e", script], timeout)


def _permission_denied(stderr: str) -> bool:
    """True when osascript reported an explicit Automation denial.

    A denied permission exits non-zero with errAEEventNotPermitted (-1743) or
    "Not authorized"; an unanswered Automation prompt still times out (exit
    124). Both are permission states, not "nothing playing".
    """
    text = (stderr or "").lower()
    return "-1743" in text or "not authorized" in text


def _osascript_error(code: int, stderr: str, app: str) -> dict[str, Any] | None:
    """Map an osascript failure to the named permission marker, else None.

    Only timeouts and explicit denials become the permission state (named by
    *app* and cached by ``detect_now_playing``); every other non-zero exit
    stays a plain "no reading" so a broken script never pins an app as
    blocked.
    """
    if code == 124:
        metrics.increment_counter("nowplaying.applescript_timeout")
        return {"error": "permission", "source": app}
    if code != 0 and _permission_denied(stderr):
        metrics.increment_counter("nowplaying.applescript_denied")
        return {"error": "permission", "source": app}
    return None


def _app_running(name: str) -> bool:
    """Cheap process check first: never launch an app and never prompt for one
    that is not running (AppleScript `tell` would launch it)."""
    code, out, _ = _run(["pgrep", "-x", name], 0.75)
    return code == 0 and bool(out.strip())


# ─── layer 1: MediaRemote (ctypes) ──────────────────────────────────────────

_CF_UTF8 = 0x08000100
_media_remote_lock = threading.Lock()
_media_remote_state: dict[str, Any] | None = None


def _load_media_remote() -> dict[str, Any] | None:
    """Load the private framework once; None when unavailable."""
    global _media_remote_state
    with _media_remote_lock:
        if _media_remote_state is not None:
            return _media_remote_state if _media_remote_state.get("ok") else None
        state: dict[str, Any] = {"ok": None}
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("System") or "/usr/lib/libSystem.B.dylib")

            class BlockDescriptor(ctypes.Structure):
                _fields_ = [("reserved", ctypes.c_ulong), ("size", ctypes.c_ulong)]

            class Block(ctypes.Structure):
                _fields_ = [
                    ("isa", ctypes.c_void_p),
                    ("flags", ctypes.c_int),
                    ("reserved", ctypes.c_int),
                    ("invoke", ctypes.c_void_p),
                    ("descriptor", ctypes.POINTER(BlockDescriptor)),
                ]

            isa_array = (ctypes.c_void_p * 32).in_dll(libc, "_NSConcreteStackBlock")
            isa = ctypes.cast(isa_array, ctypes.c_void_p)

            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            cf.CFStringCreateWithCString.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_uint32,
            ]
            cf.CFDictionaryGetValue.restype = ctypes.c_void_p
            cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            cf.CFStringGetCString.restype = ctypes.c_bool
            cf.CFStringGetCString.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_long,
                ctypes.c_uint32,
            ]
            cf.CFNumberGetValue.restype = ctypes.c_bool
            cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            cf.CFRelease.argtypes = [ctypes.c_void_p]

            remote = ctypes.CDLL(
                "/System/Library/PrivateFrameworks/MediaRemote.framework/MediaRemote"
            )
            remote.MRMediaRemoteGetNowPlayingInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            remote.MRMediaRemoteGetNowPlayingInfo.restype = None
            remote.MRMediaRemoteGetNowPlayingApplicationIsPlaying.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            remote.MRMediaRemoteGetNowPlayingApplicationIsPlaying.restype = None
            remote.MRMediaRemoteGetNowPlayingApplicationPID.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            remote.MRMediaRemoteGetNowPlayingApplicationPID.restype = None
            remote.MRMediaRemoteSendCommand.argtypes = [ctypes.c_int, ctypes.c_void_p]
            remote.MRMediaRemoteSendCommand.restype = ctypes.c_bool
            libdispatch = ctypes.CDLL(
                ctypes.util.find_library("System") or "/usr/lib/libSystem.B.dylib"
            )
            libdispatch.dispatch_queue_create.restype = ctypes.c_void_p
            libdispatch.dispatch_queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
            queue = libdispatch.dispatch_queue_create(b"machina.nowplaying", None)

            # One block/thunk set for the process lifetime (see _mr_last note).
            descriptor = BlockDescriptor(reserved=0, size=ctypes.sizeof(Block))
            keepalive: list[Any] = []

            def make_block(invoke: Callable[..., None]) -> Any:
                descriptor_ref = ctypes.pointer(descriptor)
                block = Block(
                    isa=isa,
                    flags=0,
                    reserved=0,
                    invoke=ctypes.cast(invoke, ctypes.c_void_p),
                    descriptor=descriptor_ref,
                )
                keepalive.extend((invoke, block, descriptor_ref))
                return block

            def on_info(_block: int, info: int) -> None:
                if not info:
                    _mr_record("info", None)
                    return
                found: dict[str, Any] = {}
                for label, key in _MR_KEYS.items():
                    key_ref = _mr_function_string(cf, key)
                    value_ref = cf.CFDictionaryGetValue(info, key_ref)
                    cf.CFRelease(key_ref)
                    found[label] = (
                        _mr_string_value(cf, value_ref)
                        if label in ("title", "artist", "album")
                        else _mr_number_value(cf, value_ref)
                    )
                _mr_record("info", found)

            def on_playing(_block: int, playing: int) -> None:
                _mr_record("playing", bool(playing))

            def on_pid(_block: int, pid: int) -> None:
                _mr_record("pid", int(pid))

            invoke_info = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)(on_info)
            invoke_playing = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int)(on_playing)
            invoke_pid = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int)(on_pid)
            block_info = make_block(invoke_info)
            block_playing = make_block(invoke_playing)
            block_pid = make_block(invoke_pid)
        except Exception as error:  # framework/binaries missing
            state["reason"] = f"{type(error).__name__}"
            _media_remote_state = state
            return None
        state.update(
            {
                "ok": True,
                "cf": cf,
                "remote": remote,
                "isa": isa,
                "queue": queue,
                "Block": Block,
                "BlockDescriptor": BlockDescriptor,
                "block_info": block_info,
                "block_playing": block_playing,
                "block_pid": block_pid,
                "keepalive": keepalive,
            }
        )
        _media_remote_state = state
        return state


_MR_KEYS = {
    "title": "kMRMediaRemoteNowPlayingInfoTitle",
    "artist": "kMRMediaRemoteNowPlayingInfoArtist",
    "album": "kMRMediaRemoteNowPlayingInfoAlbum",
    "duration": "kMRMediaRemoteNowPlayingInfoDuration",
    "elapsed": "kMRMediaRemoteNowPlayingInfoElapsedTime",
    "rate": "kMRMediaRemoteNowPlayingInfoPlaybackRate",
}


def _mr_function_string(cf: Any, text: str) -> int:
    return cf.CFStringCreateWithCString(None, text.encode("utf-8"), _CF_UTF8)


def _mr_string_value(cf: Any, ref: int | None) -> str | None:
    if not ref:
        return None
    buf = ctypes.create_string_buffer(4096)
    if cf.CFStringGetCString(ref, buf, len(buf), _CF_UTF8):
        return buf.value.decode("utf-8", "replace")
    return None


def _mr_number_value(cf: Any, ref: int | None) -> float | None:
    if not ref:
        return None
    value = ctypes.c_double(0)
    if cf.CFNumberGetValue(ref, 6, ctypes.byref(value)):  # kCFNumberFloat64Type
        return value.value
    return None


def _media_remote_probe() -> dict[str, Any] | None:
    """Return the raw MediaRemote reading, or None when the layer is unusable.

    An empty reading (no info dictionary while macOS gates third parties) is
    returned as ``{"info": None}`` so callers can continue to the next layer
    without pretending the API was absent.
    """
    state = _load_media_remote()
    if state is None:
        return None
    remote = state["remote"]
    queue = state["queue"]
    with _mr_probe_lock:
        started = time.monotonic()
        with _mr_state_lock:
            _mr_last.clear()
        try:
            remote.MRMediaRemoteGetNowPlayingInfo(queue, ctypes.byref(state["block_info"]))
            remote.MRMediaRemoteGetNowPlayingApplicationIsPlaying(
                queue, ctypes.byref(state["block_playing"])
            )
            remote.MRMediaRemoteGetNowPlayingApplicationPID(queue, ctypes.byref(state["block_pid"]))
            deadline = started + 1.5
            while time.monotonic() < deadline:
                with _mr_state_lock:
                    entry = _mr_last.get("info")
                if entry is not None and entry[0] >= started:
                    break
                time.sleep(0.03)
        except Exception:
            return None
        info, playing, pid = _mr_read(started)
    return {"info": info, "playing": playing, "pid": pid}


def _process_name(pid: int | None) -> str | None:
    if not pid or pid <= 0:
        return None
    code, out, _ = _run(["ps", "-p", str(pid), "-o", "comm="], 1.0)
    if code != 0 or not out.strip():
        return None
    path = out.strip()
    name = path.rsplit("/", 1)[-1]
    friendly = {
        "Google Chrome": "Google Chrome",
        "brave": "Brave Browser",
        "Microsoft Edge": "Microsoft Edge",
        "Safari": "Safari",
        "Spotify": "Spotify",
        "Music": "Music",
        "QuickTime Player": "QuickTime Player",
        "VLC": "VLC",
    }
    return friendly.get(name, name)


# ─── layer 2: nowplaying-cli / media-control ────────────────────────────────


def _cli_binary() -> tuple[str, str] | None:
    for name, flavor in (("nowplaying-cli", "nowplaying-cli"), ("media-control", "media-control")):
        path = shutil.which(name)
        if path:
            return path, flavor
    return None


def _cli_probe() -> dict[str, Any] | None:
    found = _cli_binary()
    if found is None:
        return None
    path, flavor = found
    if flavor == "nowplaying-cli":
        code, out, _ = _run(
            [path, "get", "title", "artist", "album", "duration", "elapsedTime", "playbackRate"],
            _CLI_TIMEOUT_S,
        )
        if code != 0 or not out.strip():
            return None
        values = out.splitlines()

        # nowplaying-cli prints "null" for absent fields.
        def val(index: int) -> str | None:
            if index >= len(values):
                return None
            raw = values[index].strip()
            return None if raw in ("", "null") else raw

        def num(index: int) -> float | None:
            raw = val(index)
            try:
                return float(raw) if raw is not None else None
            except ValueError:
                return None

        rate = num(5)
        return {
            "source": "now playing (CLI)",
            "title": val(0),
            "artist": val(1),
            "album": val(2),
            "duration": num(3),
            "elapsed": num(4),
            "state": ("playing" if rate and rate > 0 else "paused")
            if rate is not None
            else "unknown",
        }
    code, out, _ = _run([path, "get"], _CLI_TIMEOUT_S)
    if code != 0 or not out.strip():
        return None
    try:
        payload = json.loads(out)
    except ValueError:
        return None
    return {
        "source": "now playing (CLI)",
        "title": payload.get("title"),
        "artist": payload.get("artist"),
        "album": payload.get("album"),
        "duration": payload.get("duration"),
        "elapsed": payload.get("elapsedTime"),
        "state": "playing" if payload.get("playing") else "paused",
    }


# ─── layer 3: Spotify / Music AppleScript ───────────────────────────────────

_MEDIA_APP_SCRIPT = """
if application "{app}" is running then
    tell application "{app}"
        if player state is stopped then return "STOPPED"
        return (player state as text) & "{sep}" & (name of current track) & "{sep}" & (artist of current track) & "{sep}" & (album of current track) & "{sep}" & ((duration of current track) as text) & "{sep}" & ((player position) as text)
    end tell
end if
return "NOT_RUNNING"
"""


def _media_app_probe(app: str, timeout: float = OSASCRIPT_TIMEOUT_S) -> dict[str, Any] | None:
    if not _app_running(app):
        return None
    script = _MEDIA_APP_SCRIPT.format(app=app, sep=_LINE)
    code, out, err = _osascript(script, timeout)
    permission = _osascript_error(code, err, app)
    if permission is not None:
        return permission
    if code != 0:
        return None
    raw = out.strip()
    if raw in ("NOT_RUNNING", "STOPPED", ""):
        return None
    parts = raw.split(_LINE)
    if len(parts) < 6:
        return None
    state, title, artist, album, duration, position = parts[:6]
    try:
        # Spotify reports duration in milliseconds; Music in seconds. Normalize:
        duration_s = float(duration) / (1000.0 if app == "Spotify" else 1.0)
    except ValueError:
        duration_s = None
    try:
        elapsed_s = float(position)
    except ValueError:
        elapsed_s = None
    return {
        "source": app,
        "title": title or None,
        "artist": artist or None,
        "album": album or None,
        "duration": duration_s,
        "elapsed": elapsed_s,
        "state": "playing" if state == "playing" else "paused",
    }


# ─── layer 4: front-window YouTube tab (Chromium family + Safari) ───────────

_BROWSER_SCRIPT = """
if application "{app}" is running then
    tell application "{app}"
        if (count of windows) is 0 then return "NO_WINDOW"
        return ({title_expr}) & "{sep}" & ({url_expr})
    end tell
end if
return "NOT_RUNNING"
"""

_YOUTUBE_SUFFIXES = (" - YouTube Music", " - YouTube")


def _browser_probe(
    app: str, title_expr: str, url_expr: str, timeout: float = OSASCRIPT_TIMEOUT_S
) -> dict[str, Any] | None:
    if app != "Safari" and not _app_running(app):
        return None
    if app == "Safari" and not _app_running("Safari"):
        return None
    script = _BROWSER_SCRIPT.format(app=app, sep=_LINE, title_expr=title_expr, url_expr=url_expr)
    code, out, err = _osascript(script, timeout)
    permission = _osascript_error(code, err, app)
    if permission is not None:
        return permission
    if code != 0:
        return None
    raw = out.strip()
    if raw in ("NOT_RUNNING", "NO_WINDOW", ""):
        return None
    parts = raw.split(_LINE)
    if len(parts) < 2:
        return None
    title, url = parts[0].strip(), parts[1].strip()
    if "youtube.com/watch" not in url and "youtu.be/" not in url:
        return None
    display = title
    for suffix in _YOUTUBE_SUFFIXES:
        if display.endswith(suffix):
            display = display[: -len(suffix)]
            break
    display = display.removesuffix(" ▶").strip()
    artist = None
    # YouTube Music titles are commonly "Song • Artist".
    if " • " in display:
        song, _, maybe_artist = display.partition(" • ")
        display, artist = song.strip(), maybe_artist.strip() or None
    if not display:
        return None
    return {
        "source": f"{app} (front tab)",
        "title": display,
        "artist": artist,
        "album": None,
        "duration": None,
        "elapsed": None,
        # Chromium/Safari scripting cannot distinguish a paused tab from a
        # playing one; report unknown instead of guessing.
        "state": "unknown",
    }


def _browser_sources() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for app in _CHROMIUM_APPS:
        entries.append(
            {
                "app": app,
                "title_expr": "title of active tab of front window",
                "url_expr": "URL of active tab of front window",
            }
        )
    entries.append(
        {
            "app": "Safari",
            "title_expr": "name of current tab of front window",
            "url_expr": "URL of current tab of front window",
        }
    )
    return entries


# ─── public API ─────────────────────────────────────────────────────────────


def _empty(error: str | None = None) -> dict[str, Any]:
    return {
        "available": error is None,
        "state": None,
        "title": None,
        "artist": None,
        "album": None,
        "source": None,
        "elapsed": None,
        "duration": None,
        "error": error,
    }


def _detect() -> dict[str, Any]:
    if sys.platform != "darwin":
        return _empty("system now-playing requires macOS")

    permission_denied: str | None = None
    applescript_budget = time.monotonic() + 4.0

    # Layer 1: MediaRemote
    raw = _media_remote_probe()
    if raw is not None:
        info = raw.get("info")
        if info and (info.get("title") or info.get("artist")):
            rate = info.get("rate")
            playing = raw.get("playing")
            state = (
                "playing"
                if (playing is True)
                else "paused"
                if (playing is False or (rate is not None and rate == 0))
                else "unknown"
            )
            return {
                "available": True,
                "state": state,
                "title": info.get("title"),
                "artist": info.get("artist"),
                "album": info.get("album"),
                "source": _process_name(raw.get("pid")) or "system audio",
                "elapsed": info.get("elapsed"),
                "duration": info.get("duration"),
                "error": None,
            }

    # Layer 2: CLI adapters
    cli = _cli_probe()
    if cli is not None:
        if cli.get("title"):
            return {
                "available": True,
                "state": cli.get("state", "unknown"),
                "title": cli.get("title"),
                "artist": cli.get("artist"),
                "album": cli.get("album"),
                "source": cli.get("source"),
                "elapsed": cli.get("elapsed"),
                "duration": cli.get("duration"),
                "error": None,
            }

    # Layer 3: Spotify / Music
    for app in ("Spotify", "Music"):
        remaining = applescript_budget - time.monotonic()
        if remaining <= 0.2:
            break
        probe = _media_app_probe(app, min(OSASCRIPT_TIMEOUT_S, remaining))
        if probe is None:
            continue
        if probe.get("error") == "permission":
            permission_denied = permission_denied or app
            continue
        if probe.get("title"):
            return {
                "available": True,
                "state": probe.get("state", "unknown"),
                "title": probe.get("title"),
                "artist": probe.get("artist"),
                "album": probe.get("album"),
                "source": probe["source"],
                "elapsed": probe.get("elapsed"),
                "duration": probe.get("duration"),
                "error": None,
            }

    # Layer 4: front-window YouTube tab
    for entry in _browser_sources():
        remaining = applescript_budget - time.monotonic()
        if remaining <= 0.2:
            break
        probe = _browser_probe(
            entry["app"],
            entry["title_expr"],
            entry["url_expr"],
            min(OSASCRIPT_TIMEOUT_S, remaining),
        )
        if probe is None:
            continue
        if probe.get("error") == "permission":
            permission_denied = permission_denied or entry["app"]
            continue
        if probe.get("title"):
            return {
                "available": True,
                "state": probe.get("state", "unknown"),
                "title": probe.get("title"),
                "artist": probe.get("artist"),
                "album": None,
                "source": probe["source"],
                "elapsed": None,
                "duration": None,
                "error": None,
            }

    if permission_denied:
        return _empty(
            f"macOS blocked automation for {permission_denied} — allow it in "
            "System Settings → Privacy & Security → Automation"
        )
    # Detectors are operational; nothing is playing right now. `available`
    # stays true so the UI can render the honest empty line — an unavailable
    # API is a different state (non-macOS, or the permission case above).
    return _empty(None)


def _cache_ttl(result: dict[str, Any]) -> float:
    error = result.get("error") or ""
    if "blocked automation" in error:
        return _PERMISSION_CACHE_TTL_S
    return _CACHE_TTL_S


def detect_now_playing(*, cache: bool = True) -> dict[str, Any]:
    """Best available now-playing reading; cached briefly for polling."""
    global _cache, _cache_at
    with _RUN_LOCK:
        now = time.monotonic()
        if cache and _cache is not None and now - _cache_at < _cache_ttl(_cache):
            return dict(_cache)
        result = _detect()
        _cache = dict(result)
        _cache_at = now
        return result


# ─── transport commands ─────────────────────────────────────────────────────

_MR_COMMANDS = {"toggle": 2, "next": 4, "previous": 5}


def send_now_playing_command(action: str) -> dict[str, Any]:
    """Send a transport command to the detected source; never fabricates.

    Returns ``{"ok": bool, "source": str | None, "error": str | None}``. The
    caller re-reads state afterwards; `ok` only means the source accepted the
    command, not that the UI should guess the new state.
    """
    if sys.platform != "darwin":
        return {"ok": False, "source": None, "error": "system now-playing requires macOS"}
    if action not in _MR_COMMANDS:
        return {"ok": False, "source": None, "error": f"unknown action {action!r}"}
    current = detect_now_playing(cache=False)
    source = current.get("source") or ""

    # Prefer the source app's own scripting where we detected it.
    if source == "Spotify" or source == "Music":
        verb = {"toggle": "playpause", "next": "next track", "previous": "previous track"}[action]
        code, _, _ = _osascript(f'tell application "{source}" to {verb}')
        return {
            "ok": code == 0,
            "source": source,
            "error": None if code == 0 else "command rejected",
        }

    # CLI adapters understand the same transport verbs.
    found = _cli_binary()
    if found is not None:
        path, flavor = found
        verb = {"toggle": "togglePlay", "next": "nextTrack", "previous": "previousTrack"}[action]
        code, _, _ = _run([path, verb], _CLI_TIMEOUT_S)
        if code == 0:
            return {"ok": True, "source": "now playing (CLI)", "error": None}

    state = _load_media_remote()
    if state is not None:
        try:
            accepted = bool(state["remote"].MRMediaRemoteSendCommand(_MR_COMMANDS[action], None))
        except Exception:
            accepted = False
        if accepted:
            return {"ok": True, "source": source or "system audio", "error": None}
    return {
        "ok": False,
        "source": source or None,
        "error": "this source does not accept transport commands"
        if source
        else "nothing is playing",
    }


def describe_sources() -> dict[str, Any]:
    """Diagnostics: which layers are usable on this machine."""
    return {
        "platform": sys.platform,
        "mediaremote": _load_media_remote() is not None,
        "cli": _cli_binary()[0] if _cli_binary() else None,
        "apps": ["Spotify", "Music"],
        "browsers": [*list(_CHROMIUM_APPS), "Safari"],
    }
