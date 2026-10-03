"""Tests for the JSON-RPC gateway (ah.gateway)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from ah.core.models import AgentResponse, ContextChunk, StreamEvent
from ah.gateway.server import (
    DATABASE_UNAVAILABLE,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SESSION_NOT_FOUND,
    TURN_IN_PROGRESS,
    Gateway,
    history_from_chunks,
)

TEST_DSN = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not TEST_DSN, reason="AGENT_HARNESS_TEST_DATABASE_URL not set")


# ─── helpers ──────────────────────────────────────────────────────────────────


class FakeAgent:
    """Stands in for ReActAgent: replays scripted stream events."""

    def __init__(
        self,
        events: list[StreamEvent],
        gate: asyncio.Event | None = None,
        error: Exception | None = None,
    ):
        self.events = events
        self.gate = gate
        self.error = error

    async def run_stream(self, session_id, user_message, verbose=True):
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        for event in self.events:
            yield event


def scripted_turn() -> list[StreamEvent]:
    return [
        StreamEvent(type="text", content="Looking "),
        StreamEvent(type="tool_call", tool_name="read_file", tool_args={"path": "a.py"}),
        StreamEvent(type="tool_result", tool_name="read_file", tool_result="print('hi')"),
        StreamEvent(type="token_usage", tokens_used=42),
        StreamEvent(type="text", content="done."),
        StreamEvent(
            type="done",
            response=AgentResponse(
                content="Looking done.",
                tool_calls=[{"tool": "read_file"}],
                tokens_used=42,
                iterations=2,
            ),
        ),
    ]


class Harness:
    """Drives a Gateway in-process and records every frame it writes."""

    def __init__(self, agent: FakeAgent | None = None) -> None:
        os.environ["AH_GATEWAY_NO_SCHEDULER"] = "1"  # tests drive the runner directly
        self.frames: list[dict] = []
        self.agent = agent or FakeAgent(scripted_turn())
        self.gateway = Gateway(self.frames.append, agent_factory=lambda model, provider: self.agent)
        self._next_id = 0

    async def call(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        rid = self._next_id
        frame = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            frame["params"] = params
        await self.gateway.handle_line(json.dumps(frame))
        return next(f for f in self.frames if f.get("id") == rid and "method" not in f)

    def events(self, event_type: str | None = None) -> list[dict]:
        return [
            f["params"]
            for f in self.frames
            if f.get("method") == "event"
            and (event_type is None or f["params"]["type"] == event_type)
        ]

    async def wait_for(self, event_type: str, timeout: float = 5.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            found = self.events(event_type)
            if found:
                return found[-1]
            await asyncio.sleep(0.01)
        raise AssertionError(f"no {event_type!r} event within {timeout}s; got {self.events()}")


# ─── protocol errors (no database needed) ─────────────────────────────────────


class TestProtocolErrors:
    async def test_parse_error(self):
        h = Harness()
        await h.gateway.handle_line("{not json")
        assert h.frames[-1]["error"]["code"] == PARSE_ERROR
        assert h.frames[-1]["id"] is None

    async def test_invalid_request(self):
        h = Harness()
        await h.gateway.handle_line(
            json.dumps({"id": 1, "method": "initialize"})
        )  # no jsonrpc field
        assert h.frames[-1]["error"]["code"] == INVALID_REQUEST

    async def test_unknown_method(self):
        h = Harness()
        response = await h.call("nope")
        assert response["error"]["code"] == METHOD_NOT_FOUND

    async def test_params_must_be_object(self):
        h = Harness()
        await h.gateway.handle_line(
            json.dumps({"jsonrpc": "2.0", "id": 7, "method": "session.list", "params": [1]})
        )
        assert h.frames[-1]["error"]["code"] == INVALID_PARAMS

    async def test_session_methods_require_initialize(self):
        h = Harness()
        response = await h.call("session.list")
        assert response["error"]["code"] == DATABASE_UNAVAILABLE

    async def test_config_set_validates(self):
        h = Harness()
        assert (await h.call("config.set", {"key": "provider", "value": "nope"}))["error"][
            "code"
        ] == INVALID_PARAMS
        assert (await h.call("config.set", {"key": "no_such_key", "value": "x"}))["error"][
            "code"
        ] == INVALID_PARAMS
        secret = await h.call("config.set", {"key": "openrouter_api_key", "value": "sk-x"})
        assert secret["error"]["code"] == INVALID_PARAMS
        bad_type = await h.call("config.set", {"key": "context_budget", "value": "lots"})
        assert bad_type["error"]["code"] == INVALID_PARAMS
        ok = await h.call("config.set", {"key": "model", "value": "openai/gpt-4o"})
        assert ok["result"]["model"] == "openai/gpt-4o"
        ok = await h.call("config.set", {"key": "provider", "value": "ollama"})
        assert ok["result"]["provider"] == "ollama"

    async def test_cancel_without_turn(self):
        h = Harness()
        response = await h.call("prompt.cancel", {"sessionId": str(uuid.uuid4())})
        assert response["result"] == {"cancelled": False}

    async def test_shutdown_sets_closing(self):
        h = Harness()
        assert (await h.call("shutdown"))["result"] == {}
        assert h.gateway.closing


def test_history_from_chunks_is_chronological():
    sid = uuid.uuid4()

    def chunk(kind, payload):
        return ContextChunk(
            id=uuid.uuid4(), session_id=sid, agent_id="h", chunk_type=kind, payload=payload
        )

    newest_first = [
        chunk("assistant_message", {"content": "answer"}),
        chunk("tool_call", {"tool": "read_file", "result_preview": "x = 1"}),
        chunk("user_message", {"content": "question"}),
        chunk("memory", {"content": "ignored"}),
    ]
    assert history_from_chunks(newest_first) == [
        {"role": "user", "content": "question"},
        {"role": "tool", "tool": "read_file", "content": "x = 1"},
        {"role": "assistant", "content": "answer"},
    ]


# ─── sessions and turns (real test database) ──────────────────────────────────


@needs_db
class TestSessionsAndTurns:
    async def test_initialize_and_session_lifecycle(self):
        h = Harness()
        try:
            init = await h.call("initialize", {"model": "test/model"})
            assert init["result"]["model"] == "test/model"
            assert init["result"]["version"]

            created = (await h.call("session.create", {"title": "gateway test"}))["result"][
                "session"
            ]
            assert created["title"] == "gateway test"
            assert created["model"] == "test/model"

            listed = (await h.call("session.list", {"limit": 50}))["result"]["sessions"]
            assert created["id"] in {s["id"] for s in listed}

            resumed = (await h.call("session.resume", {"sessionId": created["id"]}))["result"]
            assert resumed["session"]["id"] == created["id"]
            assert resumed["history"] == []
        finally:
            await h.gateway.close()

    async def test_resume_unknown_session(self):
        h = Harness()
        try:
            await h.call("initialize")
            response = await h.call("session.resume", {"sessionId": str(uuid.uuid4())})
            assert response["error"]["code"] == SESSION_NOT_FOUND
            bad = await h.call("session.resume", {"sessionId": "not-a-uuid"})
            assert bad["error"]["code"] == INVALID_PARAMS
        finally:
            await h.gateway.close()

    async def test_prompt_streams_events_in_order(self):
        h = Harness()
        try:
            await h.call("initialize")
            sid = (await h.call("session.create"))["result"]["session"]["id"]
            turn_id = (await h.call("prompt.submit", {"sessionId": sid, "text": "read a.py"}))[
                "result"
            ]["turnId"]

            complete = await h.wait_for("message.complete")
            assert complete["text"] == "Looking done."
            assert (
                complete["tokens"] == 42
                and complete["iterations"] == 2
                and complete["toolCalls"] == 1
            )
            assert complete["cancelled"] is False

            types = [e["type"] for e in h.events()]
            assert types == [
                "message.start",
                "message.delta",
                "tool.start",
                "tool.complete",
                "usage",
                "message.delta",
                "message.complete",
            ]
            assert all(e["sessionId"] == sid and e["turnId"] == turn_id for e in h.events())

            start, done = h.events("tool.start")[0], h.events("tool.complete")[0]
            assert start["id"] == done["id"]
            assert start["name"] == "read_file" and start["args"] == {"path": "a.py"}
            assert done["result"] == "print('hi')" and done["isError"] is False
        finally:
            await h.gateway.close()

    async def test_second_prompt_while_running_is_rejected(self):
        gate = asyncio.Event()
        h = Harness(FakeAgent(scripted_turn(), gate=gate))
        try:
            await h.call("initialize")
            sid = (await h.call("session.create"))["result"]["session"]["id"]
            assert "result" in await h.call("prompt.submit", {"sessionId": sid, "text": "one"})
            busy = await h.call("prompt.submit", {"sessionId": sid, "text": "two"})
            assert busy["error"]["code"] == TURN_IN_PROGRESS
            gate.set()
            await h.wait_for("message.complete")
        finally:
            await h.gateway.close()

    async def test_cancel_running_turn(self):
        gate = asyncio.Event()  # never set: the turn blocks until cancelled
        h = Harness(FakeAgent(scripted_turn(), gate=gate))
        try:
            await h.call("initialize")
            sid = (await h.call("session.create"))["result"]["session"]["id"]
            await h.call("prompt.submit", {"sessionId": sid, "text": "slow"})
            await asyncio.sleep(0)
            assert (await h.call("prompt.cancel", {"sessionId": sid}))["result"] == {
                "cancelled": True
            }
            complete = await h.wait_for("message.complete")
            assert complete["cancelled"] is True
        finally:
            await h.gateway.close()

    async def test_agent_failure_reports_error_then_completes(self):
        h = Harness(FakeAgent([], error=RuntimeError("model exploded")))
        try:
            await h.call("initialize")
            sid = (await h.call("session.create"))["result"]["session"]["id"]
            await h.call("prompt.submit", {"sessionId": sid, "text": "go"})
            await h.wait_for("message.complete")
            assert "model exploded" in h.events("error")[0]["message"]
        finally:
            await h.gateway.close()

    async def test_empty_prompt_rejected(self):
        h = Harness()
        try:
            await h.call("initialize")
            sid = (await h.call("session.create"))["result"]["session"]["id"]
            response = await h.call("prompt.submit", {"sessionId": sid, "text": "   "})
            assert response["error"]["code"] == INVALID_PARAMS
        finally:
            await h.gateway.close()


@needs_db
def test_gateway_process_speaks_clean_json_over_stdio():
    """`python -m ah.gateway` answers requests and writes nothing but JSON to stdout."""
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "session.create", "params": {"title": "e2e"}},
        {"jsonrpc": "2.0", "id": 3, "method": "shutdown"},
    ]
    env = {**os.environ, "DATABASE_URL": TEST_DSN, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "ah.gateway"],
        input="".join(json.dumps(r) + "\n" for r in requests),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=90,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    frames = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    by_id = {f["id"]: f for f in frames}
    assert set(by_id) == {1, 2, 3}, frames
    assert "error" not in by_id[1], by_id[1]
    assert by_id[2]["result"]["session"]["title"] == "e2e"
    assert by_id[3]["result"] == {}
