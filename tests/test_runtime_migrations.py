"""Existing installations receive required additive runtime migrations."""

import uuid

import pytest

from tests.support.schema import isolated_schema


@pytest.mark.db
async def test_runtime_upgrade_is_idempotent_and_preserves_existing_sessions(db_pool):
    async with isolated_schema(db_pool) as (_, database):
        session_id = uuid.uuid4()
        await database.execute(
            "INSERT INTO sessions(id, title) VALUES ($1, 'existing session')", session_id
        )
        for column in (
            "parent_session_id",
            "delegation_authority",
            "ancestor_session_ids",
            "ancestor_agent_ids",
        ):
            await database.execute(f"ALTER TABLE sessions DROP COLUMN IF EXISTS {column}")
        for column in ("ancestor_session_ids", "ancestor_agent_ids"):
            await database.execute(f"ALTER TABLE llm_usage DROP COLUMN IF EXISTS {column}")

        await database.initialize_runtime_schema()
        await database.initialize_runtime_schema()
        row = await database.fetchrow(
            "SELECT title, parent_session_id, delegation_authority, ancestor_session_ids, ancestor_agent_ids FROM sessions WHERE id = $1",
            session_id,
        )
        assert row["title"] == "existing session"
        assert row["parent_session_id"] is None
        assert row["ancestor_session_ids"] == []
        assert row["ancestor_agent_ids"] == []
        assert (
            await database.fetchval(
                "SELECT count(*) FROM llm_usage WHERE ancestor_session_ids = '{}'::uuid[]"
            )
            == 0
        )


@pytest.mark.db
async def test_runtime_upgrade_adds_memory_embedding_space(db_pool):
    """Existing installations gain memories.embedding_space at boot (H08).

    ``schema.sql`` never re-runs on an initialized database, so
    ``initialize_runtime_schema`` must apply the same idempotent DDL the
    store would otherwise apply lazily.
    """
    async with isolated_schema(db_pool) as (schema, database):
        await database.execute("DROP INDEX IF EXISTS idx_memories_embedding_space")
        await database.execute("ALTER TABLE memories DROP COLUMN IF EXISTS embedding_space")

        await database.initialize_runtime_schema()
        await database.initialize_runtime_schema()

        column_type = await database.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='memories' "
            "AND column_name='embedding_space'",
            schema,
        )
        assert column_type == "text"
        assert (
            await database.fetchval(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname=$1 AND indexname='idx_memories_embedding_space'",
                schema,
            )
            == "idx_memories_embedding_space"
        )
