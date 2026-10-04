"""Emotion retrieval fetches persona interpretations in one bounded query."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from ah.memory.models import MemoryEntry, RetrievedMemory
from ah.memory.persona import PersonaMemoryStore
from ah.memory.retriever import MemoryRetriever


async def test_persona_store_searches_facts_in_one_query(monkeypatch):
    from ah.memory import persona

    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(persona.db, "fetch", fetch)
    fact_ids = [uuid.uuid4(), uuid.uuid4()]
    assert await PersonaMemoryStore().search_persona_for_facts(fact_ids, "owner", 10) == []
    fetch.assert_awaited_once()
    assert fact_ids == fetch.await_args.args[1]
    assert "PARTITION BY fact_id" in fetch.await_args.args[0]


async def test_emotion_retrieval_batches_candidate_ids():
    fact_ids = [uuid.uuid4(), uuid.uuid4()]
    candidates = [
        RetrievedMemory(
            memory=MemoryEntry(
                id=fact_id, session_id=None, agent_id="owner", content="fact", category="fact"
            ),
            score=0.5,
        )
        for fact_id in fact_ids
    ]
    persona_store = AsyncMock()
    persona_store.search_persona_for_facts.return_value = []
    retriever = MemoryRetriever(persona_store=persona_store)
    assert await retriever._retrieve_with_emotion(candidates, "joy", "owner") == candidates
    persona_store.search_persona_for_facts.assert_awaited_once_with(fact_ids, "owner", 10)


async def test_persona_batch_preserves_per_fact_limit_in_database(db_pool, monkeypatch):
    from ah.memory import persona

    monkeypatch.setattr(persona, "db", db_pool)
    fact_ids = [uuid.uuid4(), uuid.uuid4()]
    try:
        for fact_id in fact_ids:
            await db_pool.execute(
                "INSERT INTO memories (id, agent_id, content, category) VALUES ($1, $2, $3, $4)",
                fact_id,
                "owner",
                "fact",
                "fact",
            )
            for rank in range(3):
                await db_pool.execute(
                    "INSERT INTO persona_memories (fact_id, persona_id, interpretation, confidence) "
                    "VALUES ($1, $2, $3, $4)",
                    fact_id,
                    "owner",
                    f"rank {rank}",
                    0.9 - rank * 0.1,
                )
            await db_pool.execute(
                "INSERT INTO persona_memories (fact_id, persona_id, interpretation) "
                "VALUES ($1, $2, $3)",
                fact_id,
                "other",
                "private",
            )
        rows = await PersonaMemoryStore().search_persona_for_facts(fact_ids, "owner", 2)
        assert len(rows) == 4
        assert {row.fact_id for row in rows} == set(fact_ids)
        assert all(row.persona_id == "owner" for row in rows)
        assert sorted(row.interpretation for row in rows) == [
            "rank 0",
            "rank 0",
            "rank 1",
            "rank 1",
        ]
    finally:
        await db_pool.execute("DELETE FROM memories WHERE id = ANY($1::uuid[])", fact_ids)
