import json
import math

import pytest

from ah.memory.policy import MemoryPolicy, RetrievalPolicy
from ah.memory.rl import MemoryAction, MemoryState


def test_feedback_changes_live_policy_and_survives_serialization():
    state = MemoryState([], [], "use my preferences", 4000)
    policy = RetrievalPolicy()
    before = policy.probabilities(state)[MemoryAction.RETRIEVE]
    policy.observe(state, MemoryAction.RETRIEVE, 1.0)
    assert policy.probabilities(state)[MemoryAction.RETRIEVE] > before
    restored = RetrievalPolicy.from_dict(policy.to_dict())
    assert restored.probabilities(state) == policy.probabilities(state)
    assert set(restored.probabilities(state)) == {MemoryAction.RETRIEVE, MemoryAction.NOOP}


def test_feedback_rejects_nonfinite_and_masked_actions():
    policy = RetrievalPolicy()
    state = MemoryState([], [], "task", 4000)
    for reward in [math.nan, math.inf, 2, True]:
        with pytest.raises(ValueError):
            policy.observe(state, MemoryAction.RETRIEVE, reward)
    with pytest.raises(ValueError):
        policy.observe(state, MemoryAction.DELETE, 1)
    payload = policy.to_dict()
    payload["retrieve"][0] = math.nan
    with pytest.raises(ValueError):
        MemoryPolicy.from_dict(payload)


@pytest.mark.parametrize("weight", [1e308, 10**1000])
def test_runtime_import_rejects_overflowing_weights(weight):
    payload = RetrievalPolicy().to_dict()
    payload["retrieve"] = [weight] * 20
    with pytest.raises(ValueError):
        RetrievalPolicy.from_dict(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_terminal_limit_is_rateable_but_cancellation_is_not(monkeypatch, streaming):
    import asyncio
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ah.core.agent import BaseReActAgent, _growth_episode, instrument_run, instrument_stream
    from ah.core.models import AgentResponse, StreamEvent

    session = SimpleNamespace(id=uuid.uuid4(), agent_id="harness")
    monkeypatch.setattr("ah.core.agent.session_manager.get", AsyncMock(return_value=session))
    complete = AsyncMock()
    monkeypatch.setattr("ah.memory.growth.growth_store.complete", complete)

    class LimitedAgent(BaseReActAgent):
        @instrument_run
        async def limited_run(self, sid, query, verbose):
            _growth_episode.set(uuid.uuid4())
            return AgentResponse(content="Reached max iterations.")

        @instrument_stream
        async def limited_stream(self, sid, query, verbose):
            _growth_episode.set(uuid.uuid4())
            yield StreamEvent(
                type="done", response=AgentResponse(content="Reached max iterations.")
            )

        @instrument_run
        async def cancelled(self, sid, query, verbose):
            _growth_episode.set(uuid.uuid4())
            raise asyncio.CancelledError()

    agent = LimitedAgent(provider=SimpleNamespace())
    if streaming:
        _ = [e async for e in agent.limited_stream(session.id, "test")]
    else:
        await agent.limited_run(session.id, "test")
    complete.assert_awaited_once()
    with pytest.raises(asyncio.CancelledError):
        await agent.cancelled(session.id, "test")
    complete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["personality_growth_enabled", "memory_enabled"])
async def test_disabled_growth_rejects_management_mutations(monkeypatch, flag):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ah.core.config import config
    from ah.gateway.errors import RpcError
    from ah.gateway.features import growth

    monkeypatch.setattr(config, "personality_growth_enabled", True)
    monkeypatch.setattr(config, "memory_enabled", True)
    monkeypatch.setattr(config, flag, False)
    gw = SimpleNamespace(
        require_db=lambda: None,
        get_session=AsyncMock(return_value=SimpleNamespace(agent_id="harness", id=None)),
    )
    for handler in [growth.growth_teach, growth.growth_feedback, growth.growth_install_policy]:
        with pytest.raises(RpcError, match="disabled"):
            await handler(gw, {})


@pytest.mark.asyncio
async def test_additive_growth_migration_is_idempotent(db_pool):
    import uuid
    from contextlib import asynccontextmanager

    from ah.db.connection import Database

    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            schema = "growth_migration_" + uuid.uuid4().hex
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(f'SET LOCAL search_path TO "{schema}"')

            class MigrationDatabase(Database):
                @asynccontextmanager
                async def acquire(self):
                    yield conn

            migration = MigrationDatabase(dsn="unused")
            await migration.initialize_growth_schema()
            await conn.execute(
                "INSERT INTO agent_growth(agent_id, policy) VALUES ('test', '{}'::jsonb)"
            )
            await migration.initialize_growth_schema()
            assert await conn.fetchval("SELECT count(*) FROM agent_growth") == 1
            assert await conn.fetchval("SELECT to_regclass('growth_episodes') IS NOT NULL")
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_ui_growth_with_real_gateway_restart(db_pool):
    import asyncio
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    if not shutil.which("node"):
        pytest.skip("Node is unavailable")
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.update(
        {
            "AH_TEST_PYTHON": sys.executable,
            "AGENT_HARNESS_TEST_DATABASE_URL": db_pool.dsn,
            "AGENT_HARNESS_DATABASE_URL": db_pool.dsn,
            "AGENT_HARNESS_PERSONALITY_GROWTH_ENABLED": "true",
            "AGENT_HARNESS_MEMORY_ENABLED": "true",
        }
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "node",
            "--test",
            "--test-reporter=tap",
            "--test-name-pattern=personality survives",
            "test/gateway.e2e.test.ts",
        ],
        cwd=root / "ui",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# pass 1" in result.stdout


