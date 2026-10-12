"""RSI changes future runtime behavior, without adding model calls or authority."""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from machina.skills.registry import Skill
from machina.skills.runtime import SkillIndex


def test_learned_skill_weights_only_reorder_relevant_candidates():
    skills = [
        Skill(name, "Probe workflow checks", ["probe"], "inspect and verify", "unused")
        for name in ("alpha-probe", "beta-probe")
    ]
    skills.append(Skill("rust-patterns", "Rust workflow checks", [], "rust", "unused"))
    index = SkillIndex(skills)
    ranked = index.rank(
        "probe", weights={"beta-probe": 1.2, "alpha-probe": 0.8, "rust-patterns": 100000}
    )
    assert [skill.name for skill in ranked] == ["beta-probe", "alpha-probe"]
    requested = index.rank("alpha-probe probe", weights={"beta-probe": 100000, "alpha-probe": 0.01})
    assert requested[0].name == "alpha-probe"
    assert index.rank("hello", weights={"rust-patterns": 100000}) == []


def test_informational_questions_do_not_invent_an_action_strategy():
    from machina.memory.rsi import strategy_context, task_domain

    assert task_domain("What is Python?") == "general"
    assert strategy_context({"domain": task_domain("What is Python?"), "strategy": "verify"}) == ""


