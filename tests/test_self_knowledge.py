"""Feature knowledge reaches actual model calls; introspection stays scoped/read-only."""

import json
import uuid
from unittest.mock import AsyncMock

import pytest

from machina.core.config import config
from machina.core.models import LLMResponse, Session, StreamEvent


def test_capability_brief_explains_oauth_and_inline_user_images():
    from machina.core.self_knowledge import FEATURES

    assert "OAuth remains unavailable" not in FEATURES["mcp"]
    assert "OAuth" in FEATURES["mcp"]
    assert "PKCE" in FEATURES["mcp"]
    assert "[image 1]" in FEATURES.get("images", "")


def setup_agent(monkeypatch, *, allowed_tools=None):
    from machina.core.agent import ReActAgent

    for name in (
        "memory_enabled",
        "personality_growth_enabled",
        "rsi_enabled",
        "rag_enabled",
        "memory_consolidation_enabled",
        "learning_review_enabled",
        "compression_enabled",
    ):
        monkeypatch.setattr(config, name, False)
    session = Session(id=uuid.uuid4(), agent_id="knowledge-test", execution_mode="workspace")
    monkeypatch.setattr("machina.core.agent.session_manager.get", AsyncMock(return_value=session))
    monkeypatch.setattr("machina.core.agent.session_manager.update_activity", AsyncMock())
    for method in (
        "add_chunk",
        "add_chunks_batch",
        "search_archive_text",
        "get_recent_context",
        "get_latest_images",
    ):
        monkeypatch.setattr(
            f"machina.core.agent.context_manager.{method}", AsyncMock(return_value=[])
        )
    monkeypatch.setattr("machina.core.agent.usage_store.reserve", AsyncMock(return_value=None))
    monkeypatch.setattr("machina.core.agent.usage_store.finish", AsyncMock())

    class Provider:
        model = "knowledge-fixture"

        def __init__(self):
            self.calls = []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            return LLMResponse(
                content="Fixture answer", model=self.model, usage={"total_tokens": 5}
            )

        async def stream_complete(self, **kwargs):
            yield StreamEvent(type="done", response=await self.complete(**kwargs))

    provider = Provider()
    agent = ReActAgent(
        provider=provider,
        agent_id=session.agent_id,
        system_prompt="CUSTOM CORE",
        allowed_tools=allowed_tools,
    )
    return agent, provider, session


@pytest.mark.parametrize("streaming", [False, True])
async def test_feature_brief_reaches_model_even_with_empty_notes_and_custom_prompt(
    monkeypatch, streaming
):
    agent, provider, session = setup_agent(monkeypatch, allowed_tools=[])
    query = "Do you have persona, growth and RSI?"
    if streaming:
        _ = [event async for event in agent.run_stream(session.id, query, verbose=False)]
    else:
        await agent.run(session.id, query, verbose=False)
    prompt = provider.calls[0]["messages"][0]["content"]
    assert "CUSTOM CORE" in prompt
    assert "Harness capabilities" in prompt
    for value in (
        "persona_description",
        "persona_memory_enabled",
        "persona_default_emotion",
        "RSI",
        "model weights",
        "empty notes",
        "/growth",
        "/persona",
        "/mcp",
    ):
        assert value in prompt
    assert '"growth":false' in prompt and '"rsi":false' in prompt
    assert '"mode":"workspace"' in prompt
    assert "Use harness_status" not in prompt, (
        "restricted agents must not be told to use an unavailable tool"
    )
    assert len(provider.calls) == 1, "capability awareness needs no extra model call"


async def test_capability_brief_is_bounded_live_and_never_reads_secrets(monkeypatch):
    from machina.core.assembler import get_token_count
    from machina.core.self_knowledge import capability_context

    secret = "sk-or-v1-" + "a" * 50
    monkeypatch.setattr(config, "openrouter_api_key", secret)
    monkeypatch.setattr(config, "persona_description", "A warm tutor " * 100)
    session = Session(id=uuid.uuid4())
    context = capability_context(session, tool_names={"harness_status"}, growth_state="unavailable")
    assert "Use harness_status" in context
    assert secret not in context and "A warm tutor" not in context, (
        "persona content already has its own prompt section"
    )
    assert get_token_count(context) <= 650
    assert '"growth_turn":"unavailable"' in context
    monkeypatch.setattr(config, "rsi_enabled", False)
    assert '"rsi":false' in capability_context(session, tool_names=set(), growth_state="skipped")


@pytest.mark.parametrize(
    "emotion", ["sk-or-v1-" + "a" * 50, "word " * 10000], ids=["secret", "oversized"]
)
async def test_unrestricted_emotion_never_leaks_or_expands_feature_context(monkeypatch, emotion):
    from machina.core.assembler import get_token_count
    from machina.core.self_knowledge import capability_context, configuration

    monkeypatch.setattr(config, "persona_default_emotion", emotion)
    session = Session(id=uuid.uuid4())
    context = capability_context(session, tool_names={"harness_status"}, growth_state="active")
    assert emotion not in context
    assert configuration(session)["default_emotion"] == "unrecognized (neutral fallback)"
    assert get_token_count(context) <= 650