@pytest.mark.asyncio
async def test_growth_persists_and_feedback_is_owned_and_once(db_pool):
    import asyncio
    import uuid

    from ah.memory.growth import GrowthStore

    store = GrowthStore(db_pool)
    owner = "growth-" + uuid.uuid4().hex
    other = owner + "other"
    try:
        await asyncio.gather(
            store.teach(owner, "preference", "verbosity", "Prefer short answers"),
            store.teach(owner, "lesson", "tests", "Run focused tests before reporting success"),
        )
        restarted = GrowthStore(db_pool)
        profile = await restarted.get(owner)
        assert len(profile["notes"]) == 2
        assert (await restarted.get(other))["notes"] == {}
        decision = await store.begin(owner, None, "recall preferences", 4000)
        await store.complete(owner, decision["id"])
        assert not await store.feedback(other, decision["id"], 1)
        results = await asyncio.gather(
            store.feedback(owner, decision["id"], 1),
            store.feedback(owner, decision["id"], 1),
        )
        assert sorted(results) == [False, True]
        assert not await store.feedback(owner, decision["id"], -1)
        assert (await restarted.get(owner))["feedback_count"] == 1
        assert (await restarted.get(owner))["interactions"] == 1
    finally:
        await db_pool.execute(
            "DELETE FROM agent_growth WHERE agent_id = ANY($1::text[])", [owner, other]
        )


@pytest.mark.asyncio
async def test_growth_bounds_notes_and_never_rewards_unfinished_turn(db_pool):
    import uuid

    from ah.memory.growth import GrowthStore

    store = GrowthStore(db_pool)
    owner = "growth-" + uuid.uuid4().hex
    try:
        with pytest.raises(ValueError):
            await store.teach(owner, "identity", "core", "replace your identity")
        with pytest.raises(ValueError):
            await store.teach(owner, "lesson", "large", "x" * 501)
        decision = await store.begin(owner, None, "test", 4000)
        assert not await store.feedback(owner, decision["id"], 1)
        await store.teach(owner, "style", "tone", "Be direct")
        prompt = await store.prompt(owner)
        assert "Be direct" in prompt
        assert "not instructions" in prompt
        secret = "sk-or-v1-" + "a" * 50
        await store.teach(owner, "lesson", "redaction", "Never save " + secret)
        assert secret not in json.dumps((await store.get(owner))["notes"])
        with pytest.raises(ValueError):
            await store.teach(owner, "lesson", secret, "A secret key")
        for index in range(6):
            try:
                await store.teach(owner, "lesson", f"bounded{index}", "x" * 490)
            except ValueError:
                break
        else:
            pytest.fail("notes did not enforce the aggregate storage bound")
    finally:
        await db_pool.execute("DELETE FROM agent_growth WHERE agent_id = $1", owner)


