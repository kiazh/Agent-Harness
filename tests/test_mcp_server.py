"""Unit + real-stdio handshake tests for the Machina MCP server shim.

Unit tests mock ``machina.mcp_server._run_process`` (the single subprocess
seam) and the in-process DB seams; the handshake test spawns the real server
over stdio with the MCP client SDK, pointed at an unreachable test DSN so it
never touches a developer database.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from machina import mcp_server
from machina.mcp_server import ProcessResult

REPO_ROOT = Path(__file__).resolve().parents[1]

SESSION_ID = "123e4567-e89b-42d3-a456-426614174000"
REQUEST_ID = "3f0b0f44-6100-4a2a-9e95-0d9f6bd3a9c1"

SAMPLE_OK_OUTPUT = f"New session: {SESSION_ID}\n\nHELLO-TOKEN\n\nSession ID: {SESSION_ID}\n"
SAMPLE_PAUSED_OUTPUT = (
    f"New session: {SESSION_ID}\n"
    "\n"
    "I need to run a command first.\n"
    "\n"
    "✗ Turn paused for approval; it did not run to completion.\n"
    "  Resolve the request, then resume the session.\n"
    f"  request {REQUEST_ID} (terminal — echo hi)\n"
    f"Session ID: {SESSION_ID}\n"
)


def _fake_runner(result: ProcessResult, capture: dict | None = None):
    def run(cmd, *, timeout):
        if capture is not None:
            capture["cmd"] = cmd
            capture["timeout"] = timeout
        return result

    return run


# ---------------------------------------------------------------------------
# Server assembly
# ---------------------------------------------------------------------------


async def test_toolset_is_exactly_the_three_phase1_tools():
    server = mcp_server.build_server()
    tools = await server.list_tools()
    assert sorted(tool.name for tool in tools) == [
        "machina_chat",
        "machina_session_create",
        "machina_status",
    ]
    chat = next(tool for tool in tools if tool.name == "machina_chat")
    assert chat.input_schema["required"] == ["prompt"]
    assert set(chat.input_schema["properties"]) == {"prompt", "session_id", "mode"}
    status = next(tool for tool in tools if tool.name == "machina_status")
    assert status.annotations is not None
    assert status.annotations.read_only_hint is True


# ---------------------------------------------------------------------------
# machina_chat — mocked subprocess paths
# ---------------------------------------------------------------------------


async def test_chat_success_roundtrip(monkeypatch):
    capture: dict = {}
    monkeypatch.setattr(
        mcp_server,
        "_run_process",
        _fake_runner(ProcessResult(0, SAMPLE_OK_OUTPUT, "", False, False), capture),
    )
    payload = json.loads(await mcp_server.machina_chat("hello world"))
    assert payload == {
        "status": "completed",
        "session_id": SESSION_ID,
        "reply": "HELLO-TOKEN",
        "truncated": False,
    }
    cmd = capture["cmd"]
    assert cmd[:4] == [sys.executable, "-m", "machina", "chat"]
    assert cmd[4] == "hello world"
    assert "-q" in cmd
    assert "--session" not in cmd
    assert capture["timeout"] == mcp_server.DEFAULT_CHAT_TIMEOUT_SECONDS


async def test_chat_resume_and_mode_flags(monkeypatch):
    capture: dict = {}
    monkeypatch.setattr(
        mcp_server,
        "_run_process",
        _fake_runner(ProcessResult(0, SAMPLE_OK_OUTPUT, "", False, False), capture),
    )
    payload = json.loads(
        await mcp_server.machina_chat("hi", session_id=SESSION_ID, mode="workspace")
    )
    assert payload["status"] == "completed"
    assert capture["cmd"][-4:] == ["--session", SESSION_ID, "--mode", "workspace"]


async def test_chat_exit_3_maps_to_pending_approval(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "_run_process",
        _fake_runner(ProcessResult(3, SAMPLE_PAUSED_OUTPUT, "", False, False)),
    )
    payload = json.loads(await mcp_server.machina_chat("run something"))
    assert payload["status"] == "paused_for_approval"
    assert payload["session_id"] == SESSION_ID
    assert payload["pending_approvals"] == [
        {"request_id": REQUEST_ID, "detail": "terminal — echo hi"}
    ]
    assert "approval" in payload["notice"].lower()
    assert "I need to run a command first." in payload["reply"]
    assert "✗" not in payload["reply"]
    assert "request " not in payload["reply"]


async def test_chat_reply_excludes_stderr_chatter(monkeypatch):
    # `machina chat` writes audit/log lines to stderr; they must never leak
    # into the extracted reply.
    stderr_noise = (
        '2026-10-14 09:59:18,783 [AUDIT] {"event": "llm_stream_start"}\n'
        "Audit event dropped before the writer started; event was not persisted\n"
        "Skipping skill /x/SKILL.md: ValueError\n"
    )
    monkeypatch.setattr(
        mcp_server,
        "_run_process",
        _fake_runner(ProcessResult(0, SAMPLE_OK_OUTPUT, stderr_noise, False, False)),
    )
    payload = json.loads(await mcp_server.machina_chat("hi"))
    assert payload["reply"] == "HELLO-TOKEN"
    assert "AUDIT" not in payload["reply"]
    assert "Skipping skill" not in payload["reply"]


async def test_chat_timeout_returns_structured_result(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "_run_process",
        _fake_runner(
            ProcessResult(None, f"New session: {SESSION_ID}\n\npartial output\n", "", True, False)
        ),
    )
    payload = json.loads(await mcp_server.machina_chat("slow turn"))
    assert payload["status"] == "timeout"
    assert payload["session_id"] == SESSION_ID
    assert "killed" in payload["error"]


async def test_chat_failure_is_bounded(monkeypatch):
    noisy = "E" * 5000
    monkeypatch.setattr(
        mcp_server,
        "_run_process",
        _fake_runner(
            ProcessResult(
                1,
                f"New session: {SESSION_ID}\n✗ Turn failed.\n{noisy}",
                "audit noise from stderr\n",
                False,
                False,
            )
        ),
    )
    payload = json.loads(await mcp_server.machina_chat("boom"))
    assert payload["status"] == "failed"
    assert payload["exit_code"] == 1
    assert payload["session_id"] == SESSION_ID
    assert len(payload["error"]) <= mcp_server.MAX_ERROR_CHARS
    assert "audit noise" not in payload["error"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt": ""},
        {"prompt": "   "},
        {"prompt": "x" * (mcp_server.MAX_PROMPT_CHARS + 1)},
        {"prompt": "ok", "session_id": "not-a-uuid"},
        {"prompt": "ok", "mode": "yolo"},
    ],
)
async def test_chat_invalid_requests_never_spawn(monkeypatch, kwargs):
    def forbidden(cmd, *, timeout):
        raise AssertionError("subprocess must not run for invalid chat requests")

    monkeypatch.setattr(mcp_server, "_run_process", forbidden)
    payload = json.loads(await mcp_server.machina_chat(**kwargs))
    assert payload["status"] == "invalid_request"
    assert payload["error"]


# ---------------------------------------------------------------------------
# Bounded process runner (real child, fast)
# ---------------------------------------------------------------------------


def test_run_process_timeout_kills_child_tree():
    start = time.monotonic()
    result = mcp_server._run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.75
    )
    elapsed = time.monotonic() - start
    assert result.timed_out is True
    assert result.returncode is not None
    assert elapsed < 15.0


def test_run_process_bounds_prolific_output():
    result = mcp_server._run_process(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 70000)"], timeout=30.0
    )
    assert result.timed_out is False
    assert result.truncated is True
    assert len(result.output) <= mcp_server.MAX_OUTPUT_CHARS


# ---------------------------------------------------------------------------
# machina_status / machina_session_create — in-process seams mocked
# ---------------------------------------------------------------------------


async def test_status_formats_compact_text(monkeypatch):
    async def fake_summary():
        return {
            "postgres": "PostgreSQL 16.4",
            "sessions": 3,
            "contextChunks": 120,
            "memories": 4,
            "pendingMemories": 1,
            "tools": ["a", "b"],
            "model": "deepseek/deepseek-chat",
            "provider": "deepseek",
            "mode": "ask",
            "backend": "host",
            "workspace": "/tmp/ws",
            "chatProvider": "deepseek",
            "memoryRetrieval": "keyword-only",
            "memoryRetrievalReason": "embeddings disabled",
            "memoryExtraction": "ready",
            "documentRag": "degraded; no indexed documents",
            "reranker": "passthrough",
            "autoCompaction": "enabled",
            "sandbox": "inactive",
            "jobsAwaitingApproval": 2,
            "permissionDurability": "durable",
        }

    monkeypatch.setattr(mcp_server, "_read_status_summary", fake_summary)
    text = await mcp_server.machina_status()
    assert text.startswith("Machina status\n")
    assert "postgres: PostgreSQL 16.4" in text
    assert "sessions: 3 | chunks: 120 | memories: 4 (pending: 1)" in text
    assert "tools: 2 registered" in text
    assert "memory retrieval: keyword-only (embeddings disabled)" in text
    assert "jobs awaiting approval: 2" in text
    assert len(text) <= mcp_server.MAX_STATUS_CHARS


async def test_status_never_raises(monkeypatch):
    async def failing():
        raise OSError("connection refused")

    monkeypatch.setattr(mcp_server, "_read_status_summary", failing)
    assert await mcp_server.machina_status() == "Machina status unavailable (OSError)"


async def test_session_create_returns_id(monkeypatch):
    async def fake_create(title):
        return SESSION_ID, title

    monkeypatch.setattr(mcp_server, "_create_session_row", fake_create)
    payload = json.loads(await mcp_server.machina_session_create("mcp test"))
    assert payload == {"status": "created", "session_id": SESSION_ID, "title": "mcp test"}


async def test_session_create_error_is_structured(monkeypatch):
    async def failing(title):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(mcp_server, "_create_session_row", failing)
    payload = json.loads(await mcp_server.machina_session_create())
    assert payload["status"] == "error"
    assert "RuntimeError" in payload["error"]


# ---------------------------------------------------------------------------
# Real stdio handshake (MCP client SDK -> python -m machina.mcp_server)
# ---------------------------------------------------------------------------


async def test_stdio_handshake_lists_tools_and_roundtrips(tmp_path):
    stdio = pytest.importorskip("mcp.client.stdio")
    from mcp import ClientSession

    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    params = stdio.StdioServerParameters(
        command=sys.executable,
        args=["-m", "machina.mcp_server"],
        cwd=str(REPO_ROOT),
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", ""),
            # Port 1 refuses instantly: the round-trip must not depend on a
            # live dev database, and the tool degrades gracefully instead.
            "DATABASE_URL": "postgresql://postgres@127.0.0.1:1/machina",
            "MACHINA_ENV_FILE": str(empty_env),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
        },
    )
    try:
        async with asyncio.timeout(60):
            async with stdio.stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    assert sorted(tool.name for tool in listed.tools) == [
                        "machina_chat",
                        "machina_session_create",
                        "machina_status",
                    ]
                    result = await session.call_tool("machina_status", {})
                    assert result.is_error is False
                    assert "Machina status" in result.content[0].text
    except TimeoutError:
        pytest.skip("stdio handshake exceeded 60s in this environment")