def test_mode_summary_is_bounded_and_missing_mode_is_unknown():
    from types import SimpleNamespace

    from machina.core.self_knowledge import configuration

    assert configuration(SimpleNamespace())["mode"] == "unknown"
    assert configuration(SimpleNamespace(execution_mode="word " * 10000))["mode"] == "unknown"


async def test_status_omits_oversized_data_without_losing_counts_or_breaking_json(monkeypatch):
    from machina.tools import harness

    sid = uuid.uuid4()
    owner = "bounded-owner"
    monkeypatch.setattr(config, "persona_default_emotion", "word " * 10000)
    monkeypatch.setattr(config, "persona_description", "persona " * 10000)
    monkeypatch.setattr(harness, "active_agent_scope", AsyncMock(return_value=(owner, sid)))
    monkeypatch.setattr(
        harness.session_manager, "get", AsyncMock(return_value=Session(id=sid, agent_id=owner))
    )
    monkeypatch.setattr(
        harness.growth_store,
        "get",
        AsyncMock(
            return_value={
                "notes": {"style:tone": "note " * 10000},
                "interactions": 7,
                "feedback_count": 2,
            }
        ),
    )
    monkeypatch.setattr(
        harness.growth_store,
        "rsi_status",
        AsyncMock(return_value={"enabled": True, "trial": {"proposal": "trial " * 10000}}),
    )
    result = await harness.harness_status()
    payload = json.loads(result)
    assert len(result) <= 8500
    assert payload["learning"]["note_count"] == 1
    assert payload["learning"]["notes_omitted"]
    assert payload["persona_description_omitted"]
    assert payload["learning"]["rsi"]["trial_omitted"]
    assert payload["learning"]["interactions"] == 7


async def test_status_tool_is_read_only_owned_and_redacted(monkeypatch):
    from machina.tools import harness

    sid = uuid.uuid4()
    owner = "status-owner"
    session = Session(id=sid, agent_id=owner, execution_mode="ask")
    secret = "sk-or-v1-" + "b" * 50
    monkeypatch.setattr(config, "persona_description", "Tutor " + secret)
    monkeypatch.setattr(harness, "active_agent_scope", AsyncMock(return_value=(owner, sid)))
    monkeypatch.setattr(harness.session_manager, "get", AsyncMock(return_value=session))
    growth = AsyncMock(
        return_value={
            "notes": {"style:tone": "Warm " + secret},
            "interactions": 7,
            "feedback_count": 2,
        }
    )
    rsi = AsyncMock(return_value={"enabled": True, "revision": 3, "observations": 4, "trial": None})
    monkeypatch.setattr(harness.growth_store, "get", growth)
    monkeypatch.setattr(harness.growth_store, "rsi_status", rsi)
    payload = json.loads(await harness.harness_status())
    growth.assert_awaited_once_with(owner)
    rsi.assert_awaited_once_with(owner)
    assert payload["learning"]["interactions"] == 7
    assert payload["learning"]["rsi"]["revision"] == 3
    assert payload["session"]["mode"] == "ask"
    assert secret not in json.dumps(payload)
    assert payload["read_only"] is True
    from machina.permissions.tools import action_for_tool

    assert action_for_tool("harness_status", {})["operation"] == "context.read"
    assert "mcp_call" in payload["features"]["mcp"], "discovery is not remote tool exposure"


async def test_status_tool_refuses_wrong_owner_and_sanitizes_failure(monkeypatch):
    from machina.core.exceptions import ToolError
    from machina.tools import harness

    sid = uuid.uuid4()
    monkeypatch.setattr(harness, "active_agent_scope", AsyncMock(return_value=("caller", sid)))
    monkeypatch.setattr(
        harness.session_manager,
        "get",
        AsyncMock(return_value=Session(id=sid, agent_id="someone-else")),
    )
    growth = AsyncMock()
    monkeypatch.setattr(harness.growth_store, "get", growth)
    with pytest.raises(ToolError, match="owned"):
        await harness.harness_status()
    growth.assert_not_awaited()
    monkeypatch.setattr(
        harness.session_manager,
        "get",
        AsyncMock(side_effect=RuntimeError("postgresql://operator:secret@live/db")),
    )
    with pytest.raises(ToolError) as error:
        await harness.harness_status()
    assert "operator" not in str(error.value) and "secret" not in str(error.value)


