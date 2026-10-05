"""Launch the TypeScript terminal UI that lives in ``ui/``.

The UI is a Node.js program (built on ``@earendil-works/pi-tui``). It spawns
``python -m ah.gateway`` with the interpreter passed in ``AH_PYTHON`` and talks
to it over JSON-RPC on stdio.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from ah.cli import output

__all__ = ["MIN_NODE_VERSION", "launch_ui", "ui_dir"]

MIN_NODE_VERSION = (22, 19)


def ui_dir() -> Path:
    """Location of the UI package (override with ``AH_UI_DIR``)."""
    override = os.environ.get("AH_UI_DIR")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[2] / "ui"


def _node_version(node: str) -> tuple[int, ...] | None:
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
        return tuple(int(part) for part in out.stdout.strip().lstrip("v").split(".")[:2])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def launch_ui(
    *,
    model: str | None = None,
    provider: str | None = None,
    session_id: str | None = None,
) -> int:
    """Run the UI in the foreground and return its exit code."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        output.error(
            "The interactive UI needs a terminal.",
            'For scripts and pipes use one-shot mode: ah chat "your message"',
        )
        return 1

    directory = ui_dir()
    entry = directory / "src" / "main.ts"

    node = shutil.which("node")
    if node is None:
        output.error(
            "Node.js is not installed.",
            "Install Node.js 22.19 or newer from https://nodejs.org, then run `ah` again.",
        )
        return 1
    version = _node_version(node)
    if version is None or version < MIN_NODE_VERSION:
        found = ".".join(map(str, version)) if version else "unknown"
        wanted = ".".join(map(str, MIN_NODE_VERSION))
        output.error(f"Node.js {wanted}+ is required (found {found}).")
        return 1
    if not entry.exists():
        output.error(
            f"UI not found at {directory}.",
            "Set AH_UI_DIR to the ui/ folder of an AgentHarness checkout.",
        )
        return 1
    if not (directory / "node_modules" / "@earendil-works" / "pi-tui").exists():
        output.error(
            "UI dependencies are not installed.",
            f'Run: npm install --ignore-scripts --prefix "{directory}"',
        )
        return 1

    args = [node, "--disable-warning=ExperimentalWarning", str(entry)]
    if model:
        args += ["--model", model]
    if provider:
        args += ["--provider", provider]
    if session_id:
        args += ["--session", session_id]

    # Generate a random auth token for the gateway. The UI client reads this
    # from AH_GATEWAY_TOKEN and includes it in the initialize call.
    gateway_token = secrets.token_hex(32)
    env = {**os.environ, "AH_PYTHON": sys.executable, "AH_GATEWAY_TOKEN": gateway_token}
    try:
        return subprocess.call(args, env=env)
    except KeyboardInterrupt:
        return 130
