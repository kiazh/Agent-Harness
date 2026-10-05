"""Regression tests for the database and runtime paths behind research features."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ah.core.context import ContextManager
from ah.core.exceptions import ToolError, ValidationError
from ah.core.serialization import payload_to_msgpack
from ah.core.session import SessionManager
from ah.memory.approval import MemoryApprovalGate
from ah.memory.identity import AgentBelief, IdentityGate
from ah.memory.persona import PersonaMemoryStore
from ah.memory.retriever import MemoryRetriever
from ah.memory.store import MemoryStore
from ah.memory.user_profile import UserProfileStore
from ah.rag.loaders import FileLoader
from ah.soulspec.schema import SoulSpec
from ah.tools import file as file_tools
from ah.tools.terminal import terminal


async def test_bulk_approval_is_atomic_and_idempotent(db_pool, monkeypatch):
    monkeypatch.setattr("ah.memory.approval.db", db_pool)
    monkeypatch.setattr("ah.memory.store.db", db_pool)
    monkeypatch.setattr("ah.memory.identity.db", db_pool)
    agent = f"approval-{uuid.uuid4()}"
    gate = MemoryApprovalGate(enabled=True)
    pending = await gate.submit("A durable fact", "fact", agent_id=agent)
    try:
        assert await gate.approve_all(agent) == 1
        assert await gate.approve_all(agent) == 0
        row = await db_pool.fetchrow(
            "SELECT status, memory_id FROM pending_memories WHERE id = $1", pending.id
        )
        assert row["status"] == "approved"
        assert (
            await db_pool.fetchval("SELECT COUNT(*) FROM memories WHERE agent_id = $1", agent) == 1
        )
    finally:
        await db_pool.execute("DELETE FROM pending_memories WHERE id = $1", pending.id)
        await db_pool.execute("DELETE FROM memories WHERE agent_id = $1", agent)


async def test_profile_topic_update_uses_jsonb_comparison(db_pool, monkeypatch):
    monkeypatch.setattr("ah.memory.user_profile.db", db_pool)
    store = UserProfileStore()
    user_id = f"profile-{uuid.uuid4()}"
    profile = await store.create(user_id)
    try:
        await store.record_interaction(profile.id, "python")
        updated = await store.record_interaction(profile.id, "python")
        assert updated.interaction_count == 2
        assert updated.topics["python"] == 2
    finally:
        await store.delete(profile.id)


async def test_quarantine_excludes_search_results(db_pool, monkeypatch):
    monkeypatch.setattr("ah.memory.store.db", db_pool)
    agent = f"quarantine-{uuid.uuid4()}"
    await db_pool.execute(
        "INSERT INTO memories (agent_id, content, category, quarantined) "
        "VALUES ($1, 'safe', 'fact', FALSE), ($1, 'unsafe', 'fact', TRUE)",
        agent,
    )
    try:
        results = await MemoryStore().search(agent_id=agent)
        assert [entry.content for entry in results] == ["safe"]
    finally:
        await db_pool.execute("DELETE FROM memories WHERE agent_id = $1", agent)


async def test_belief_jsonb_and_keyed_provenance(db_pool, monkeypatch):
    monkeypatch.setattr("ah.memory.identity.db", db_pool)
    monkeypatch.setattr("ah.memory.store.db", db_pool)
    monkeypatch.setenv("AGENT_HARNESS_PROVENANCE_KEY", "integration-only-key")
    agent = f"belief-{uuid.uuid4()}"
    gate = IdentityGate()
    await gate.save_beliefs(
        AgentBelief(agent_id=agent, known_facts={"Python": 0.9}, traits={}, values={})
    )
    try:
        loaded = await gate._load_beliefs(agent)
        assert loaded.known_facts["Python"] == 0.9
        # The store uses the module singleton; isolate it to this database.
        entry = await MemoryStore().add(None, agent, "Python is good", "fact")
        provenance = await db_pool.fetchrow(
            "SELECT source_agent, signature FROM memory_provenance WHERE memory_id = $1",
            entry.id,
        )
        assert provenance["source_agent"] == agent
        assert len(provenance["signature"]) == 64
    finally:
        await db_pool.execute("DELETE FROM memory_provenance WHERE source_agent = $1", agent)
        await db_pool.execute("DELETE FROM memories WHERE agent_id = $1", agent)
        await db_pool.execute("DELETE FROM agent_beliefs WHERE agent_id = $1", agent)


async def test_unsigned_shared_memory_is_quarantined(db_pool, monkeypatch):
    monkeypatch.setattr("ah.memory.identity.db", db_pool)
    monkeypatch.setattr("ah.memory.store.db", db_pool)
    monkeypatch.delenv("AGENT_HARNESS_PROVENANCE_KEY", raising=False)
    target = f"target-{uuid.uuid4()}"
    entry = await MemoryStore().add(
        None, target, "Unverified shared claim", "fact", source_agent="other-agent"
    )
    try:
        assert entry.quarantined
        assert await MemoryStore().search(agent_id=target) == []
    finally:
        await db_pool.execute("DELETE FROM memories WHERE id = $1", entry.id)


async def test_persona_interpretation_reaches_retrieval(db_pool, monkeypatch):
    monkeypatch.setattr("ah.memory.identity.db", db_pool)
    monkeypatch.setattr("ah.memory.store.db", db_pool)
    monkeypatch.setattr("ah.memory.retriever.db", db_pool)
    monkeypatch.setattr("ah.memory.persona.db", db_pool)
    monkeypatch.delenv("AGENT_HARNESS_PROVENANCE_KEY", raising=False)
    agent = f"persona-{uuid.uuid4()}"
    store = MemoryStore()
    fact = await store.add(None, agent, "Python is useful", "fact")
    personas = PersonaMemoryStore()
    try:
        await personas.add_persona(
            fact.id,
            agent,
            "Approach Python tasks with curiosity",
            emotional_valence=0.7,
        )
        results = await MemoryRetriever(store=store, persona_store=personas, rerank=False).retrieve(
            "Python", agent_id=agent, emotion="joy"
        )
        assert results[0].memory.content == "Python is useful"
        assert results[0].persona_interpretation == "Approach Python tasks with curiosity"
    finally:
        await db_pool.execute("DELETE FROM memories WHERE id = $1", fact.id)


async def test_archived_conversation_recalls_without_embedding(db_pool, monkeypatch):
    monkeypatch.setattr("ah.core.context.db", db_pool)
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id"
    )
    manager = ContextManager()
    try:
        first = await manager.add_chunk(
            session_id,
            "harness",
            "user_message",
            {"content": "nebula telescope observation"},
            40,
        )
        await db_pool.execute(
            "UPDATE context_chunks SET created_at = now() - interval '1 day' WHERE id = $1",
            first.id,
        )
        for index in range(10):
            await manager.add_chunk(
                session_id,
                "harness",
                "user_message",
                {"content": f"recent message {index}"},
                10,
            )
        assert await manager.evict_old_chunks(session_id, max_chunks=10) == 1
        row = await db_pool.fetchrow(
            "SELECT payload_msgpack, agent_id, chunk_type, token_count "
            "FROM context_archive WHERE chunk_id = $1",
            first.id,
        )
        assert row["payload_msgpack"] == payload_to_msgpack(
            {"content": "nebula telescope observation"}
        )
        assert (row["agent_id"], row["chunk_type"], row["token_count"]) == (
            "harness",
            "user_message",
            40,
        )
        matches = await manager.search_archive_text(session_id, "nebula telescope")
        assert matches[0][0].id == first.id
    finally:
        monkeypatch.setattr("ah.core.session.db", db_pool)
        await SessionManager().delete(session_id)
        assert (
            await db_pool.fetchval(
                "SELECT COUNT(*) FROM context_archive WHERE session_id = $1", session_id
            )
            == 0
        )


async def test_eviction_keeps_source_when_archive_fails(monkeypatch):
    manager = ContextManager()
    session_id = uuid.uuid4()
    rows = [
        {
            "id": uuid.uuid4(),
            "token_count": 10,
            "chunk_type": "user_message",
            "created_at": None,
            "payload_msgpack": b"\x80",
            "embedding": None,
            "agent_id": "harness",
        }
        for _ in range(11)
    ]
    fake_db = AsyncMock()
    fake_db.fetch = AsyncMock(return_value=rows)
    fake_db.fetchrow = AsyncMock(return_value={"total_chunks": 11, "total_tokens": 110})
    connection = AsyncMock()
    connection.transaction = MagicMock()
    fake_db.acquire = MagicMock()
    fake_db.acquire.return_value.__aenter__.return_value = connection
    with (
        patch("ah.core.context.db", fake_db),
        patch.object(manager, "archive_chunk", AsyncMock(side_effect=RuntimeError("archive down"))),
    ):
        assert await manager.evict_old_chunks(session_id, max_chunks=10) == 0
    connection.execute.assert_not_awaited()


def test_soulspec_package_round_trip_and_registry(tmp_path, monkeypatch):
    from ah.core.agent_def import AgentRegistry

    spec = SoulSpec(
        name="research-helper",
        version="1.0.0",
        persona=SoulSpec.Persona(
            name="Research Helper",
            description="Finds and checks sources",
            system_prompt="# Identity\nVerify sources before answering.\n",
        ),
    )
    directory = tmp_path / spec.name
    spec.write_package(directory, author="Test Author", tags=["research"])
    manifest = json.loads((directory / "soul.json").read_text(encoding="utf-8"))
    assert manifest["specVersion"] == "0.5"
    restored = SoulSpec.from_package(directory)
    assert restored.persona.system_prompt == spec.persona.system_prompt
    monkeypatch.setenv("AGENT_HARNESS_AGENTS_DIR", str(tmp_path))
    definition = AgentRegistry()._file_definitions()["research-helper"]
    assert definition.source == "soulspec"
    assert "Verify sources" in definition.system_prompt


def test_relative_rag_loader_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "note.md").write_text("hello", encoding="utf-8")
    assert FileLoader(base_dir=".").load("note.md").content == "hello"


async def test_terminal_requires_explicit_sandbox(monkeypatch):
    monkeypatch.delenv("AGENT_HARNESS_TERMINAL_SANDBOX", raising=False)
    with pytest.raises(ValidationError, match="disabled"):
        await terminal("git status")


async def test_file_tools_refuse_environment_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(file_tools, "_BASE_DIR", tmp_path.resolve())
    (tmp_path / ".env").write_text("PRIVATE=value", encoding="utf-8")
    (tmp_path / ".env.example").write_text("PUBLIC=example", encoding="utf-8")
    with pytest.raises(ToolError, match="private"):
        await file_tools.read_file(".env")
    assert "PUBLIC=example" in await file_tools.read_file(".env.example")
