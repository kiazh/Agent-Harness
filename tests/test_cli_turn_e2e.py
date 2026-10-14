"""Round-4 CLI E2E proofs (R4-017 SIGINT cancel, R4-018 headless full grant).

Real ``machina chat`` subprocesses run against:

* a fake OpenAI-compatible provider at the network boundary (HTTP SSE on
  loopback), and
* the real test database (``MACHINA_TEST_DATABASE_URL`` — the ``machina_test``
  database only; the suite is skipped when it is not configured).

R4-017: SIGINT during a slow tool must cancel the running turn promptly,
release the session claim, kill the tool's process group, and exit 130.

R4-018: a saved ``full`` mode default must activate the same session-scoped
full grant the gateway/REST create paths activate, so a headless governed
tool call runs with zero approval pauses (pre-fix: exit 3).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DSN = os.environ.get("MACHINA_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DSN, reason="MACHINA_TEST_DATABASE_URL not set"),
    pytest.mark.skipif(os.name == "nt", reason="POSIX SIGINT/process-group semantics are required"),
]

# Bounded waits. Startup only polls (CI may import slowly); the cancellation
# bound is measured from the signal itself.
STARTUP_DEADLINE_S = 60.0
SIGINT_EXIT_DEADLINE_S = 10.0
SIGINT_CANCEL_BOUND_S = 3.0

_SESSION_RE = re.compile(
    r"New session: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

# Only the variables a CLI child needs; everything else (developer secrets,
# MACHINA_* overrides, DATABASE_URL) is scrubbed so the run is hermetic.
_ENV_PASSTHROUGH = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "TERM",
    "VIRTUAL_ENV",
)


class _FakeProvider:
    """Fake OpenAI-compatible provider: tool call first, final text after.

    The first request without a ``tool`` result in the conversation streams a
    single tool call; every later request streams the final text and a stop
    finish reason. Runs on loopback in a background thread.
    """

    def __init__(self, tool_name: str, tool_arguments: dict, final_text: str) -> None:
        self.tool_name = tool_name
        self.tool_arguments = tool_arguments
        self.final_text = final_text
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _FakeProvider:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def request_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def saw_tool_result(self) -> bool:
        with self._lock:
            return any(
                message.get("role") == "tool"
                for request in self.requests
                for message in (request.get("messages") or [])
            )

    def _make_handler(self):
        provider = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 — stdlib handler naming
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = {}
                with provider._lock:
                    provider.requests.append(body)
                    have_tool_result = any(
                        message.get("role") == "tool" for message in (body.get("messages") or [])
                    )
                chunks = (
                    provider._text_chunks() if have_tool_result else provider._tool_call_chunks()
                )
                payload = ("".join(chunks) + "data: [DONE]\n\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass  # keep pytest output clean

        return _Handler

    def _tool_call_chunks(self) -> list[str]:
        arguments = json.dumps(self.tool_arguments)
        first = {
            "model": "fake-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": self.tool_name, "arguments": arguments},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        }
        last = {
            "model": "fake-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        }
        return [f"data: {json.dumps(first)}\n\n", f"data: {json.dumps(last)}\n\n"]

    def _text_chunks(self) -> list[str]:
        first = {
            "model": "fake-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": self.final_text},
                    "finish_reason": None,
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
        }
        last = {
            "model": "fake-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        return [f"data: {json.dumps(first)}\n\n", f"data: {json.dumps(last)}\n\n"]


def _cli_env(tmp_path: Path, provider: _FakeProvider, mode: str) -> tuple[dict, Path]:
    """Hermetic child env: temp HOME/workspace, test DSN, loopback provider."""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key in _ENV_PASSTHROUGH}
    env.update(
        {
            "HOME": str(home),
            "MACHINA_HOME": str(workspace),
            "MACHINA_ENV_FILE": str(env_file),
            "MACHINA_DATABASE_URL": TEST_DSN,
            "DATABASE_URL": TEST_DSN,
            "MACHINA_EXECUTION_MODE": mode,
            "MACHINA_CUSTOM_BASE_URL": provider.base_url,
            "MACHINA_CUSTOM_API_KEY": "",
            "MACHINA_RATE_LIMIT_CALLS_PER_MINUTE": "1000",
            "NO_COLOR": "1",
            "TERM": "dumb",
            "PATH": os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")]),
        }
    )
    return env, workspace


def _spawn_chat(env: dict, message: str = "go") -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "machina",
            "chat",
            message,
            "--provider",
            "custom",
            "--model",
            "fake-model",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _kill_process_group(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_for_path(path: Path, deadline_s: float) -> bool:
    limit = time.monotonic() + deadline_s
    while time.monotonic() < limit:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _assert_process_gone(pid: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"orphaned tool process {pid} is still alive after the CLI exited")


async def _claim_state(session_id: str) -> dict | None:
    connection = await asyncpg.connect(TEST_DSN)
    try:
        row = await connection.fetchrow(
            "SELECT claim_owner, claim_kind, claim_expires_at FROM sessions WHERE id = $1",
            uuid.UUID(session_id),
        )
        return dict(row) if row else None
    finally:
        await connection.close()


def _session_id_from(output: str) -> str | None:
    match = _SESSION_RE.search(output)
    return match.group(1) if match else None


# ─── R4-017: SIGINT cancels the running turn ────────────────────────────────


def test_chat_sigint_cancels_slow_tool_turn_and_releases_claim(tmp_path: Path) -> None:
    provider = _FakeProvider(
        tool_name="terminal",
        tool_arguments={"command": "pytest slow_test.py", "timeout": 60},
        final_text="slow tool should never complete in this test",
    )
    slow_pid: int | None = None
    with provider:
        env, workspace = _cli_env(tmp_path, provider, mode="ask")
        marker = workspace / "started.marker"
        (workspace / "slow_test.py").write_text(
            "import os, time\n"
            "from pathlib import Path\n"
            "Path(__file__).with_name('started.marker').write_text(str(os.getpid()))\n"
            "\n"
            "def test_slow_started():\n"
            "    time.sleep(60)\n",
            encoding="utf-8",
        )
        proc = _spawn_chat(env)
        try:
            assert _wait_for_path(marker, STARTUP_DEADLINE_S), (
                "the slow tool never started (provider boundary or broker refused)"
            )
            slow_pid = int(marker.read_text(encoding="utf-8").strip() or 0)

            started = time.monotonic()
            proc.send_signal(signal.SIGINT)
            try:
                output, _ = proc.communicate(timeout=SIGINT_EXIT_DEADLINE_S)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc.pid)
                output, _ = proc.communicate(timeout=5)
                pytest.fail(
                    "machina chat kept running after SIGINT (deferred interrupt; "
                    f"the worker was not cancelled):\n{output}"
                )
            elapsed = time.monotonic() - started

            assert proc.returncode == 130, f"SIGINT must exit 130, got {proc.returncode}:\n{output}"
            assert elapsed < SIGINT_CANCEL_BOUND_S, (
                f"cancellation took {elapsed:.2f}s (bound {SIGINT_CANCEL_BOUND_S}s)"
            )
            session_id = _session_id_from(output)
            assert session_id, f"could not parse the session id from the output:\n{output}"
            state = asyncio.run(_claim_state(session_id))
            assert state is not None, "session row vanished"
            assert not state.get("claim_owner"), f"turn claim was not released: {state}"
        finally:
            if proc.poll() is None:
                _kill_process_group(proc.pid)
                proc.wait(timeout=5)
            _kill_process_group(slow_pid)
        # The terminal tool's cancellation path must have taken the whole
        # process group down (no orphaned pytest still sleeping).
        assert slow_pid, "missing slow-tool pid"
        _assert_process_gone(slow_pid)


# ─── R4-018: headless full mode activates the session grant ────────────────


def test_headless_full_mode_runs_routine_tool_without_pause(tmp_path: Path) -> None:
    provider = _FakeProvider(
        tool_name="read_file",
        tool_arguments={"path": "hello.txt"},
        final_text="HEADLESS FULL MODE COMPLETE",
    )
    with provider:
        env, workspace = _cli_env(tmp_path, provider, mode="full")
        (workspace / "hello.txt").write_text("FULL MODE FILE CONTENT\n", encoding="utf-8")
        proc = _spawn_chat(env)
        try:
            output, _ = proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                _kill_process_group(proc.pid)
                proc.wait(timeout=5)

        assert proc.returncode == 0, (
            f"saved full mode must run the routine tool; got exit {proc.returncode}:\n{output}"
        )
        assert provider.request_count() >= 2, "the tool result was never sent back to the provider"
        assert provider.saw_tool_result(), "the routine tool did not execute"
        assert "FULL MODE FILE CONTENT" in output, f"tool result missing from output:\n{output}"
        assert "HEADLESS FULL MODE COMPLETE" in output
        assert "paused for approval" not in output, output
        assert "Needs approval" not in output, output
        assert "FULL HOST mode active" in output, (
            f"activation notice missing from output:\n{output}"
        )
