"""R4-083 gateway standalone token disclosure — E2E standalone handshake.

``python -m machina.gateway`` must make its minted token usable: no launcher
injects ``MACHINA_GATEWAY_TOKEN``, so the token is disclosed on stderr. The
proof drives a real subprocess against the real machina_test database:
read the disclosed token, run the handshake, call a real method with the
token, and confirm the wrong token stays rejected.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DSN = os.environ.get("MACHINA_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="MACHINA_TEST_DATABASE_URL not set")

TOKEN_PREFIX = "gateway auth token: "


class _Stdio:
    """Line pumps for a child's stdout/stderr so neither pipe can deadlock."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc
        self.stdout: queue.Queue[str | None] = queue.Queue()
        self.stderr: queue.Queue[str | None] = queue.Queue()
        for stream, sink in ((proc.stdout, self.stdout), (proc.stderr, self.stderr)):
            threading.Thread(target=self._pump, args=(stream, sink), daemon=True).start()

    @staticmethod
    def _pump(stream: Any, sink: queue.Queue[str | None]) -> None:
        try:
            for line in iter(stream.readline, ""):
                sink.put(line)
        finally:
            sink.put(None)

    def send(self, frame: dict[str, Any]) -> dict[str, Any]:
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()
        line = self.stdout.get(timeout=60)
        if line is None:
            raise AssertionError("gateway stdout closed before a frame arrived")
        return json.loads(line)

    def wait_stderr(self, needle: str, timeout: float = 60.0) -> str:
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while time.monotonic() < deadline:
            try:
                line = self.stderr.get(timeout=deadline - time.monotonic())
            except queue.Empty:
                break
            if line is None:
                break
            seen.append(line)
            if needle in line:
                return line
        raise AssertionError(f"stderr never carried {needle!r}; saw: {''.join(seen)[-2000:]}")


def test_standalone_gateway_discloses_its_token_and_serves_methods(tmp_path):
    from machina.gateway.errors import UNAUTHORIZED

    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": TEST_DSN,
        "MACHINA_ENV_FILE": str(tmp_path / ".env"),
        "MACHINA_GATEWAY_NO_SCHEDULER": "1",
    }
    assert "MACHINA_GATEWAY_TOKEN" not in env
    proc = subprocess.Popen(
        [sys.executable, "-m", "machina.gateway"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=ROOT,
    )
    io = _Stdio(proc)
    try:
        line = io.wait_stderr(TOKEN_PREFIX)
        token = line.split(TOKEN_PREFIX, 1)[1].strip().split(" ", 1)[0]
        assert len(token) == 64
        int(token, 16)  # a secrets.token_hex value, not a placeholder

        init = io.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1, "token": token},
            }
        )
        assert "error" not in init, init
        assert init["result"]["protocolVersion"] == 1

        listed = io.send(
            {"jsonrpc": "2.0", "id": 2, "method": "providers.list", "params": {"token": token}}
        )
        assert "error" not in listed, listed
        assert isinstance(listed["result"]["providers"], list)

        rejected = io.send(
            {"jsonrpc": "2.0", "id": 3, "method": "providers.list", "params": {"token": "0" * 64}}
        )
        assert rejected["error"]["code"] == UNAUTHORIZED, rejected

        shutdown = io.send(
            {"jsonrpc": "2.0", "id": 4, "method": "shutdown", "params": {"token": token}}
        )
        assert shutdown["result"] == {}
        proc.wait(timeout=30)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
