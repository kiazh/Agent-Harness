"""Database regressions for context pagination and session forking."""

from __future__ import annotations

import uuid

from ah.core.context import ContextManager, context_manager
from ah.core.serialization import payload_to_msgpack
from ah.core.session import SessionManager


async def test_get_all_chunks_includes_null_timestamp_page(db_pool, monkeypatch):
    """A NULL timestamp at the page boundary must not truncate the snapshot."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ('harness', 'null page') RETURNING id"
    )
    try:
        await db_pool.execute(
            """INSERT INTO context_chunks
               (session_id, agent_id, chunk_type, payload_msgpack, created_at)
               SELECT $1, 'harness', 'user_message', $2, NULL
               FROM generate_series(1, 501)""",
            session_id,
            payload_to_msgpack({"content": "page item"}),
        )
        assert len(await ContextManager().get_all_chunks(session_id)) == 501
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", session_id)


async def test_get_chunks_since_advances_from_null_timestamp(db_pool, monkeypatch):
    """A consolidation cursor with a NULL timestamp still excludes old rows."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ('harness', 'null cursor') RETURNING id"
    )
    older_id, newer_id = sorted((uuid.uuid4(), uuid.uuid4()))
    try:
        for chunk_id in (older_id, newer_id):
            await db_pool.execute(
                """INSERT INTO context_chunks
                   (id, session_id, agent_id, chunk_type, payload_msgpack, created_at)
                   VALUES ($1, $2, 'harness', 'user_message', $3, NULL)""",
                chunk_id,
                session_id,
                payload_to_msgpack({"content": "page item"}),
            )
        chunks = await ContextManager().get_chunks_since(
            session_id, since_time=None, since_id=older_id
        )
        assert [chunk.id for chunk in chunks] == [newer_id]
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", session_id)


async def test_fork_preserves_document_source(db_pool, monkeypatch):
    """Forked indexed documents retain their source identity."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.session.db", db_pool)
    manager = SessionManager()
    source = await manager.create(title="document source")
    fork = None
    try:
        await context_manager.add_chunk(
            source.id,
            "harness",
            "document",
            {"text": "A document"},
            document_source="notes/source.md",
        )
        fork = await manager.fork(source.id)
        assert (
            await db_pool.fetchval(
                "SELECT document_source FROM context_chunks WHERE session_id=$1", fork.id
            )
            == "notes/source.md"
        )
        copied = await ContextManager().get_chunks(fork.id, chunk_type="document")
        assert len(copied) == 1
        assert copied[0].document_source == "notes/source.md"
    finally:
        if fork is not None:
            await manager.delete(fork.id)
        await manager.delete(source.id)


async def test_get_chunks_exposes_document_source(db_pool, monkeypatch):
    """A stored document's source is available through the chunk reader."""
    monkeypatch.setattr("ah.core.context.db", db_pool)
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (agent_id, title) VALUES ('harness', 'document read') RETURNING id"
    )
    try:
        added = await context_manager.add_chunk(
            session_id,
            "harness",
            "document",
            {"text": "A document"},
            document_source="notes/source.md",
        )
        assert added.document_source == "notes/source.md"
        read = await ContextManager().get_chunks(session_id, chunk_type="document")
        assert read[0].document_source == "notes/source.md"
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", session_id)
