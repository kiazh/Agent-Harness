"""HELD-55 (finding 5): batched history must replay in original event order.

A batch INSERT gives every row the same transaction ``created_at``. Before this
fix, retrieval tie-broke on the random client-side UUID, so a batch's
user/tool/assistant events replayed out of order in resumed prompts, exports and
consolidation. The fix persists a durable ``event_seq`` assigned in original
event order (additive column + runtime migration), keeps ``created_at`` primary
and leaves the UUID (``id``) as the final fallback.
"""

from __future__ import annotations

import uuid

import msgpack
import pytest

import machina.core.context as ctxmod
from machina.core.context import ContextManager
from tests.support.schema import isolated_schema

pytestmark = [pytest.mark.integration, pytest.mark.db]

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="


def _chunk(session_id, label, chunk_type="user_message", images=None):
    payload = {"content": label}
    if images is not None:
        payload["images"] = images
    return {
        "session_id": session_id,
        "agent_id": "harness",
        "chunk_type": chunk_type,
        "payload": payload,
        "token_count": 1,
    }


async def _session(database) -> uuid.UUID:
    return await database.fetchval("INSERT INTO sessions(title) VALUES ('held-55') RETURNING id")


def _contents(chunks):
    return [c.payload.get("content") for c in chunks]


async def test_batch_replays_in_event_order_across_readers(db_pool, monkeypatch):
    """One 8-row batch draws from every reader that consumes replay order."""
    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(ctxmod, "db", database)
        cm = ContextManager()
        sid = await _session(database)

        await cm.add_chunks_batch([_chunk(sid, f"e{i}") for i in range(8)])

        # Premise under test: the whole batch shares one transaction timestamp.
        assert (
            await database.fetchval(
                "SELECT count(DISTINCT created_at) FROM context_chunks WHERE session_id=$1",
                sid,
            )
            == 1
        )

        newest = [f"e{i}" for i in reversed(range(8))]
        oldest = [f"e{i}" for i in range(8)]
        assert [
            r["payload"]["content"] for r in await cm.get_recent_context(sid, limit=8)
        ] == newest
        assert _contents(await cm.get_chunks(sid, limit=8)) == newest
        assert _contents(await cm.get_chunks_since(sid, limit=8)) == oldest
        assert _contents(await cm.get_transcript_chunks(sid)) == newest


async def test_event_seq_is_monotonic_across_batches(db_pool, monkeypatch):
    """Ordinals keep climbing between batches; later batch replays later."""
    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(ctxmod, "db", database)
        cm = ContextManager()
        sid = await _session(database)

        await cm.add_chunks_batch([_chunk(sid, f"a{i}") for i in range(3)])
        await cm.add_chunks_batch([_chunk(sid, f"b{i}") for i in range(3)])

        rows = await database.fetch(
            "SELECT payload_msgpack, event_seq FROM context_chunks "
            "WHERE session_id=$1 ORDER BY event_seq ASC",
            sid,
        )
        seqs = [r["event_seq"] for r in rows]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert [msgpack.unpackb(r["payload_msgpack"], raw=False)["content"] for r in rows] == [
            "a0",
            "a1",
            "a2",
            "b0",
            "b1",
            "b2",
        ]


async def test_image_bearing_batch_still_ordered(db_pool, monkeypatch):
    """The image-cache batch variant (renumbered params) preserves order."""
    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(ctxmod, "db", database)
        cm = ContextManager()
        sid = await _session(database)

        batch = [_chunk(sid, f"e{i}") for i in range(4)]
        batch[1]["payload"]["images"] = [{"mediaType": "image/png", "data": PNG}]
        await cm.add_chunks_batch(batch)

        assert _contents(await cm.get_chunks(sid, limit=8)) == ["e3", "e2", "e1", "e0"]
        assert await cm.get_latest_images(sid, "harness")  # cache row written
        assert (
            await database.fetchval(
                "SELECT count(DISTINCT event_seq) FROM context_chunks WHERE session_id=$1",
                sid,
            )
            == 4
        )


