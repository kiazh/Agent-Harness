"""Real SDK integration tests against the disposable local MCP fixture."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from machina.connectors.mcp_client import MCPConnection
from machina.connectors.models import (
    AuthRequired,
    ConfigError,
    ConnectionFailed,
    ConnectionState,
    ConnectorConfig,
    ConnectorToolError,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "support" / "mcp_fixture_server.py"


def _fixture_processes(name: str) -> set[int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True
    )
    marker = f"--name {name}"
    matches: set[int] = set()
    for line in result.stdout.splitlines():
        if marker not in line or str(FIXTURE) not in line:
            continue
        try:
            matches.add(int(line.strip().split(maxsplit=1)[0]))
        except (ValueError, IndexError):
            continue
    return matches


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_pid_exit(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return True
        await asyncio.sleep(0.025)
    return not _pid_is_alive(pid)


def _stdio_config(name: str, pidfile: Path, **kwargs) -> ConnectorConfig:
    return ConnectorConfig(
        id=name,
        command=sys.executable,
        argv=[str(FIXTURE), "--transport", "stdio", "--name", name, "--pidfile", str(pidfile)],
        cwd=str(ROOT),
        shutdown_timeout=2.0,
        **kwargs,
    ).validate()


@asynccontextmanager
async def _connected_stdio(tmp_path: Path, **kwargs):
    name = f"mcp-{uuid.uuid4().hex}"
    pidfile = tmp_path / f"{name}.pid"
    connection = MCPConnection(_stdio_config(name, pidfile, **kwargs))
    try:
        await connection.connect()
        yield connection
    finally:
        await connection.disconnect()
        if pidfile.exists():
            pid = int(pidfile.read_text(encoding="utf-8"))
            exited = await _wait_pid_exit(pid)
            if not exited:
                os.kill(pid, signal.SIGKILL)
            assert exited, "connection cleanup left a fixture process alive"
        assert not _fixture_processes(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect_in_new_task", [False, True], ids=["same-task", "cross-task"])
async def test_stdio_disconnect_cycles_retire_fixture_without_teardown_noise(
    tmp_path, caplog, disconnect_in_new_task
):
    test_name = f"mcp-{uuid.uuid4().hex}"
    pids: list[tuple[str, int]] = []
    try:
        for cycle in range(3):
            name = f"{test_name}-{cycle}"
            pidfile = tmp_path / f"fixture-{cycle}.pid"
            connection = MCPConnection(_stdio_config(name, pidfile))
            await connection.connect()
            deadline = time.monotonic() + 5.0
            while not pidfile.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert pidfile.exists(), "fixture did not publish its process id"
            pid = int(pidfile.read_text(encoding="utf-8"))
            pids.append((name, pid))

            # Gateway requests arrive in separate tasks; the same-task case
            # protects callers that deliberately keep the whole lifecycle local.
            if disconnect_in_new_task:
                await asyncio.create_task(connection.disconnect())
            else:
                await connection.disconnect()

            exited = await _wait_pid_exit(pid)
            remaining = _fixture_processes(name)
            assert exited, f"SDK shutdown returned but fixture PID {pid} is still alive"
            assert not remaining, f"fixture processes survived disconnect: {sorted(remaining)}"

        issues = [
            record.getMessage()
            for record in caplog.records
            if "shutdown issue" in record.getMessage()
        ]
        assert not issues, f"teardown emitted an error after SDK process shutdown: {issues}"
    finally:
        for _name, pid in pids:
            if _pid_is_alive(pid):
                os.kill(pid, signal.SIGKILL)


async def test_stdio_initialize_discover_and_call_round_trip(tmp_path):
    async with _connected_stdio(tmp_path) as connection:
        assert connection.state == ConnectionState.CONNECTED
        assert connection.server_name.startswith("fixture-mcp-")
        assert connection.protocol_version
        print(f"Negotiated stdio protocol: {connection.protocol_version}")
        tools, truncated = await asyncio.create_task(connection.list_tools())
        assert not truncated
        by_name = {tool.remote_name: tool for tool in tools}
        assert set(by_name) == {"echo", "slow", "big", "boom", "inject", "ask_user"}
        assert by_name["echo"].schema["properties"]["text"]["type"] == "string"
        outcome = await asyncio.create_task(connection.call_tool("echo", {"text": "wire proof"}))
        assert outcome.ok and outcome.text == "wire proof"
        assert not outcome.outcome_unknown


async def test_stdio_call_timeout_reports_unknown_outcome_without_retry(tmp_path):
    async with _connected_stdio(tmp_path, call_timeout=0.05) as connection:
        await connection.list_tools()
        outcome = await connection.call_tool("slow", {"seconds": 1.0})
        assert not outcome.ok and outcome.outcome_unknown
        assert "timed out" in outcome.error


async def test_large_result_is_bounded_and_marked(tmp_path):
    async with _connected_stdio(tmp_path, result_limit=128) as connection:
        await connection.list_tools()
        outcome = await connection.call_tool("big", {"size": 4096})
        assert outcome.ok and outcome.truncated
        assert "truncated" in outcome.text
        assert len(outcome.text.encode()) <= 128
        assert outcome.structured is None or len(str(outcome.structured).encode()) <= 128


async def test_server_tool_error_is_a_known_failure(tmp_path):
    async with _connected_stdio(tmp_path) as connection:
        await connection.list_tools()
        outcome = await connection.call_tool("boom", {})
        assert not outcome.ok and not outcome.outcome_unknown
        assert "boom" in outcome.error


async def test_server_elicitation_is_rejected_without_model_callback(tmp_path, monkeypatch):
    from machina.core.provider import DirectProvider

    async def forbidden_model(*_args, **_kwargs):
        pytest.fail("an MCP elicitation reached a model")

    monkeypatch.setattr(DirectProvider, "complete", forbidden_model)
    monkeypatch.setattr(DirectProvider, "stream_complete", forbidden_model)
    async with _connected_stdio(tmp_path) as connection:
        await connection.list_tools()
        outcome = await connection.call_tool("ask_user", {"question": "answer from a model?"})
        assert not outcome.ok and not outcome.outcome_unknown
        assert "Elicitation not supported" in outcome.error


@pytest.mark.parametrize("failure", ["binary", "cwd", "secret", "disabled"])
async def test_invalid_launch_is_refused_before_fixture_starts(tmp_path, failure):
    name = f"mcp-{uuid.uuid4().hex}"
    pidfile = tmp_path / "never.pid"
    config = _stdio_config(name, pidfile)
    expected = ConnectionFailed
    if failure == "binary":
        config.command = str(tmp_path / "no-such-binary")
    elif failure == "cwd":
        config.cwd = str(tmp_path / "missing-directory")
        expected = ConfigError
    elif failure == "secret":
        config.secret_refs = ["MCP_TEST_TOKEN"]
        expected = AuthRequired
    else:
        config.enabled = False
    connection = MCPConnection(config)
    with pytest.raises(expected):
        await connection.connect()
    await connection.disconnect()
    if failure == "secret":
        assert connection.state == ConnectionState.AUTH_REQUIRED
    assert not pidfile.exists()
    assert not _fixture_processes(name)


async def test_unseen_tools_and_invalid_arguments_never_dispatch(tmp_path):
    async with _connected_stdio(tmp_path) as connection:
        with pytest.raises(ConnectorToolError, match="discover first"):
            await connection.call_tool("echo", {"text": "x"})
        await connection.list_tools()
        with pytest.raises(ConnectorToolError):
            await connection.call_tool("echo", {"text": 42})


async def test_connection_public_metadata_does_not_contain_secret_values(tmp_path):
    secret = f"synthetic-secret-{uuid.uuid4().hex}"
    name = f"mcp-{uuid.uuid4().hex}"
    connection = MCPConnection(
        _stdio_config(name, tmp_path / "secret.pid", secret_refs=["MCP_FIXTURE_ERROR_SECRET"])
    )
    try:
        await connection.connect({"MCP_FIXTURE_ERROR_SECRET": secret})
        await connection.list_tools()
        outcome = await connection.call_tool("boom", {})
        assert not outcome.ok
        assert secret not in outcome.error
        assert secret not in outcome.text
        assert secret not in connection.read_stderr()
        assert secret not in str(connection.config.public_view())
        assert secret not in connection.last_error
        assert secret not in connection.server_name
    finally:
        await connection.disconnect()


async def test_loopback_http_initialize_discover_and_call_round_trip(tmp_path):
    name = f"mcp-{uuid.uuid4().hex}"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    pidfile = tmp_path / "http.pid"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(FIXTURE),
        "--transport",
        "http",
        "--port",
        str(port),
        "--name",
        name,
        "--pidfile",
        str(pidfile),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONNOUSERSITE": "1"},
    )
    connection = MCPConnection(
        ConnectorConfig(
            id=name,
            transport="streamable_http",
            endpoint=f"http://127.0.0.1:{port}/mcp",
            local_access=True,
        ).validate()
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            if process.returncode is not None:
                pytest.fail(
                    f"HTTP fixture exited during startup: {(await process.stderr.read()).decode()}"
                )
            try:
                _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                assert time.monotonic() < deadline, "HTTP fixture never bound its test port"
                await asyncio.sleep(0.025)
        await asyncio.create_task(connection.connect())
        print(f"Negotiated HTTP protocol: {connection.protocol_version}")
        tools, truncated = await asyncio.create_task(connection.list_tools())
        assert not truncated and any(tool.remote_name == "echo" for tool in tools)
        outcome = await asyncio.create_task(connection.call_tool("echo", {"text": "http proof"}))
        assert outcome.ok and outcome.text == "http proof"
    finally:
        await asyncio.create_task(connection.disconnect())
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill()
            await process.wait()
        assert not _pid_is_alive(process.pid)
        assert not _fixture_processes(name)


@pytest.mark.parametrize(
    "host",
    ["169.254.169.254", "100.100.100.100", "fd00:ec2::254", "169.254.4.8", "10.0.0.1", "127.0.0.1"],
)
async def test_http_rejects_unsafe_destinations_without_consent(host):
    rendered = f"[{host}]" if ":" in host else host
    connection = MCPConnection(
        ConnectorConfig(
            id="blocked",
            transport="streamable_http",
            endpoint=f"http://{rendered}/mcp",
        ).validate()
    )
    with pytest.raises(ConnectionFailed):
        await connection.connect()
    assert connection.state == ConnectionState.ERROR
    await connection.disconnect()


async def test_dropped_stdio_call_reports_unknown_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_FIXTURE_EXIT_ON_CALL", "1")
    async with _connected_stdio(tmp_path, env_allowlist=["MCP_FIXTURE_EXIT_ON_CALL"]) as connection:
        await connection.list_tools()
        outcome = await connection.call_tool("echo", {"text": "dispatch then drop"})
        assert not outcome.ok and outcome.outcome_unknown


def test_dns_alias_cannot_allow_cloud_metadata_even_with_local_consent(monkeypatch):
    from machina.connectors.mcp_client import _classify_host

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.100.100.100", 80))],
    )
    with pytest.raises(ConnectionFailed, match="metadata"):
        _classify_host("metadata.test", local_access=True)


async def test_http_credential_profiles_fail_closed_until_auth_is_implemented():
    connection = MCPConnection(
        ConnectorConfig(
            id="auth",
            transport="streamable_http",
            endpoint="http://127.0.0.1:1/mcp",
            local_access=True,
            credential_profile="fixture-profile",
        ).validate()
    )
    with pytest.raises(AuthRequired, match="not implemented"):
        await connection.connect({"fixture-profile": "synthetic-token"})
    assert connection.state == ConnectionState.AUTH_REQUIRED


async def test_discovery_deadline_covers_all_pages():
    from types import SimpleNamespace

    class EndlessCatalog:
        calls = 0

        async def list_tools(self, **_kwargs):
            await asyncio.sleep(0.015)
            self.calls += 1
            return SimpleNamespace(tools=[], next_cursor=str(self.calls))

    connection = MCPConnection(
        ConnectorConfig(id="pages", command=sys.executable, discovery_timeout=0.04).validate()
    )
    session = EndlessCatalog()
    connection._session = session
    try:
        with pytest.raises(ConnectionFailed, match="discovery timed out"):
            await asyncio.wait_for(connection.list_tools(), 0.3)
    finally:
        connection._session = None


async def test_shutdown_timeout_keeps_owner_tracked_until_cleanup_finishes(monkeypatch):
    connection = MCPConnection(
        ConnectorConfig(id="delayed", command=sys.executable, shutdown_timeout=0.02).validate()
    )
    release = asyncio.Event()
    cleanup_started = asyncio.Event()

    async def fake_transport(_secrets):
        async def cleanup():
            cleanup_started.set()
            await release.wait()

        connection._stack.push_async_callback(cleanup)
        connection._session = object()

    monkeypatch.setattr(connection, "_connect_stdio", fake_transport)
    await connection.connect()
    owner = connection._owner_task
    try:
        with pytest.raises(ConnectionFailed, match="shutdown timed out"):
            await asyncio.create_task(connection.disconnect())
        assert cleanup_started.is_set()
        assert connection._owner_task is owner and not owner.done()
        assert not owner.cancelling()
        with pytest.raises(ConnectionFailed, match="still retiring"):
            await connection.connect()
    finally:
        release.set()
        await connection.disconnect()
    assert owner.done() and connection._owner_task is None


@pytest.mark.asyncio
async def test_rotated_bearer_token_is_registered_before_request_and_never_replayed():
    import json
    from unittest.mock import AsyncMock

    import httpx2
    import mcp.types

    from machina.connectors.models import ConnectorConfig

    connection = MCPConnection(
        ConnectorConfig(
            id="rotating",
            transport="streamable_http",
            endpoint="https://resource.example/mcp",
            oauth={"client_mode": "auto"},
        )
    )
    initial, rotated = "initial-synthetic-token", 'rotated-synthetic-"token'
    identity = {
        "credentialKey": "synthetic-key",
        "resource": connection.config.endpoint,
        "issuer": "https://issuer.example",
        "clientId": "synthetic-client",
    }
    coordinator = AsyncMock()
    coordinator.auth_identity = lambda _id: identity
    coordinator.ensure_token.side_effect = [initial, rotated]
    auth, token = await connection._oauth_preflight(coordinator)
    connection.register_oauth_token(token)
    flow = auth.async_auth_flow(httpx2.Request("POST", connection.config.endpoint))
    request = await anext(flow)
    assert request.headers["Authorization"] == f"Bearer {rotated}"
    assert connection._redact(f"{initial} {rotated}") == "[REDACTED] [REDACTED]"
    assert json.loads(connection._redact(json.dumps({"error": rotated})))["error"] == "[REDACTED]"
    result = mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=f"{initial} {rotated}")],
        structured_content={"echo": rotated},
    )
    outcome = connection._normalize("echo", result, mcp.types)
    assert outcome.text == "[REDACTED] [REDACTED]"
    assert outcome.structured == {"echo": "[REDACTED]"}
    with pytest.raises(StopAsyncIteration):
        await flow.asend(httpx2.Response(401))
    assert coordinator.ensure_token.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["schema", "name"])
async def test_discovery_rejects_secret_echoes_in_dispatch_contract(field):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from machina.connectors.models import ConnectionFailed, ConnectorConfig

    token = "synthetic-catalog-token"
    connection = MCPConnection(
        ConnectorConfig(
            id="catalog",
            transport="streamable_http",
            endpoint="https://resource.example/mcp",
        )
    )
    connection.register_oauth_token(token)
    entry = SimpleNamespace(
        name=token if field == "name" else "echo",
        description=token,
        input_schema={"type": "object", "description": token}
        if field == "schema"
        else {"type": "object"},
    )
    connection._session = SimpleNamespace(
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=[entry], next_cursor=None))
    )
    with pytest.raises(ConnectionFailed) as error:
        await connection.list_tools()
    assert token not in str(error.value)
    assert connection._schemas == {}