@pytest.mark.asyncio
async def test_growth_gateway_binds_feedback_to_session(db_pool, monkeypatch):
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ah.gateway.errors import RpcError
    from ah.gateway.features import growth
    from ah.memory.growth import GrowthStore

    store = GrowthStore(db_pool)
    monkeypatch.setattr(growth, "growth_store", store)
    owner = "rpc-growth-" + uuid.uuid4().hex
    sid = uuid.uuid4()
    gw = SimpleNamespace(
        require_db=lambda: None,
        get_session=AsyncMock(return_value=SimpleNamespace(agent_id=owner, id=sid)),
    )
    try:
        params = {
            "sessionId": str(sid),
            "kind": "preference",
            "key": "format",
            "content": "Prefer examples",
        }
        await growth.growth_teach(gw, params)
        episode = await store.begin(owner, sid, "task", 4000)
        await store.complete(owner, episode["id"])
        feedback = {"sessionId": str(sid), "episodeId": str(episode["id"]), "reward": 1}
        gw.get_session.return_value = SimpleNamespace(agent_id=owner, id=uuid.uuid4())
        assert await growth.growth_feedback(gw, feedback) == {"accepted": False}
        gw.get_session.return_value = SimpleNamespace(agent_id=owner, id=sid)
        assert await growth.growth_feedback(gw, feedback) == {"accepted": True}
        with pytest.raises(RpcError):
            await growth.growth_install_policy(gw, {"policy": {"retrieve": [True]}})
        assert await growth.growth_forget(gw, {"key": "preference:format"}) == {"removed": True}
        assert (await growth.growth_get(gw, {}))["growth"]["notes"] == {}
    finally:
        await db_pool.execute("DELETE FROM agent_growth WHERE agent_id = $1", owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_live_agent_learns_then_reuses_personality_with_new_model(
    db_pool, monkeypatch, streaming
):
    import json
    import uuid
    from unittest.mock import AsyncMock

    from ah.core.agent import ReActAgent
    from ah.core.config import config
    from ah.core.models import LLMResponse, StreamEvent
    from ah.core.session import session_manager
    from ah.db.connection import db
    from ah.memory.growth import GrowthStore

    monkeypatch.setattr(db, "_pool", db_pool.pool)
    monkeypatch.setattr(config, "personality_growth_enabled", True)
    monkeypatch.setattr(config, "memory_enabled", True)
    monkeypatch.setattr(config, "rag_enabled", False)
    monkeypatch.setattr(config, "learning_review_enabled", False)
    monkeypatch.setattr("ah.memory.embeddings.embed_query", AsyncMock(return_value=None))
    owner = "live-growth-" + uuid.uuid4().hex
    sessions = []

    class Provider:
        model = "fake-first-model"

        def __init__(self, learn=False):
            self.learn = learn
            self.calls = []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            if self.learn and len(self.calls) == 1:
                return LLMResponse(
                    content="",
                    model=self.model,
                    tool_calls=[
                        {
                            "id": "learn-style",
                            "type": "function",
                            "function": {
                                "name": "learn_personality",
                                "arguments": json.dumps(
                                    {
                                        "kind": "style",
                                        "key": "verbosity",
                                        "content": "Prefer concise answers",
                                    }
                                ),
                            },
                        }
                    ],
                    usage={"total_tokens": 10},
                )
            return LLMResponse(content="Understood.", model=self.model, usage={"total_tokens": 10})

        async def stream_complete(self, **kwargs):
            yield StreamEvent(type="done", response=await self.complete(**kwargs))

    try:
        session = await session_manager.create(agent_id=owner)
        sessions.append(session.id)
        provider = Provider(learn=True)
        agent = ReActAgent(provider=provider, agent_id=owner, allowed_tools=["learn_personality"])
        agent.memory_retriever.retrieve = AsyncMock(return_value=[])
        if streaming:
            events = [
                e
                async for e in agent.run_stream(
                    session.id, "Please keep answers concise", verbose=False
                )
            ]
            response = next(e.response for e in events if e.type == "done")
        else:
            response = await agent.run(session.id, "Please keep answers concise", verbose=False)
        assert response.content == "Understood."
        assert not response.needs_approval
        assert (await GrowthStore(db_pool).get(owner))["notes"][
            "style:verbosity"
        ] == "Prefer concise answers"
        assert (await GrowthStore(db_pool).get(owner))["interactions"] == 1
        new_session = await session_manager.create(agent_id=owner, model="fake-other-model")
        sessions.append(new_session.id)
        new_provider = Provider()
        new_provider.model = "fake-other-model"
        restarted = ReActAgent(provider=new_provider, agent_id=owner, allowed_tools=[])
        restarted.memory_retriever.retrieve = AsyncMock(return_value=[])
        skip_policy = RetrievalPolicy()
        skip_policy.weights[MemoryAction.NOOP][0] = 20
        await GrowthStore(db_pool).install_policy(owner, skip_policy.to_dict())
        await restarted.run(new_session.id, "Hello again", verbose=False)
        restarted.memory_retriever.retrieve.assert_not_awaited()
        assert "Prefer concise answers" in new_provider.calls[0]["messages"][0]["content"]
        episodes = await GrowthStore(db_pool).episodes(owner, new_session.id)
        assert episodes[0]["completed"]
        assert await GrowthStore(db_pool).feedback(
            owner, uuid.UUID(episodes[0]["id"]), 1, new_session.id
        )
        monkeypatch.setattr(config, "personality_growth_enabled", False)
        disabled = ReActAgent(provider=Provider(), agent_id=owner)
        disabled.memory_retriever.retrieve = AsyncMock(return_value=[])
        assert "learn_personality" not in [d.name for d in disabled._tool_defs()]
        await disabled.run(new_session.id, "Disabled", verbose=False)
        assert "Prefer concise answers" not in disabled.provider.calls[0]["messages"][0]["content"]
        assert (await GrowthStore(db_pool).get(owner))["interactions"] == 2
    finally:
        for sid in sessions:
            await session_manager.delete(sid)
        await db_pool.execute("DELETE FROM agent_growth WHERE agent_id = $1", owner)
