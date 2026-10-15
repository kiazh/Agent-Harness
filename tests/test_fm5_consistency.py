"""FM5 W5/W6/W7/W13/W15: consolidation locking + backlog drain, delivery
atomicity, re-index idempotence, and embedding-space scan caching.

DB-backed tests use the ``db_pool`` fixture (auto-skips without
``MACHINA_TEST_DATABASE_URL``).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from machina.core.models import ContextChunk
from machina.memory.consolidator import MemoryConsolidator
from machina.rag.loaders import Document
from machina.rag.pipeline import RAGConfig, RAGPipeline


class _FakeStore:
    async def search(self, **kwargs):
        return []

    async def search_by_embedding(self, **kwargs):
        return []

    async def update_access(self, *args, **kwargs):
        return None

    async def add(self, **kwargs):
        return SimpleNamespace(id=uuid.uuid4())


def _chunk(session_id: uuid.UUID, created_at: datetime, text: str = "hi") -> ContextChunk:
    return ContextChunk(
        id=uuid.uuid4(),
        session_id=session_id,
        agent_id="harness",
        chunk_type="user_message",
        payload={"content": text},
        token_count=1,
        created_at=created_at,
    )


# --- W5: advisory-lock serialization ----------------------------------------


async def test_concurrent_consolidation_extraction_is_serialized(db_pool, monkeypatch):
    session_id = uuid.uuid4()
    chunk = _chunk(session_id, datetime.now(UTC))
    monkeypatch.setattr("machina.memory.consolidator.db", db_pool)

    class _Ctx:
        async def get_chunks_since(self, *args, **kwargs):
            return [chunk]

        async def get_chunks(self, *args, **kwargs):
            return [chunk]

    monkeypatch.setattr("machina.memory.consolidator.context_manager", _Ctx())
    consolidator = MemoryConsolidator(store=_FakeStore())

    running = 0
    max_running = 0

    async def _slow_extract(*args, **kwargs):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.2)
        running -= 1
        return []

    consolidator._extract_memories = _slow_extract
    first, second = await asyncio.gather(
        consolidator.consolidate_session(session_id, "harness"),
        consolidator.consolidate_session(session_id, "harness"),
    )
    assert max_running == 1, "concurrent extraction overlapped across instances"
    assert first.status == "empty" and second.status == "empty"


async def test_lock_unavailable_falls_back_and_still_consolidates(monkeypatch):
    session_id = uuid.uuid4()
    chunk = _chunk(session_id, datetime.now(UTC))

    class _Ctx:
        async def get_chunks_since(self, *args, **kwargs):
            return [chunk]

        async def get_chunks(self, *args, **kwargs):
            return [chunk]

    class _NoDb:
        connected = True

        def acquire(self):
            raise RuntimeError("database down")

    monkeypatch.setattr("machina.memory.consolidator.db", _NoDb())
    monkeypatch.setattr("machina.memory.consolidator.context_manager", _Ctx())
    consolidator = MemoryConsolidator(store=_FakeStore())
    consolidator._extract_memories = AsyncMock(return_value=[])
    result = await consolidator.consolidate_session(session_id, "harness")
    assert result.status == "empty" and result.checkpoint is not None


# --- W6: oldest-first backlog drain -----------------------------------------


async def test_first_consolidation_reads_oldest_window(monkeypatch):
    session_id = uuid.uuid4()
    now = datetime.now(UTC)
    chunks = [_chunk(session_id, now + timedelta(seconds=i)) for i in range(3)]
    calls: list[tuple] = []

    class _Ctx:
        async def get_chunks_since(self, sid, **kwargs):
            calls.append(kwargs)
            return list(chunks)

        async def get_chunks(self, sid, **kwargs):
            raise AssertionError("newest-first window must not be the first read")

    monkeypatch.setattr("machina.memory.consolidator.context_manager", _Ctx())
    consolidator = MemoryConsolidator(store=_FakeStore())
    consolidator._extract_memories = AsyncMock(return_value=[])
    result = await consolidator.consolidate_session(session_id, "harness")
    assert calls and calls[0].get("since_time") is None and calls[0].get("since_id") is None
    # Checkpoint is the newest of the oldest window, so cursor continuation
    # walks forward and the backlog stays reachable.
    assert result.checkpoint["id"] == str(chunks[-1].id)


async def test_legacy_context_stub_without_since_api_falls_back(monkeypatch):
    session_id = uuid.uuid4()
    chunk = _chunk(session_id, datetime.now(UTC))
    seen: list[str] = []

    class _LegacyCtx:
        async def get_chunks(self, sid, **kwargs):
            seen.append("get_chunks")
            return [chunk]

    monkeypatch.setattr("machina.memory.consolidator.context_manager", _LegacyCtx())
    consolidator = MemoryConsolidator(store=_FakeStore())
    consolidator._extract_memories = AsyncMock(return_value=[])
    result = await consolidator.consolidate_session(session_id, "harness")
    assert seen == ["get_chunks"]
    assert result.status == "empty"


async def test_backlog_drains_in_bounded_oldest_first_pages(db_pool, monkeypatch):
    monkeypatch.setattr("machina.core.context.db", db_pool)
    monkeypatch.setattr("machina.memory.consolidator.db", db_pool)
    from machina.core.serialization import payload_to_msgpack

    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id"
    )
    base = datetime.now(UTC) - timedelta(hours=2)
    try:
        await db_pool.executemany(
            """INSERT INTO context_chunks
               (session_id, agent_id, chunk_type, payload_msgpack, created_at)
               VALUES ($1, 'harness', 'user_message', $2, $3)""",
            [
                (
                    session_id,
                    payload_to_msgpack({"content": f"chunk {i}"}),
                    base + timedelta(seconds=i),
                )
                for i in range(150)
            ],
        )
        ids = [
            row["id"]
            for row in await db_pool.fetch(
                "SELECT id FROM context_chunks WHERE session_id = $1 ORDER BY created_at ASC, id ASC",
                session_id,
            )
        ]
        consolidator = MemoryConsolidator(store=_FakeStore())
        consolidator._extract_memories = AsyncMock(return_value=[])

        first = await consolidator.consolidate_session(session_id, "harness")
        assert first.chunks_processed == 100
        assert first.checkpoint is not None
        assert first.checkpoint["id"] == str(ids[99])

        cursor_time = datetime.fromisoformat(first.checkpoint["at"])
        second = await consolidator.consolidate_session(
            session_id, "harness", since=(cursor_time, ids[99])
        )
        assert second.chunks_processed == 50
        assert second.checkpoint["id"] == str(ids[149])
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


# --- W7: delivery receipt atomicity ------------------------------------------


async def test_receipt_insert_conflict_refetches_existing_receipt():
    from machina.memory.shared_bus import SharedMemoryBus

    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    existing = {
        "source_memory_id": source_id,
        "recipient_agent": "target",
        "target_memory_id": target_id,
        "status": "accepted",
        "reason": "",
    }
    source_row = {
        "id": source_id,
        "session_id": None,
        "agent_id": "source",
        "content": "shared fact",
        "category": "fact",
        "importance": 0.5,
        "quarantined": False,
    }
    provenance_row = {
        "memory_id": source_id,
        "source_agent": "source",
        "signature": "sig",
        "parent_memory_id": None,
        "created_at": datetime.now(UTC),
    }
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=AsyncMock())
    conn.fetchrow = AsyncMock(side_effect=[source_row, None, provenance_row, existing])
    conn.execute = AsyncMock(return_value="INSERT 0 0")
    database = AsyncMock()
    manager = AsyncMock()
    manager.__aenter__.return_value = conn
    database.acquire = MagicMock(return_value=manager)

    provenance = MagicMock()
    provenance.source_agent = "source"
    provenance.verify.return_value = True
    provenance_cls = MagicMock(return_value=provenance)
    gate = AsyncMock()
    gate.validate_incoming = AsyncMock(return_value=SimpleNamespace(is_valid=True, reason=""))
    store = MagicMock(add=AsyncMock(return_value=MagicMock(quarantined=False, id=target_id)))
    with (
        patch("machina.memory.shared_bus.db", database),
        patch("machina.memory.shared_bus.MemoryProvenance", provenance_cls),
        patch("machina.memory.shared_bus.identity_gate", gate),
        patch("machina.memory.shared_bus.memory_store", store),
    ):
        receipt = await SharedMemoryBus().deliver(source_id, "target", publisher_agent="source")
    assert receipt.target_memory_id == target_id
    assert receipt.status == "accepted"
    # The copy and its receipt are committed by the same transaction.
    assert conn.transaction.called
    assert store.add.await_args.kwargs["connection"] is conn
    assert conn.execute.await_args.args[1] == source_id


async def test_concurrent_delivery_yields_one_copy_and_one_receipt(db_pool, monkeypatch):
    monkeypatch.setenv("MACHINA_PROVENANCE_KEY", "test-only-provenance-key")
    monkeypatch.setattr("machina.memory.store.db", db_pool)
    monkeypatch.setattr("machina.memory.identity.db", db_pool)
    monkeypatch.setattr("machina.memory.shared_bus.db", db_pool)
    from machina.memory.shared_bus import SharedMemoryBus
    from machina.memory.store import MemoryStore

    source_agent = f"source-{uuid.uuid4()}"
    target_agent = f"target-{uuid.uuid4()}"
    store = MemoryStore()
    source = await store.add(None, source_agent, "Concurrent delivery fact", "fact")
    try:
        receipts = await asyncio.gather(
            SharedMemoryBus().deliver(source.id, target_agent, publisher_agent=source_agent),
            SharedMemoryBus().deliver(source.id, target_agent, publisher_agent=source_agent),
        )
        assert [r.status for r in receipts] == ["accepted", "accepted"]
        assert receipts[0].target_memory_id == receipts[1].target_memory_id
        copies = await store.search(agent_id=target_agent)
        assert len(copies) == 1
        count = await db_pool.fetchval(
            "SELECT count(*) FROM shared_memory_deliveries WHERE source_memory_id = $1",
            source.id,
        )
        assert count == 1
    finally:
        await db_pool.execute(
            "DELETE FROM memories WHERE agent_id = ANY($1::text[])",
            [source_agent, target_agent],
        )


# --- W13: re-index idempotence -----------------------------------------------


async def test_reindex_same_document_source_keeps_single_copy(db_pool, monkeypatch):
    monkeypatch.setattr("machina.rag.pipeline.db", db_pool)
    monkeypatch.setattr("machina.rag.pipeline.record_failure", lambda *a, **k: None)
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id"
    )
    source = f"doc-{uuid.uuid4()}.txt"
    doc = Document(content=("alpha beta gamma delta. " * 40), source=source, doc_type="text")
    pipeline = RAGPipeline(config=RAGConfig(enable_embeddings=False, enable_reranking=False))
    try:
        first = await pipeline.index_document(doc, session_id)
        second = await pipeline.index_document(doc, session_id)
        count = await db_pool.fetchval(
            "SELECT count(*) FROM context_chunks WHERE session_id = $1 AND document_source = $2",
            session_id,
            source,
        )
        assert count == len(second) == len(first)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_empty_reindex_deletes_previous_generation_only_for_its_source(db_pool, monkeypatch):
    """R4-088(a): truncating a document to empty still replaces its generation.

    An empty re-index previously returned before the delete, leaving the old
    chunks searchable. The delete must run for the same (session, source)
    while other sources and other sessions stay untouched (adversarial
    boundary), and the warmed pipeline cache must not resurrect the deleted
    generation.
    """
    monkeypatch.setattr("machina.rag.pipeline.db", db_pool)
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id"
    )
    other_session = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id"
    )
    source = f"truncated-{uuid.uuid4()}.txt"
    kept_source = f"kept-{uuid.uuid4()}.txt"
    pipeline = RAGPipeline(config=RAGConfig(enable_embeddings=False, enable_reranking=False))
    try:
        await pipeline.index_document(
            Document(content=("alpha beta gamma delta. " * 40), source=source, doc_type="text"),
            session_id,
        )
        await pipeline.index_document(
            Document(content=("keep this generation " * 10), source=kept_source, doc_type="text"),
            session_id,
        )
        # Same source string under another session: must never be collateral.
        await pipeline.index_document(
            Document(content=("other session content " * 10), source=source, doc_type="text"),
            other_session,
        )
        found = await pipeline.search("alpha", session_id, rerank=False)
        assert found, "precondition: the generation is searchable"
        assert all(r.chunk.payload["metadata"]["source"] == source for r in found)

        emptied = await pipeline.index_document(
            Document(content="", source=source, doc_type="text"), session_id
        )
        assert emptied == []
        count = await db_pool.fetchval(
            "SELECT count(*) FROM context_chunks WHERE session_id = $1 AND document_source = $2",
            session_id,
            source,
        )
        assert count == 0, "empty re-index left the previous generation"
        other_count = await db_pool.fetchval(
            "SELECT count(*) FROM context_chunks WHERE session_id = $1 AND document_source = $2",
            session_id,
            kept_source,
        )
        assert other_count > 0, "unrelated source was deleted"
        foreign_count = await db_pool.fetchval(
            "SELECT count(*) FROM context_chunks WHERE session_id = $1 AND document_source = $2",
            other_session,
            source,
        )
        assert foreign_count > 0, "another session's same-named source was deleted"
        assert await pipeline.search("alpha", session_id, rerank=False) == []
    finally:
        await db_pool.execute(
            "DELETE FROM sessions WHERE id = ANY($1::uuid[])", [session_id, other_session]
        )


async def test_concurrent_reindex_of_one_source_keeps_single_copy(db_pool, monkeypatch):
    """8 concurrent replacements of one document leave exactly one generation.

    Without per-(session, source) serialization each replacement's DELETE
    cannot see the other transactions' uncommitted INSERTs, so every
    transaction commits its own copy of the document (audit repro: 8
    concurrent replacements -> 8 stored chunks for one source).
    """
    monkeypatch.setattr("machina.rag.pipeline.db", db_pool)
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id"
    )
    source = f"race-{uuid.uuid4()}.txt"
    doc = Document(content=("A" * 2000) + "\n", source=source, doc_type="text")
    pipeline = RAGPipeline(config=RAGConfig(enable_embeddings=False, enable_reranking=False))
    try:
        await asyncio.gather(*(pipeline.index_document(doc, session_id) for _ in range(8)))
        count = await db_pool.fetchval(
            "SELECT count(*) FROM context_chunks WHERE session_id = $1 AND document_source = $2",
            session_id,
            source,
        )
        assert count == 1
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session_id)


async def test_reindex_deletes_previous_generation_before_insert():
    pipeline = RAGPipeline(config=RAGConfig(enable_embeddings=False, enable_reranking=False))
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value="DELETE 2")
    mock_db.fetch = AsyncMock(return_value=[])
    from tests.support.database import attach_connection

    attach_connection(mock_db)
    doc = Document(content="alpha beta gamma delta", source="docs/note.md", doc_type="text")
    with patch("machina.rag.pipeline.db", mock_db):
        await pipeline.index_document(doc, uuid.uuid4())
    delete_call = mock_db.execute.await_args
    assert "DELETE FROM context_chunks" in delete_call.args[0]
    assert delete_call.args[2] == "docs/note.md"


# --- W15: embedding-space scan cache -----------------------------------------


async def test_embedding_space_scan_cached_per_session_and_model():
    pipeline = RAGPipeline(config=RAGConfig(enable_embeddings=False))
    mock_db = AsyncMock()
    mock_db.fetch = AsyncMock(return_value=[])
    session_id = uuid.uuid4()
    with patch("machina.rag.pipeline.db", mock_db):
        await pipeline._enforce_embedding_space(session_id, "text-embedding-3-small")
        await pipeline._enforce_embedding_space(session_id, "text-embedding-3-small")
        assert mock_db.fetch.await_count == 1
        await pipeline._enforce_embedding_space(uuid.uuid4(), "text-embedding-3-small")
        assert mock_db.fetch.await_count == 2
        # Re-index invalidates the verdict for this session.
        pipeline._invalidate_search_cache(session_id)
        await pipeline._enforce_embedding_space(session_id, "text-embedding-3-small")
        assert mock_db.fetch.await_count == 3


async def test_embedding_space_cache_expires():
    pipeline = RAGPipeline(config=RAGConfig(enable_embeddings=False))
    pipeline._embedding_space_cache_ttl = 0.0
    mock_db = AsyncMock()
    mock_db.fetch = AsyncMock(return_value=[])
    session_id = uuid.uuid4()
    with patch("machina.rag.pipeline.db", mock_db):
        await pipeline._enforce_embedding_space(session_id, "m")
        await pipeline._enforce_embedding_space(session_id, "m")
    assert mock_db.fetch.await_count == 2