@pytest.mark.parametrize("streaming", [False, True])
async def test_paused_and_cancelled_turns_do_not_record_rsi_success(
    db_pool, monkeypatch, streaming
):
    from machina.core.agent import BaseReActAgent, _growth_episode, instrument_run, instrument_stream
    from machina.core.models import AgentResponse, StreamEvent
    from machina.memory.growth import GrowthStore

    store = GrowthStore(db_pool)
    owner = "rsi-paused-" + uuid.uuid4().hex
    from machina.core.session import session_manager
    from machina.db.connection import db

    monkeypatch.setattr(db, "_pool", db_pool.pool)
    monkeypatch.setattr("machina.memory.growth.growth_store", store)
    session = await session_manager.create(agent_id=owner)

    class PausedAgent(BaseReActAgent):
        @instrument_run
        async def paused(self, sid, query, verbose):
            decision = await store.begin(owner, sid, query, 4000)
            _growth_episode.set(decision["id"])
            return AgentResponse(
                content="Approval needed", needs_approval=[{"request_id": "pause"}]
            )

        @instrument_stream
        async def paused_stream(self, sid, query, verbose):
            yield StreamEvent(type="done", response=await self.paused(sid, query, verbose))

        @instrument_run
        async def cancelled(self, sid, query, verbose):
            decision = await store.begin(owner, sid, query, 4000)
            _growth_episode.set(decision["id"])
            raise asyncio.CancelledError

        @instrument_stream
        async def cancelled_stream(self, sid, query, verbose):
            decision = await store.begin(owner, sid, query, 4000)
            _growth_episode.set(decision["id"])
            raise asyncio.CancelledError
            yield  # Make this an async generator; cancellation yields no terminal event.

    agent = PausedAgent(provider=object(), agent_id=owner)
    try:
        if streaming:
            _ = [event async for event in agent.paused_stream(session.id, "fix Python")]
        else:
            await agent.paused(session.id, "fix Python")
        with pytest.raises(asyncio.CancelledError):
            if streaming:
                _ = [event async for event in agent.cancelled_stream(session.id, "fix Python")]
            else:
                await agent.cancelled(session.id, "fix Python")
        episodes = await store.episodes(owner, session.id)
        assert len(episodes) == 2 and not any(episode["completed"] for episode in episodes)
        assert (await store.rsi_status(owner))["observations"] == 0
    finally:
        _growth_episode.set(None)
        await session_manager.delete(session.id)
        await db_pool.execute("DELETE FROM agent_growth WHERE agent_id = $1", owner)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "scenario",
    ["success", "budget", "failed_command", "manual_skill", "concurrent_persona", "cancelled"],
)
async def test_agent_records_evidence_and_reuses_routing_after_restart(
    db_pool, monkeypatch, tmp_path, streaming, scenario
):
    from machina.core.agent import ReActAgent
    from machina.core.config import config
    from machina.core.models import LLMResponse, StreamEvent
    from machina.core.session import session_manager
    from machina.db.connection import db
    from machina.memory.growth import GrowthStore

    monkeypatch.setattr(db, "_pool", db_pool.pool)
    for flag, value in {
        "rsi_enabled": True,
        "memory_enabled": True,
        "personality_growth_enabled": True,
        "rag_enabled": False,
        "learning_review_enabled": False,
        "memory_consolidation_enabled": False,
    }.items():
        monkeypatch.setattr(config, flag, value)
    monkeypatch.setattr("machina.tools.file._BASE_DIR", tmp_path)
    monkeypatch.setattr("machina.memory.embeddings.embed_query", AsyncMock(return_value=None))
    owner = "rsi-agent-" + uuid.uuid4().hex
    sessions = []
    if scenario == "concurrent_persona":
        from machina.memory.growth import growth_store

        await growth_store.teach(owner, "style", "tone", "Snapshot tone")
        original_prompt = growth_store.prompt
        changed = False

        async def concurrent_prompt(agent_id, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                await growth_store.teach(owner, "style", "tone", "Later tone")
            return await original_prompt(agent_id, **kwargs)

        monkeypatch.setattr(growth_store, "prompt", concurrent_prompt)
    tool = "skill_read" if scenario == "manual_skill" else "list_files"
    if scenario == "manual_skill":
        # This test must attribute the manual read, independently of auto-loading.
        monkeypatch.setattr("machina.skills.runtime.automatic_skill_context", lambda *args, **kwargs: "")
    if scenario == "failed_command":
        import sys

        from machina.tools.terminal import _run_bounded

        actual_failure = _run_bounded(
            [sys.executable, "-c", "raise SystemExit(1)"], 3, str(tmp_path)
        )
        monkeypatch.setattr(
            "machina.core.agent.registry.execute", AsyncMock(return_value=actual_failure)
        )

    class Provider:
        model = "fake-rsi-model"

        def __init__(self, use_tool=False):
            self.use_tool = use_tool
            self.calls = []
            self.waiting = asyncio.Event()

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            if self.use_tool and len(self.calls) == 1:
                return LLMResponse(
                    content="",
                    model=self.model,
                    usage={"total_tokens": 10},
                    tool_calls=[
                        {
                            "id": "rsi-list",
                            "type": "function",
                            "function": {
                                "name": tool,
                                "arguments": json.dumps(
                                    {"skill_name": "ecc-python-patterns"}
                                    if scenario == "manual_skill"
                                    else {"path": "."}
                                ),
                            },
                        }
                    ],
                )
            if self.use_tool and scenario == "budget":
                from machina.core.exceptions import UsageBudgetExceededError

                raise UsageBudgetExceededError("session request budget exceeded (1/1)")
            if self.use_tool and scenario == "cancelled":
                self.waiting.set()
                await asyncio.Event().wait()
            return LLMResponse(
                content="Observed the project files.", model=self.model, usage={"total_tokens": 10}
            )

        async def stream_complete(self, **kwargs):
            yield StreamEvent(type="done", response=await self.complete(**kwargs))

    try:
        first = await session_manager.create(agent_id=owner)
        sessions.append(first.id)
        provider = Provider(use_tool=True)
        agent = ReActAgent(provider=provider, agent_id=owner, allowed_tools=["read_file", tool])
        agent.memory_retriever.retrieve = AsyncMock(return_value=[])

        async def invoke():
            if streaming:
                result = [
                    event
                    async for event in agent.run_stream(
                        first.id, "Inspect this Python project", verbose=False
                    )
                ]
                return next(event.response for event in result if event.type == "done")
            return await agent.run(first.id, "Inspect this Python project", verbose=False)

        if scenario == "cancelled":
            task = asyncio.create_task(invoke())
            try:
                await asyncio.wait_for(provider.waiting.wait(), 5)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert len(provider.calls) == 2
            episodes = await GrowthStore(db_pool).episodes(owner, first.id)
            assert len(episodes) == 1 and not episodes[0]["completed"]
            assert (await GrowthStore(db_pool).rsi_status(owner))["observations"] == 0
            return
        response = await invoke()
        assert not response.needs_approval
        assert len(provider.calls) == 2
        status = await GrowthStore(db_pool).rsi_status(owner)
        assert status["observations"] == 1
        failed = scenario in {"budget", "failed_command"}
        assert (status["tool_weights"][tool] < 1) if failed else (status["tool_weights"][tool] > 1)
        if scenario == "budget":
            assert response.outcome == "failed"
        if scenario == "failed_command":
            assert response.tool_calls[0]["outcome"] == "failed"
        if scenario == "manual_skill":
            assert status["skill_weights"]["ecc-python-patterns"] > 1
        if scenario == "concurrent_persona":
            evidence = await db_pool.fetchrow(
                "SELECT r.revision FROM rsi_episodes r JOIN growth_episodes g ON g.id = r.id "
                "WHERE g.agent_id = $1",
                owner,
            )
            assert evidence["revision"] == 1 and status["revision"] == 2
            assert "Snapshot tone" in str(provider.calls[0]["messages"])
            assert "Later tone" not in str(provider.calls[0]["messages"])
        second = await session_manager.create(agent_id=owner)
        sessions.append(second.id)
        restarted = ReActAgent(
            provider=Provider(), agent_id=owner, allowed_tools=["read_file", tool]
        )
        restarted.memory_retriever.retrieve = AsyncMock(return_value=[])
        await restarted.run(second.id, "Inspect this Python project", verbose=False)
        offered = [tool.name for tool in restarted.provider.calls[0]["tools"]]
        assert offered == (["read_file", tool] if failed else [tool, "read_file"])
        assert len(restarted.provider.calls) == 1
        prompt = str(restarted.provider.calls[0]["messages"])
        assert "Experience-backed task approach" in prompt
    finally:
        for sid in sessions:
            await session_manager.delete(sid)
        await db_pool.execute("DELETE FROM agent_growth WHERE agent_id = $1", owner)


async def test_task_local_tool_weights_do_not_widen_allowlists_or_leak_between_tasks():
    from machina.core.agent import ReActAgent, _rsi_choice

    agent = ReActAgent(provider=object(), allowed_tools=["read_file", "list_files"])

    async def offered(weights):
        token = _rsi_choice.set({"tool_weights": weights})
        try:
            await asyncio.sleep(0)
            return [tool.name for tool in agent._tool_defs()]
        finally:
            _rsi_choice.reset(token)

    first, second = await asyncio.gather(
        offered({"list_files": 1.2, "terminal": 100}), offered({"read_file": 1.2})
    )
    assert first == ["list_files", "read_file"]
    assert second == ["read_file", "list_files"]
