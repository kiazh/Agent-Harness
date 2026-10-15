"""RED-first regression tests for holistic-review lane L5 (H20/H21).

H20 (P1): explicit compression with LLM summarization must resolve the
session's effective provider/model through the same precedence an ordinary
turn uses (session pin -> agent definition -> global config). Before the fix,
``context.compress`` handed the gateway's global defaults to
``services.compress_session`` as explicit overrides, so a session pinned to a
local/private route (Ollama, custom endpoint) had its transcript summarized
through the global default provider. The CLI path passed no override and fell
straight through to the global defaults (same defect, services.py side).

H21 (P2): the stdio dispatch loop awaits each ``handle_line`` before reading
the next request, so slow harness/delegation/discovery work blocks later
``prompt.cancel`` / ``approvals.resolve`` / ``mode.revoke`` / ``shutdown``
requests. Control requests must stay dispatchable while slow work runs.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio

from machina.core.models import LLMResponse


class _CapturingSummarizer:
    """Provider-boundary fake that records its own lifecycle."""

    model = "l5-capture-summarizer"

    def __init__(self, closed):
        self._closed = closed

    async def complete(self, **kwargs):
        return LLMResponse(content="SUMMARY", model=self.model, usage={"total_tokens": 1})

    async def close(self):
        self._closed.append(True)


def _capture_provider(monkeypatch, *namespaces):
    """Patch ``get_provider`` on every module that looked it up lazily."""
    routes: list[tuple] = []
    closed: list[bool] = []

    def _factory(*, provider=None, model=None, reasoning_effort=None):
        routes.append((provider, model))
        return _CapturingSummarizer(closed)

    for namespace in namespaces:
        monkeypatch.setattr(namespace, "get_provider", _factory)
    return routes, closed


@pytest_asyncio.fixture
async def live_db():
    """Connect the shared test DB (skip when MACHINA_TEST_DATABASE_URL is absent)."""
    import os

    if not os.environ.get("MACHINA_TEST_DATABASE_URL"):
        pytest.skip("MACHINA_TEST_DATABASE_URL not set")
    from machina.db.connection import db

    await db.connect()
    yield db
    await db.close()


# ---------------------------------------------------------------------------
# H20 — explicit compression keeps the session's pinned route
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("live_db")
async def test_explicit_compression_uses_pinned_session_route(monkeypatch):
    """gateway/features/sessions.py + services.py: a session pinned to Ollama
    must be summarized through Ollama, never through the gateway default."""
    from machina.core import provider as provider_module
    from machina.core.config import config
    from machina.core.context import context_manager
    from machina.core.session import session_manager
    from machina.gateway.features import sessions as sessions_feature

    routes, closed = _capture_provider(monkeypatch, provider_module)
    monkeypatch.setattr(config, "compression_llm_summarize", True)
    monkeypatch.setattr(config, "compression_preserve_recent", 1)

    session = await session_manager.create(
        title="l5-h20-gateway", model="llama3.1", provider="ollama"
    )

    class _GatewayStub:
        # Gateway/global defaults deliberately differ from the session pin.
        model = "openrouter/free"
        provider = "openrouter"

        def require_db(self):
            return None

        async def get_session(self, params):
            return await session_manager.get(params["sessionId"])

    try:
        for i in range(4):
            await context_manager.add_chunk(
                session.id,
                "harness",
                "user_message",
                {"content": f"private local transcript {i}"},
                token_count=20,
            )
        out = await sessions_feature.context_compress(
            _GatewayStub(), {"sessionId": str(session.id)}  # type: ignore[arg-type]
        )
        assert out["compressed"] is True
        assert routes == [("ollama", "llama3.1")]
        assert closed == [True]
    finally:
        from machina.db.connection import db

        await db.execute("DELETE FROM sessions WHERE id = $1", session.id)


def test_compress_services_route_matches_agent_factory_precedence(monkeypatch):
    """services.py: no overrides -> session pin, then agent definition, then
    global config — the same precedence ``build_agent_for_session`` applies."""
    import machina.core.provider as provider_module
    from machina import services as services_module
    from machina.core import context as context_module
    from machina.core.agent_def import AgentDef, agent_registry
    from machina.core.config import config
    from machina.core.models import ContextChunk

    routes, _closed = _capture_provider(monkeypatch, provider_module)
    monkeypatch.setattr(config, "compression_llm_summarize", True)
    monkeypatch.setattr(config, "compression_preserve_recent", 1)
    monkeypatch.setattr(config, "provider", "openrouter")
    monkeypatch.setattr(config, "model", "openrouter/free")

    chunks = [
        ContextChunk(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            agent_id="harness",
            chunk_type="user_message",
            payload={"content": f"msg {i}"},
            token_count=20,
        )
        for i in range(4)
    ]

    async def _all_chunks(session_id):
        return list(chunks)

    async def _replace(sid, ids, new_chunks, archive_reason="compressed", **kwargs):
        return len(new_chunks)

    monkeypatch.setattr(context_module.context_manager, "get_all_chunks", _all_chunks)
    monkeypatch.setattr(context_module.context_manager, "replace_chunks_by_ids", _replace)

    # 1. Session pin wins over the global default.
    pinned = SimpleNamespace(
        id=chunks[0].session_id, agent_id="harness", provider="ollama", model="llama3.1"
    )
    asyncio.run(services_module.compress_session(pinned))
    assert routes == [("ollama", "llama3.1")]

    # 2. No session pin: the agent definition carries the route.
    routes.clear()
    definition = AgentDef(name="researcher", model="def-model", provider="ollama")

    async def _definition(name):
        return definition

    monkeypatch.setattr(agent_registry, "get", _definition)
    unpinned = SimpleNamespace(
        id=chunks[0].session_id, agent_id="researcher", provider=None, model=None
    )
    asyncio.run(services_module.compress_session(unpinned))
    assert routes == [("ollama", "def-model")]


# ---------------------------------------------------------------------------
# H21 — control requests dispatch while a slow request is running
# ---------------------------------------------------------------------------


class _ScriptedGateway:
    """Fake Gateway for the stdio dispatch loop.

    ``interop.run`` blocks on ``release_slow`` until the test releases it;
    every request records its dispatch order. ``session.create`` yields
    briefly so the drain path can observe pending chained work.
    """

    instances: list[_ScriptedGateway] = []

    def __init__(self, write, *args, **kwargs):
        self.write = write
        self.closing = False
        self.dispatched: list[str] = []
        self.cancelled: list[str] = []
        self.slow_started = asyncio.Event()
        self.release_slow = asyncio.Event()
        self.slow_finished = False
        self.closed = False
        type(self).instances.append(self)

    async def handle_line(self, line: str) -> None:
        frame = json.loads(line)
        method = frame["method"]
        self.dispatched.append(method)
        try:
            if method == "interop.run":
                self.slow_started.set()
                await self.release_slow.wait()
                self.slow_finished = True
            elif method == "session.create":
                await asyncio.sleep(0.05)
            elif method == "shutdown":
                self.closing = True
        except asyncio.CancelledError:
            self.cancelled.append(method)
            raise
        self.write({"jsonrpc": "2.0", "id": frame.get("id"), "result": {}})

    async def close(self, timeout: float = 10.0) -> None:
        self.closed = True


def _seed_reader(lines: list[dict | str | None]):
    def reader(loop, queue):
        for line in lines:
            if line is None or isinstance(line, str):
                queue.put_nowait(line)
            else:
                queue.put_nowait(json.dumps(line))

    return reader


async def _start_serve(monkeypatch, lines, written=None):
    from machina.gateway import __main__ as gateway_main
    from machina.gateway import server as gateway_server

    monkeypatch.setenv("MACHINA_GATEWAY_TOKEN", "l5-token")
    _ScriptedGateway.instances.clear()
    monkeypatch.setattr(gateway_server, "Gateway", _ScriptedGateway)
    if written is not None:
        monkeypatch.setattr(gateway_main, "_write", written.append)
    monkeypatch.setattr(gateway_main, "_start_stdin_reader", _seed_reader(lines))
    serve = asyncio.create_task(gateway_main._serve())
    while not _ScriptedGateway.instances:  # let _serve construct the gateway
        await asyncio.sleep(0)
    return serve, _ScriptedGateway.instances[0]


_CONTROL_METHODS = ("prompt.cancel", "mode.revoke", "approvals.resolve", "shutdown")


async def test_control_requests_dispatch_during_slow_request(monkeypatch):
    """RED pre-fix: the serial loop dispatches prompt.cancel only after the
    slow interop.run finishes. Post-fix every control request runs while the
    slow request is still in flight."""
    serve, gateway = await _start_serve(
        monkeypatch,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "interop.run", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "prompt.cancel", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "mode.revoke", "params": {}},
            {"jsonrpc": "2.0", "id": 4, "method": "approvals.resolve", "params": {}},
            {"jsonrpc": "2.0", "id": 5, "method": "shutdown", "params": {}},
        ],
    )
    try:
        await asyncio.wait_for(gateway.slow_started.wait(), timeout=5)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not set(_CONTROL_METHODS) <= set(
            gateway.dispatched
        ):
            await asyncio.sleep(0.01)
        assert set(_CONTROL_METHODS) <= set(gateway.dispatched)
        # Control requests kept their relative order.
        assert [m for m in gateway.dispatched if m in _CONTROL_METHODS] == list(
            _CONTROL_METHODS
        )
        assert gateway.slow_finished is False  # slow request still in flight
        assert gateway.closing is True
    finally:
        gateway.release_slow.set()
    await asyncio.wait_for(serve, timeout=10)
    assert gateway.closed is True


async def test_pipelined_requests_drain_before_shutdown_closes(monkeypatch):
    """Orderly shutdown is preserved: requests already read still run to
    completion (no cancellation) and their responses are written."""
    written: list[dict] = []
    serve, gateway = await _start_serve(
        monkeypatch,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "session.create", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": {}},
        ],
        written=written,
    )
    await asyncio.wait_for(serve, timeout=10)
    assert gateway.closed is True
    assert gateway.cancelled == []
    # Chained (non-control) requests keep their arrival order; the control
    # shutdown may interleave with them by design.
    assert [m for m in gateway.dispatched if m != "shutdown"] == [
        "initialize",
        "session.create",
    ]
    assert {frame.get("id") for frame in written} == {1, 2, 3}
    assert all("result" in frame for frame in written)


async def test_eof_cancels_requests_that_outlast_the_drain(monkeypatch):
    """A request that ignores EOF past the drain bound is cancelled and joined
    instead of holding the process open."""
    from machina.gateway import __main__ as gateway_main

    # raising=False: the bound is new surface; pre-fix this test is RED for
    # the serial-loop behavior, not for a missing attribute.
    monkeypatch.setattr(gateway_main, "REQUEST_DRAIN_TIMEOUT_SECONDS", 0.05, raising=False)
    serve, gateway = await _start_serve(
        monkeypatch,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "interop.run", "params": {}},
            None,  # EOF
        ],
    )
    await asyncio.wait_for(gateway.slow_started.wait(), timeout=5)
    await asyncio.wait_for(serve, timeout=2)
    assert gateway.slow_finished is False
    assert gateway.cancelled == ["interop.run"]
    assert gateway.closed is True