async def test_failed_growth_preparation_still_describes_feature_without_claiming_success(
    monkeypatch,
):
    agent, _, session = setup_agent(monkeypatch, allowed_tools=[])
    monkeypatch.setattr(config, "memory_enabled", True)
    monkeypatch.setattr(config, "personality_growth_enabled", True)
    monkeypatch.setattr(config, "rsi_enabled", True)
    monkeypatch.setattr(
        "machina.memory.growth.growth_store.begin",
        AsyncMock(side_effect=RuntimeError("fixture unavailable")),
    )
    monkeypatch.setattr("machina.memory.embeddings.embed_query", AsyncMock(return_value=None))
    agent.memory_retriever.retrieve = AsyncMock(return_value=[])
    _, messages = await agent._prepare_context(session.id, "What is RSI?", pending_chunks=[])
    prompt = messages[0]["content"]
    assert "Harness capabilities" in prompt
    assert '"growth":true' in prompt and '"growth_turn":"unavailable"' in prompt


@pytest.mark.parametrize("streaming", [False, True])
async def test_live_agent_inspects_own_state_without_approval_or_truncated_evidence(
    db_pool, monkeypatch, streaming
):
    from machina.core.agent import ReActAgent
    from machina.core.session import session_manager
    from machina.db.connection import db
    from machina.memory.growth import growth_store

    monkeypatch.setattr(db, "_pool", db_pool.pool)
    monkeypatch.setattr(config, "memory_enabled", True)
    monkeypatch.setattr(config, "personality_growth_enabled", True)
    monkeypatch.setattr(config, "rsi_enabled", True)
    monkeypatch.setattr(config, "rag_enabled", False)
    monkeypatch.setattr(config, "memory_consolidation_enabled", False)
    monkeypatch.setattr(config, "learning_review_enabled", False)
    monkeypatch.setattr("machina.memory.embeddings.embed_query", AsyncMock(return_value=None))
    owner = "self-knowledge-" + uuid.uuid4().hex
    session = await session_manager.create(agent_id=owner)

    class Provider:
        model = "self-status-fixture"

        def __init__(self):
            self.calls = []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return LLMResponse(
                    content="",
                    model=self.model,
                    tool_calls=[
                        {
                            "id": "status-1",
                            "type": "function",
                            "function": {"name": "harness_status", "arguments": "{}"},
                        }
                    ],
                    usage={"total_tokens": 5},
                )
            return LLMResponse(
                content="Inspected my current state.", model=self.model, usage={"total_tokens": 5}
            )

        async def stream_complete(self, **kwargs):
            yield StreamEvent(type="done", response=await self.complete(**kwargs))

    try:
        await growth_store.teach(owner, "style", "tone", "Speak plainly")
        provider = Provider()
        agent = ReActAgent(provider=provider, agent_id=owner, allowed_tools=["harness_status"])
        agent.memory_retriever.retrieve = AsyncMock(return_value=[])
        if streaming:
            events = [
                event
                async for event in agent.run_stream(
                    session.id, "Show my actual persona and RSI state", verbose=False
                )
            ]
            result = next(event.response for event in events if event.type == "done")
        else:
            result = await agent.run(
                session.id, "Show my actual persona and RSI state", verbose=False
            )
        assert not result.needs_approval
        assert len(provider.calls) == 2
        observed = next(
            message["content"]
            for message in provider.calls[1]["messages"]
            if message["role"] == "tool"
        )
        payload = json.loads(observed)
        assert payload["session"]["agent_id"] == owner
        assert payload["learning"]["notes"] == {"style:tone": "Speak plainly"}
        assert payload["learning"]["rsi"]["revision"] > 0
        assert "mcp_call" in payload["features"]["mcp"]
    finally:
        await session_manager.delete(session.id)
        await db_pool.execute("DELETE FROM agent_growth WHERE agent_id = $1", owner)


async def test_status_registry_never_reuses_other_agents_or_stale_configuration(monkeypatch):
    from machina.tools import harness
    from machina.tools.base import registry

    sid = uuid.uuid4()
    monkeypatch.setattr(harness, "active_agent_scope", AsyncMock(return_value=("one", sid)))
    getter = AsyncMock(return_value=Session(id=sid, agent_id="one"))
    monkeypatch.setattr(harness.session_manager, "get", getter)
    monkeypatch.setattr(
        harness.growth_store,
        "get",
        AsyncMock(return_value={"notes": {}, "interactions": 0, "feedback_count": 0}),
    )
    monkeypatch.setattr(harness.growth_store, "rsi_status", AsyncMock(return_value={}))
    first = json.loads(await registry.execute("harness_status"))
    monkeypatch.setattr(harness, "active_agent_scope", AsyncMock(return_value=("two", sid)))
    getter.return_value = Session(id=sid, agent_id="two")
    monkeypatch.setattr(config, "rsi_enabled", False)
    second = json.loads(await registry.execute("harness_status"))
    assert first["session"]["agent_id"] == "one"
    assert second["session"]["agent_id"] == "two"
    assert second["configuration"]["rsi"] is False
