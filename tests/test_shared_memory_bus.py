"""Cross-agent memory delivery must validate provenance for each recipient."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from ah.db.connection import Database
from ah.memory.shared_bus import SharedMemoryBus
from ah.memory.store import MemoryStore
from ah.tools import registry
from ah.tools.agents import current_agent_id, current_session_id


async def test_delivery_is_scoped_idempotent_and_rejects_tampering(db_pool, monkeypatch):
    monkeypatch.setenv("AGENT_HARNESS_PROVENANCE_KEY", "test-only-provenance-key")
    monkeypatch.setattr("ah.memory.store.db", db_pool)
    monkeypatch.setattr("ah.memory.retriever.db", db_pool)
    monkeypatch.setattr("ah.memory.identity.db", db_pool)
    monkeypatch.setattr("ah.memory.shared_bus.db", db_pool)
    source_agent = f"source-{uuid.uuid4()}"
    target_agent = f"target-{uuid.uuid4()}"
    other_agent = f"other-{uuid.uuid4()}"
    store = MemoryStore()
    bus = SharedMemoryBus()
    source = await store.add(None, source_agent, "The green deployment passed", "fact")
    tampered = await store.add(None, source_agent, "The blue deployment failed", "fact")
    try:
        first = await bus.deliver(source.id, target_agent, publisher_agent=source_agent)
        assert first.status == "accepted"
        assert first.target_memory_id is not None
        again = await bus.deliver(source.id, target_agent, publisher_agent=source_agent)
        assert again.target_memory_id == first.target_memory_id
        assert [item.id for item in await store.search(agent_id=target_agent)] == [
            first.target_memory_id
        ]
        assert await store.search(agent_id=other_agent) == []

        await db_pool.execute(
            "UPDATE memories SET content = 'The blue deployment passed' WHERE id = $1",
            tampered.id,
        )
        rejected = await bus.deliver(tampered.id, other_agent, publisher_agent=source_agent)
        assert rejected.status == "rejected"
        assert rejected.target_memory_id is None
        assert await store.search(agent_id=other_agent) == []
        assert (await bus.deliver(tampered.id, other_agent, publisher_agent=source_agent)).status == "rejected"
        with pytest.raises(PermissionError):
            await bus.deliver(source.id, other_agent, publisher_agent=target_agent)
    finally:
        await db_pool.execute("DELETE FROM memories WHERE agent_id = ANY($1::text[])", [
            source_agent, target_agent, other_agent,
        ])


async def test_delivery_without_signing_key_is_rejected(db_pool, monkeypatch):
    monkeypatch.delenv("AGENT_HARNESS_PROVENANCE_KEY", raising=False)
    monkeypatch.setattr("ah.memory.store.db", db_pool)
    monkeypatch.setattr("ah.memory.identity.db", db_pool)
    monkeypatch.setattr("ah.memory.shared_bus.db", db_pool)
    source_agent = f"source-{uuid.uuid4()}"
    target_agent = f"target-{uuid.uuid4()}"
    store = MemoryStore()
    try:
        source = await store.add(None, source_agent, "Private source memory", "fact")
        result = await SharedMemoryBus().deliver(
            source.id, target_agent, publisher_agent=source_agent
        )
        assert result.status == "rejected"
        assert result.target_memory_id is None
        assert await store.search(agent_id=target_agent) == []
    finally:
        await db_pool.execute("DELETE FROM memories WHERE agent_id = ANY($1::text[])", [
            source_agent, target_agent,
        ])


async def test_delivery_works_with_a_single_database_connection(db_pool, monkeypatch):
    monkeypatch.setenv("AGENT_HARNESS_PROVENANCE_KEY", "test-only-provenance-key")
    monkeypatch.setenv("AGENT_HARNESS_DB_MIN_POOL", "1")
    monkeypatch.setenv("AGENT_HARNESS_DB_MAX_POOL", "1")
    single = Database(dsn=db_pool.dsn)
    await single.connect()
    monkeypatch.setattr("ah.memory.store.db", single)
    monkeypatch.setattr("ah.memory.identity.db", single)
    monkeypatch.setattr("ah.memory.shared_bus.db", single)
    source_agent = f"source-{uuid.uuid4()}"
    target_agent = f"target-{uuid.uuid4()}"
    try:
        source = await MemoryStore().add(None, source_agent, "Single pool delivery", "fact")
        result = await asyncio.wait_for(
            SharedMemoryBus().deliver(source.id, target_agent, publisher_agent=source_agent),
            timeout=3,
        )
        assert result.status == "accepted"
    finally:
        await single.execute(
            "DELETE FROM memories WHERE agent_id = ANY($1::text[])",
            [source_agent, target_agent],
        )
        await single.close()


async def test_memory_and_share_tools_use_the_running_agent_scope(db_pool, monkeypatch):
    monkeypatch.setenv("AGENT_HARNESS_PROVENANCE_KEY", "test-only-provenance-key")
    monkeypatch.setattr("ah.memory.store.db", db_pool)
    monkeypatch.setattr("ah.memory.retriever.db", db_pool)
    monkeypatch.setattr("ah.memory.identity.db", db_pool)
    monkeypatch.setattr("ah.memory.shared_bus.db", db_pool)
    monkeypatch.setattr("ah.tools.agents.db", db_pool)
    source_agent = f"source-{uuid.uuid4()}"
    target_agent = f"target-{uuid.uuid4()}"
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ($1, 'Share tool') RETURNING id",
        source_agent,
    )
    session_token = current_session_id.set(session_id)
    agent_token = current_agent_id.set(source_agent)
    try:
        await registry.execute("remember", content="The purple deployment succeeded")
        owned = await MemoryStore().search(agent_id=source_agent)
        assert len(owned) == 1
        assert owned[0].session_id == session_id
        assert "purple deployment" in await registry.execute(
            "recall", query="purple deployment"
        )
        shared = await registry.execute(
            "share_memory", memory_id=str(owned[0].id), recipient_agent=target_agent
        )
        assert "accepted" in shared
        assert len(await MemoryStore().search(agent_id=target_agent)) == 1
    finally:
        current_agent_id.reset(agent_token)
        current_session_id.reset(session_token)
        await db_pool.execute(
            "DELETE FROM memories WHERE agent_id = ANY($1::text[])",
            [source_agent, target_agent],
        )
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)
