"""Regressions for failures found at the runtime boundaries."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from ah.core.agent import BaseReActAgent
from ah.core.assembler import PromptAssembler, get_token_count
from ah.core.models import LLMResponse
from ah.core.provider import AsyncTokenBucket


@pytest.mark.asyncio
async def test_tool_allowlist_is_enforced_at_execution_and_ollama_arguments_work(monkeypatch):
    from ah.core.context import context_manager
    from ah.tools.base import registry

    execute = AsyncMock(return_value="read")
    monkeypatch.setattr(registry, "execute", execute)
    monkeypatch.setattr(context_manager, "add_chunk", AsyncMock())
    agent = BaseReActAgent.__new__(BaseReActAgent)
    agent.allowed_tools = ["read_file"]
    agent.agent_id = "researcher"
    response = LLMResponse(
        content="",
        model="test",
        tool_calls=[
            {"id": "denied", "function": {"name": "write_file", "arguments": "{}"}},
            {"id": "allowed", "function": {"name": "read_file", "arguments": {"path": "a"}}},
        ],
    )
    messages: list[dict] = []
    events = [
        event
        async for event in agent._execute_tool_calls_stream(response, messages, [], uuid.uuid4())
    ]

    execute.assert_awaited_once_with("read_file", path="a")
    assert "not allowed" in events[1].tool_result
    assert events[-1].tool_result == "read"


@pytest.mark.asyncio
async def test_stream_does_not_retry_after_a_visible_delta():
    class Provider:
        def __init__(self):
            self.calls = 0

        async def stream_complete(self, **_kwargs):
            from ah.core.models import StreamEvent

            self.calls += 1
            yield StreamEvent(type="text", content="partial")
            raise RuntimeError("stream interrupted")

    agent = BaseReActAgent.__new__(BaseReActAgent)
    agent.provider = Provider()
    received = []
    with pytest.raises(RuntimeError, match="interrupted"):
        async for event in agent._stream_llm_with_retry([], []):
            received.append(event.content)
    assert received == ["partial"]
    assert agent.provider.calls == 1


def test_prompt_budget_and_document_text():
    assembler = PromptAssembler(20)
    prompt = assembler.assemble("system" * 200, None, [], [], "query")
    assert get_token_count(prompt) <= 20
    document = assembler._compress_chunk(
        {"type": "document", "payload": {"text": "A" * 300, "source": "notes.md"}}
    )
    assert "A" * 300 in document
    assert "notes.md" in document


@pytest.mark.asyncio
async def test_rag_cache_respects_result_count_and_dense_mode():
    from ah.rag.pipeline import RAGConfig, RAGPipeline
    from ah.rag.search import SearchResult

    class Embedder:
        async def embed(self, _text):
            return [1.0]

    class Search:
        def __init__(self):
            self.hybrid_calls = 0
            self.dense_calls = 0

        async def search(self, **kwargs):
            self.hybrid_calls += 1
            return [
                SearchResult(chunk=SimpleNamespace(payload={"text": "x"}), score=1.0)
                for _ in range(kwargs["top_k"])
            ]

        async def search_dense(self, **kwargs):
            self.dense_calls += 1
            return await self.search(**kwargs)

    search = Search()
    pipeline = RAGPipeline(embedder=Embedder(), search=search)
    session_id = uuid.uuid4()
    assert len(await pipeline.search("same", session_id, top_k=1, rerank=False)) == 1
    assert len(await pipeline.search("same", session_id, top_k=3, rerank=False)) == 3
    assert search.hybrid_calls == 2

    dense = Search()
    dense_pipeline = RAGPipeline(
        embedder=Embedder(), search=dense, config=RAGConfig(enable_hybrid_search=False)
    )
    await dense_pipeline.search("same", session_id, top_k=2, rerank=False)
    assert dense.dense_calls == 1


@pytest.mark.asyncio
async def test_http_rpc_uses_app_database_without_closing_it(monkeypatch):
    from ah.api.app import _RpcGateway
    from ah.core.session import session_manager
    from ah.db.connection import db

    monkeypatch.setattr(db, "_pool", object())
    close = AsyncMock()
    monkeypatch.setattr(db, "close", close)
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(session_manager, "list_sessions", listing)
    gateway = _RpcGateway()
    assert await gateway.call("session.list", {"limit": 5}) == {"sessions": []}
    await gateway.close()
    close.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_store_filters_search_and_count_by_session(monkeypatch):
    from ah.db.connection import db
    from ah.memory.store import MemoryStore

    session_id = uuid.uuid4()
    fetch = AsyncMock(return_value=[])
    fetchval = AsyncMock(return_value=2)
    monkeypatch.setattr(db, "fetch", fetch)
    monkeypatch.setattr(db, "fetchval", fetchval)
    store = MemoryStore()
    assert await store.search(session_id=session_id, limit=7) == []
    assert "session_id = $1" in fetch.await_args.args[0]
    assert fetch.await_args.args[1:] == (session_id, 7, 0)
    assert await store.count(session_id=session_id) == 2
    assert fetchval.await_args.args[1:] == (session_id,)


@pytest.mark.asyncio
async def test_gateway_session_cursor_reaches_older_sessions(monkeypatch):
    from ah.core.session import session_manager
    from ah.gateway.server import Gateway

    sessions = [
        SimpleNamespace(
            id=uuid.uuid4(),
            title=str(index),
            model="m",
            provider="ollama",
            status="active",
            last_activity=None,
        )
        for index in range(3)
    ]

    async def list_page(*, limit, offset):
        return sessions[offset : offset + limit]

    monkeypatch.setattr(session_manager, "list_sessions", list_page)
    gateway = Gateway(lambda _frame: None)
    gateway._db_ready = True
    first = await gateway._session_list({"limit": 2})
    second = await gateway._session_list({"limit": 2, "cursor": first["nextCursor"]})
    assert [item["id"] for item in first["sessions"] + second["sessions"]] == [
        str(session.id) for session in sessions
    ]


@pytest.mark.asyncio
async def test_parallel_delegation_serializes_one_failure(monkeypatch):
    from ah.core.orchestrator import DelegationResult, orchestrator
    from ah.gateway.features import agents_run

    session_id = uuid.uuid4()
    good = DelegationResult("researcher", "a", "done", session_id, 2, 1, "complete")
    monkeypatch.setattr(
        orchestrator, "run_parallel", AsyncMock(return_value=[good, RuntimeError("failed")])
    )
    gateway = SimpleNamespace(require_db=lambda: None)
    result = await agents_run(
        gateway,
        {
            "mode": "parallel",
            "steps": [{"agent": "researcher", "task": "a"}, {"agent": "coder", "task": "b"}],
        },
    )
    assert [item["status"] for item in result["results"]] == ["complete", "error"]
    assert result["results"][1]["agent"] == "coder"


@pytest.mark.asyncio
async def test_job_claim_has_recovery_lease_and_named_agent_definition(monkeypatch):
    from ah.core.agent_def import AgentDef, agent_registry
    from ah.core.scheduler import JobRunner, JobStore
    from ah.db.connection import db

    fetchrow = AsyncMock(return_value=None)
    monkeypatch.setattr(db, "fetchrow", fetchrow)
    await JobStore().claim_due(datetime.now(UTC))
    sql = fetchrow.await_args.args[0]
    assert "next_run_at = $1 +" in sql
    assert "status <> 'running'" not in sql

    definition = AgentDef(
        name="researcher",
        system_prompt="Research only",
        tools=["read_file"],
        model="special-model",
        provider="ollama",
        max_iterations=3,
    )
    monkeypatch.setattr(agent_registry, "get", AsyncMock(return_value=definition))
    provider = object()
    monkeypatch.setattr("ah.core.provider.get_provider", lambda **_kwargs: provider)
    agent = await JobRunner()._build_agent("researcher")
    assert agent.allowed_tools == ["read_file"]
    assert agent.system_prompt == "Research only"
    assert agent.max_iterations == 3


@pytest.mark.asyncio
async def test_running_job_renews_its_lease(monkeypatch):
    from ah.core.scheduler import JobRunner

    monkeypatch.setattr("ah.core.scheduler.RUN_LEASE_SECONDS", 0.03)
    job_id = uuid.uuid4()
    session_id = uuid.uuid4()

    class Store:
        def __init__(self):
            self.claimed = False
            self.renewed = 0
            self.finished = False

        async def claim_due(self):
            if self.claimed:
                return None
            self.claimed = True
            return SimpleNamespace(
                id=job_id,
                session_id=session_id,
                name="test",
                agent_name="harness",
                prompt="work",
            )

        async def renew_lease(self, _job_id):
            self.renewed += 1

        async def finish(self, _job_id, *, error=None):
            assert error is None
            self.finished = True

    class Agent:
        async def run(self, *_args, **_kwargs):
            await asyncio.sleep(0.06)

    store = Store()
    assert await JobRunner(store=store, agent_factory=lambda _name: Agent()).run_due_once()
    assert store.renewed > 0
    assert store.finished


@pytest.mark.asyncio
async def test_zero_rate_limit_disables_throttling():
    await asyncio.wait_for(AsyncTokenBucket(rate=0, capacity=0).acquire(), timeout=0.1)


def test_compose_starts_api_and_initializes_schema():
    compose = yaml.safe_load((Path(__file__).parents[1] / "docker-compose.yml").read_text())
    app = compose["services"]["app"]
    assert "ah init" in app["command"][0]
    assert "ah serve" in app["command"][0]
    assert "127.0.0.1:8000:8000" in app["ports"]
