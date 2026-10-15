"""Cross-agent memory delivery must validate provenance for each recipient."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from machina.db.connection import Database
from machina.memory.shared_bus import SharedMemoryBus
from machina.memory.store import MemoryStore
from machina.tools.agents import current_agent_id, current_session_id
from machina.tools.base import registry


async def test_delivery_is_scoped_idempotent_and_rejects_tampering(db_pool, monkeypatch):
    monkeypatch.setenv("MACHINA_PROVENANCE_KEY", "test-only-provenance-key")
    monkeypatch.setattr("machina.memory.store.db", db_pool)
    monkeypatch.setattr("machina.memory.retriever.db", db_pool)
    monkeypatch.setattr("machina.memory.identity.db", db_pool)
    monkeypatch.setattr("machina.memory.shared_bus.db", db_pool)
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
        assert (
            await bus.deliver(tampered.id, other_agent, publisher_agent=source_agent)
        ).status == "rejected"
        with pytest.raises(PermissionError):
            await bus.deliver(source.id, other_agent, publisher_agent=target_agent)
    finally:
        await db_pool.execute(
            "DELETE FROM memories WHERE agent_id = ANY($1::text[])",
            [
                source_agent,
                target_agent,
                other_agent,
            ],
        )


async def test_delivery_without_signing_key_is_rejected(db_pool, monkeypatch):
    monkeypatch.delenv("MACHINA_PROVENANCE_KEY", raising=False)
    monkeypatch.setattr("machina.memory.store.db", db_pool)
    monkeypatch.setattr("machina.memory.identity.db", db_pool)
    monkeypatch.setattr("machina.memory.shared_bus.db", db_pool)
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
        await db_pool.execute(
            "DELETE FROM memories WHERE agent_id = ANY($1::text[])",
            [
                source_agent,
                target_agent,
            ],
        )


async def test_missing_provenance_rejection_is_retryable(db_pool, monkeypatch):
    """R4-089 (d): a transiently missed provenance row must not be cached as a
    permanent rejection; once the provenance row is visible, the retry
    delivers the memory."""
    monkeypatch.setenv("MACHINA_PROVENANCE_KEY", "test-only-provenance-key")
    monkeypatch.setattr("machina.memory.store.db", db_pool)
    monkeypatch.setattr("machina.memory.identity.db", db_pool)
    monkeypatch.setattr("machina.memory.shared_bus.db", db_pool)
    source_agent = f"source-{uuid.uuid4()}"
    target_agent = f"target-{uuid.uuid4()}"
    store = MemoryStore()
    bus = SharedMemoryBus()
    source = await store.add(None, source_agent, "Retryable provenance fact", "fact")
    try:
        provenance = await db_pool.fetchrow(
            """
            SELECT memory_id, source_agent, signature, parent_memory_id
            FROM memory_provenance WHERE memory_id = $1
            """,
            source.id,
        )
        assert provenance is not None

        # Transient miss: the source memory is durable but its provenance row
        # is not visible yet (e.g. the write's provenance insert failed after
        # the memory insert committed).
        await db_pool.execute(
            "DELETE FROM memory_provenance WHERE memory_id = $1", source.id
        )
        first = await bus.deliver(source.id, target_agent, publisher_agent=source_agent)
        assert first.status == "rejected"
        assert (
            await db_pool.fetchval(
                "SELECT COUNT(*) FROM shared_memory_deliveries WHERE source_memory_id = $1",
                source.id,
            )
            == 0
        ), "a transient provenance miss must not become a durable rejection"

        # Recovery: the provenance row becomes visible; the retry must succeed.
        await db_pool.execute(
            """
            INSERT INTO memory_provenance
                (memory_id, source_agent, signature, parent_memory_id)
            VALUES ($1, $2, $3, $4)
            """,
            provenance["memory_id"],
            provenance["source_agent"],
            provenance["signature"],
            provenance["parent_memory_id"],
        )
        second = await bus.deliver(source.id, target_agent, publisher_agent=source_agent)
        assert second.status == "accepted"
        assert second.target_memory_id is not None
    finally:
        await db_pool.execute(
            "DELETE FROM memories WHERE agent_id = ANY($1::text[])",
            [source_agent, target_agent],
        )


async def test_delivery_works_with_a_single_database_connection(db_pool, monkeypatch):
    monkeypatch.setenv("MACHINA_PROVENANCE_KEY", "test-only-provenance-key")
    monkeypatch.setenv("MACHINA_DB_MIN_POOL", "1")
    monkeypatch.setenv("MACHINA_DB_MAX_POOL", "1")
    single = Database(dsn=db_pool.dsn)
    await single.connect()
    monkeypatch.setattr("machina.memory.store.db", single)
    monkeypatch.setattr("machina.memory.identity.db", single)
    monkeypatch.setattr("machina.memory.shared_bus.db", single)
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
    monkeypatch.setenv("MACHINA_PROVENANCE_KEY", "test-only-provenance-key")
    monkeypatch.setattr("machina.memory.store.db", db_pool)
    monkeypatch.setattr("machina.memory.retriever.db", db_pool)
    monkeypatch.setattr("machina.memory.identity.db", db_pool)
    monkeypatch.setattr("machina.memory.shared_bus.db", db_pool)
    monkeypatch.setattr("machina.tools.agents.db", db_pool)
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
        assert "purple deployment" in await registry.execute("recall", query="purple deployment")
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