async def test_runtime_migration_adds_and_defaults_event_seq(db_pool):
    """Fresh install defines event_seq; legacy upgrade is idempotent."""
    async with isolated_schema(db_pool) as (schema, database):
        # Fresh schema already carries the additive artefacts.
        assert (
            await database.fetchval(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema=$1 AND table_name='context_chunks' "
                "AND column_name='event_seq'",
                schema,
            )
            == "bigint"
        )
        assert (
            await database.fetchval("SELECT to_regclass($1)", f"{schema}.context_chunks_event_seq")
            is not None
        )

        # Simulate a legacy database: drop column (cascades the owned sequence
        # and index), seed a pre-existing row, then upgrade twice.
        await database.execute(
            f'ALTER TABLE "{schema}".context_chunks DROP COLUMN IF EXISTS event_seq CASCADE'
        )
        await database.execute(f'DROP SEQUENCE IF EXISTS "{schema}".context_chunks_event_seq')
        sid = await _session(database)
        legacy_id = await database.fetchval(
            "INSERT INTO context_chunks(session_id, agent_id, chunk_type, payload_msgpack) "
            "VALUES ($1, 'harness', 'user_message', $2) RETURNING id",
            sid,
            msgpack.packb({"content": "legacy"}, use_bin_type=True),
        )
        assert (
            await database.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema=$1 AND table_name='context_chunks' "
                "AND column_name='event_seq'",
                schema,
            )
            == 0
        )

        await database.initialize_runtime_schema()
        await database.initialize_runtime_schema()
        assert (
            await database.fetchval(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema=$1 AND table_name='context_chunks' "
                "AND column_name='event_seq'",
                schema,
            )
            == "bigint"
        )
        default = await database.fetchval(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='context_chunks' "
            "AND column_name='event_seq'",
            schema,
        )
        assert "context_chunks_event_seq" in default
        assert (
            await database.fetchval("SELECT to_regclass($1)", f"{schema}.context_chunks_event_seq")
            is not None
        )
        assert (
            await database.fetchval(
                "SELECT to_regclass($1)", f"{schema}.idx_context_chunks_session_created_event"
            )
            is not None
        )

        # Existing row survives untouched and carries no ordinal (falls back).
        assert (
            await database.fetchval("SELECT event_seq FROM context_chunks WHERE id=$1", legacy_id)
            is None
        )
        # A new insert draws an ordinal from the restored default.
        new_id = await database.fetchval(
            "INSERT INTO context_chunks(session_id, agent_id, chunk_type, payload_msgpack) "
            "VALUES ($1, 'harness', 'user_message', $2) RETURNING id",
            sid,
            msgpack.packb({"content": "new"}, use_bin_type=True),
        )
        assert (
            await database.fetchval("SELECT event_seq FROM context_chunks WHERE id=$1", new_id)
            is not None
        )


async def test_legacy_rows_keep_uuid_fallback_tiebreak(db_pool, monkeypatch):
    """Rows without an ordinal still order deterministically by id."""
    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(ctxmod, "db", database)
        cm = ContextManager()
        sid = await _session(database)

        ids = []
        for label in ("e0", "e1", "e2", "e3"):
            ids.append(
                await database.fetchval(
                    "INSERT INTO context_chunks"
                    "(session_id, agent_id, chunk_type, payload_msgpack, created_at, event_seq) "
                    "VALUES ($1, 'harness', 'user_message', $2, "
                    "'2020-01-01 00:00:00+00'::timestamptz, NULL) RETURNING id",
                    sid,
                    msgpack.packb({"content": label}, use_bin_type=True),
                )
            )
        expected_ids = sorted((str(i) for i in ids), reverse=True)
        got = await cm.get_chunks(sid, limit=10)
        assert [str(c.id) for c in got] == expected_ids
