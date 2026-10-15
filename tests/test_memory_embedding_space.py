"""H08: stored memory vectors must carry an embedding-space identity.

Switching the embedding backend/model with the same stored width used to leave
old vectors in the dense candidate pool: SQL accepted them (dimensions match)
and cosine ranking compared vectors from two incompatible spaces, so
similarity ranking and embedding-based duplicate decisions became unreliable.

These tests pin the fix:

* a model switch excludes foreign-space vectors from dense retrieval;
* rows written before the migration (``embedding_space IS NULL``) stay
  retrievable through the documented compatibility fallback;
* an initialized database without the column receives the additive migration
  lazily before the first embedding-backed write/query (idempotent);
* writes persist the producing space and reads filter on it in SQL.

The M1 follow-ups are pinned at the bottom:

* ``initialize_runtime_schema`` applies the same additive migration at boot;
* the approval gate carries the submission space on ``pending_memories`` and
  completes the vector under the ORIGINAL space after a model switch, with a
  lazy idempotent migration and degraded pre-migration shapes on DDL failure.

RED before the fix (mixing observable / missing keyword), GREEN after.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from machina.memory.store import MemoryStore
from tests.support.schema import isolated_schema

# A canonical 1536-d unit vector: identical floats in both spaces, so any
# cross-space result can only come from the missing space identity.
UNIT_E0 = [1.0] + [0.0] * 1535


class _FakeEmbedder:
    """Minimal embedder stand-in: the space identity is name + width."""

    def __init__(self, model: str, dimensions: int = 1536) -> None:
        self._model = model
        self._dimensions = dimensions

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions


def _use_embedder(monkeypatch, model: str, dimensions: int = 1536) -> None:
    """Point the shared embedder resolution at a fake backend."""
    fake = _FakeEmbedder(model, dimensions)
    monkeypatch.setattr("machina.rag.embedder.get_shared_embedder", lambda: fake)


def _mock_row(**overrides):
    row = {
        "id": uuid.uuid4(),
        "session_id": None,
        "agent_id": "harness",
        "content": "Test memory",
        "category": "fact",
        "importance": 0.5,
        "created_at": datetime.now(),
        "last_accessed": None,
        "access_count": 0,
        "embedding": None,
        "explicitly_important": False,
        "base_strength": 1.0,
        "quarantined": False,
    }
    row.update(overrides)
    return row


# ===========================================================================
# Embedding-space identity helper
# ===========================================================================


def test_space_identity_is_stable_and_distinguishes_spaces():
    """Same backend/model/width maps to one id; model or width changes it."""
    from machina.memory.embeddings import embedding_space_id

    first = embedding_space_id(_FakeEmbedder("model-a"))
    assert first == embedding_space_id(_FakeEmbedder("model-a"))
    assert first != embedding_space_id(_FakeEmbedder("model-b"))
    assert first != embedding_space_id(_FakeEmbedder("model-a", dimensions=768))
    assert first and "model-a" in first


def test_space_identity_is_none_without_embedder(monkeypatch):
    """Keyword-only operation has no space: callers keep pre-H08 behavior."""
    from machina.memory.embeddings import embedding_space_id

    monkeypatch.setattr("machina.rag.embedder.get_shared_embedder", lambda: None)
    assert embedding_space_id() is None


# ===========================================================================
# Mocked SQL-shape contracts
# ===========================================================================


async def test_add_persists_explicit_embedding_space(monkeypatch):
    """A write with an embedding stores the space identifier alongside it."""
    from tests.support.database import attach_connection

    store = MemoryStore()
    mock_db = AsyncMock()
    attach_connection(mock_db)
    mock_db.fetchrow = AsyncMock(return_value=_mock_row())
    executed: list[str] = []

    async def fake_execute(query, *args):
        executed.append(query)
        return "OK"

    mock_db.execute = fake_execute
    with patch("machina.memory.store.db", mock_db):
        entry = await store.add(
            session_id=None,
            agent_id="harness",
            content="spaced memory",
            category="fact",
            embedding=list(UNIT_E0),
            embedding_space="fake:model-a:1536",
        )
    assert entry is not None
    insert_query, *params = mock_db.fetchrow.await_args.args
    assert "embedding_space" in insert_query
    assert params[-1] == "fake:model-a:1536"
    assert any("ALTER TABLE memories" in query for query in executed)


async def test_search_by_embedding_filters_compatible_spaces(monkeypatch):
    """Dense search only compares rows from the same space (plus legacy NULL)."""
    store = MemoryStore()
    mock_db = AsyncMock()
    mock_db.fetch = AsyncMock(return_value=[])
    mock_db.execute = AsyncMock(return_value="OK")
    with patch("machina.memory.store.db", mock_db):
        results = await store.search_by_embedding(
            list(UNIT_E0), embedding_space="fake:model-a:1536"
        )
    assert results == []
    query, *params = mock_db.fetch.await_args.args
    assert "embedding_space = $" in query
    assert "embedding_space IS NULL" in query
    assert "fake:model-a:1536" in params


# ===========================================================================
# Real-database behavior and migration
# ===========================================================================


@pytest.mark.integration
@pytest.mark.db
async def test_model_switch_excludes_foreign_space_vectors(db_pool, monkeypatch):
    """Same width, different model: switching must not mix vector spaces."""
    import machina.memory.identity as identity_module
    import machina.memory.store as store_module

    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(store_module, "db", database)
        monkeypatch.setattr(identity_module, "db", database)
        store = MemoryStore()

        _use_embedder(monkeypatch, "model-a")
        entry_a = await store.add(
            session_id=None,
            agent_id="harness",
            content="Written while model-a was configured",
            category="fact",
            embedding=list(UNIT_E0),
        )
        same_space = await store.search_by_embedding(list(UNIT_E0))
        assert [memory.id for memory, _ in same_space] == [entry_a.id]

        # Same dimensions, different model: a different embedding space.
        _use_embedder(monkeypatch, "model-b")
        entry_b = await store.add(
            session_id=None,
            agent_id="harness",
            content="Written while model-b was configured",
            category="fact",
            embedding=list(UNIT_E0),
        )
        switched = await store.search_by_embedding(list(UNIT_E0))
        assert [memory.id for memory, _ in switched] == [entry_b.id], (
            "vectors from the previous embedding space must not be ranked "
            "against queries from the new one"
        )


@pytest.mark.integration
@pytest.mark.db
async def test_legacy_untagged_rows_stay_retrievable(db_pool, monkeypatch):
    """Rows written before H08 (NULL space) keep working via the fallback."""
    import machina.memory.identity as identity_module
    import machina.memory.store as store_module

    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(store_module, "db", database)
        monkeypatch.setattr(identity_module, "db", database)
        store = MemoryStore()

        legacy_id = uuid.uuid4()
        await database.execute(
            """
            INSERT INTO memories (id, agent_id, content, category, embedding)
            VALUES ($1, 'harness', 'pre-H08 row', 'fact', $2::vector)
            """,
            legacy_id,
            "[1.0," + ",".join("0.0" for _ in range(1535)) + "]",
        )

        _use_embedder(monkeypatch, "model-a")
        entry_a = await store.add(
            session_id=None,
            agent_id="harness",
            content="Tagged row",
            category="fact",
            embedding=list(UNIT_E0),
        )

        results = await store.search_by_embedding(list(UNIT_E0))
        assert {memory.id for memory, _ in results} == {legacy_id, entry_a.id}

        # Foreign-space rows are excluded, but legacy NULL rows keep the
        # documented compatibility fallback under the new space as well.
        _use_embedder(monkeypatch, "model-b")
        switched = await store.search_by_embedding(list(UNIT_E0))
        assert {memory.id for memory, _ in switched} == {legacy_id}


@pytest.mark.integration
@pytest.mark.db
async def test_existing_database_gains_embedding_space_on_first_use(db_pool, monkeypatch):
    """An initialized pre-H08 database is upgraded additively and idempotently."""
    import machina.memory.identity as identity_module
    import machina.memory.store as store_module

    async with isolated_schema(db_pool) as (schema, database):
        # Simulate an existing installation that predates H08.
        await database.execute("DROP INDEX IF EXISTS idx_memories_embedding_space")
        await database.execute("ALTER TABLE memories DROP COLUMN IF EXISTS embedding_space")

        legacy_id = uuid.uuid4()
        await database.execute(
            """
            INSERT INTO memories (id, agent_id, content, category, embedding)
            VALUES ($1, 'harness', 'pre-H08 row', 'fact', $2::vector)
            """,
            legacy_id,
            "[1.0," + ",".join("0.0" for _ in range(1535)) + "]",
        )

        monkeypatch.setattr(store_module, "db", database)
        monkeypatch.setattr(identity_module, "db", database)
        _use_embedder(monkeypatch, "model-a")

        # First embedding-backed operation applies the additive migration.
        store = MemoryStore()
        entry = await store.add(
            session_id=None,
            agent_id="harness",
            content="First write after upgrade",
            category="fact",
            embedding=list(UNIT_E0),
        )

        column_type = await database.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='memories' AND column_name='embedding_space'",
            schema,
        )
        assert column_type == "text"
        tagged = await database.fetchval(
            "SELECT embedding_space FROM memories WHERE id=$1", entry.id
        )
        assert tagged and "model-a" in tagged
        assert (
            await database.fetchval("SELECT embedding_space FROM memories WHERE id=$1", legacy_id)
            is None
        )

        # The migration stays idempotent for later processes/instances.
        second = MemoryStore()
        repeat = await second.search_by_embedding(list(UNIT_E0))
        assert {memory.id for memory, _ in repeat} == {legacy_id, entry.id}


@pytest.mark.integration
@pytest.mark.db
async def test_caller_transaction_write_applies_migration(db_pool, monkeypatch):
    """The approval-style write (caller connection) migrates in its transaction."""
    import machina.memory.identity as identity_module
    import machina.memory.store as store_module

    async with isolated_schema(db_pool) as (_schema, database):
        await database.execute("DROP INDEX IF EXISTS idx_memories_embedding_space")
        await database.execute("ALTER TABLE memories DROP COLUMN IF EXISTS embedding_space")

        monkeypatch.setattr(store_module, "db", database)
        monkeypatch.setattr(identity_module, "db", database)
        _use_embedder(monkeypatch, "model-a")
        store = MemoryStore()

        async with database.acquire() as conn:
            async with conn.transaction():
                entry = await store.add(
                    session_id=None,
                    agent_id="harness",
                    content="approval-style write",
                    category="fact",
                    embedding=list(UNIT_E0),
                    connection=conn,
                )

        tagged = await database.fetchval(
            "SELECT embedding_space FROM memories WHERE id=$1", entry.id
        )
        assert tagged and "model-a" in tagged
        # DDL applied inside a caller transaction is not memoized; the next
        # durable operation still resolves (idempotent DDL) and sees the row.
        repeat = await store.search_by_embedding(list(UNIT_E0))
        assert [memory.id for memory, _ in repeat] == [entry.id]


async def test_migration_failure_degrades_to_pre_h08_shapes(monkeypatch):
    """A database where the additive DDL cannot run keeps today's behavior."""
    from tests.support.database import attach_connection

    store = MemoryStore()
    mock_db = AsyncMock()
    attach_connection(mock_db)
    mock_db.fetchrow = AsyncMock(return_value=_mock_row())

    async def boom(query, *args):
        if "ALTER TABLE memories" in query:
            raise RuntimeError("permission denied for DDL")
        return "OK"

    mock_db.execute = boom
    with patch("machina.memory.store.db", mock_db):
        entry = await store.add(
            session_id=None,
            agent_id="harness",
            content="degraded write",
            category="fact",
            embedding=list(UNIT_E0),
            embedding_space="fake:model-a:1536",
        )
    assert entry is not None
    insert_query, *params = mock_db.fetchrow.await_args.args
    assert "embedding_space" not in insert_query
    assert params[-1] is not None  # the embedding itself is still stored

    mock_db.fetch = AsyncMock(return_value=[{**_mock_row(), "similarity": 0.9}])
    with patch("machina.memory.store.db", mock_db):
        results = await store.search_by_embedding(
            list(UNIT_E0), embedding_space="fake:model-a:1536"
        )
    assert len(results) == 1
    query, *_ = mock_db.fetch.await_args.args
    assert "embedding_space" not in query


# ===========================================================================
# Approval gate: pending rows keep the space of their submitted vector
# (M1 follow-up: submission under space A, approval after a switch to B)
# ===========================================================================


def _pending_row(**overrides):
    row = {
        "id": uuid.uuid4(),
        "content": "Pending memory",
        "category": "fact",
        "importance": 0.5,
        "agent_id": "harness",
        "session_id": None,
        "redactions": [],
        "status": "pending",
        "created_at": datetime.now(),
        "explicitly_important": False,
        "base_strength": 1.0,
        "embedding": None,
    }
    row.update(overrides)
    return row


async def test_submit_persists_embedding_space(monkeypatch):
    """A submitted vector persists the space of the producing embedder."""
    from machina.core.serialization import embedding_to_str
    from machina.memory.approval import MemoryApprovalGate
    from tests.support.database import attach_connection

    gate = MemoryApprovalGate(enabled=True)
    mock_db = AsyncMock()
    attach_connection(mock_db)
    _use_embedder(monkeypatch, "model-a")
    executed: list[tuple[str, tuple]] = []

    async def fake_execute(query, *args):
        executed.append((query, args))
        return "INSERT 0 1"

    mock_db.execute = fake_execute
    with patch("machina.memory.approval.db", mock_db):
        pending = await gate.submit(
            content="Submitted while model-a was configured",
            category="fact",
            embedding=list(UNIT_E0),
        )
    assert pending is not None
    insert_query, insert_args = next(
        (query, args) for query, args in executed if "INSERT INTO pending_memories" in query
    )
    assert "embedding_space" in insert_query
    assert "model-a" in str(insert_args[-2])
    # The vector itself remains the trailing bound parameter.
    assert insert_args[-1] == embedding_to_str(list(UNIT_E0))
    assert any("ALTER TABLE pending_memories" in query for query, _ in executed)


async def test_approve_passes_submission_space_through(monkeypatch):
    """Completion forwards the pending row's space into the durable write."""
    from machina.core.serialization import embedding_to_str
    from machina.memory.approval import MemoryApprovalGate
    from tests.support.database import attach_connection

    gate = MemoryApprovalGate(enabled=True)
    mock_db = AsyncMock()
    attach_connection(mock_db)
    row = _pending_row(
        embedding=embedding_to_str(list(UNIT_E0)),
        embedding_space="fake:model-a:1536",
    )
    mock_db.fetchrow = AsyncMock(return_value=row)
    mock_store = AsyncMock()
    mock_store.add = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
    with (
        patch("machina.memory.approval.db", mock_db),
        patch("machina.memory.approval.memory_store", mock_store),
    ):
        await gate.approve(row["id"])
    select_query = mock_db.fetchrow.await_args.args[0]
    assert "embedding_space" in select_query
    assert mock_store.add.await_args.kwargs["embedding_space"] == "fake:model-a:1536"


@pytest.mark.integration
@pytest.mark.db
async def test_pending_submission_keeps_original_space_across_model_switch(db_pool, monkeypatch):
    """A vector submitted under space A is completed with A, not the current B."""
    import machina.memory.approval as approval_module
    import machina.memory.identity as identity_module
    import machina.memory.store as store_module
    from machina.memory.approval import MemoryApprovalGate

    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(store_module, "db", database)
        monkeypatch.setattr(identity_module, "db", database)
        monkeypatch.setattr(approval_module, "db", database)
        gate = MemoryApprovalGate(enabled=True)

        _use_embedder(monkeypatch, "model-a")
        pending = await gate.submit(
            content="Submitted while model-a was configured",
            category="fact",
            embedding=list(UNIT_E0),
        )

        # Same width, different model: the configured space changes while the
        # write is still pending.
        _use_embedder(monkeypatch, "model-b")
        memory = await gate.approve(pending.id)
        assert memory is not None
        stored_space = await database.fetchval(
            "SELECT embedding_space FROM memories WHERE id=$1", memory.id
        )
        assert stored_space is not None and "model-a" in stored_space, (
            "approval must complete the vector under its ORIGINAL submission "
            "space, not the embedder configured at approval time"
        )
        pending_space = await database.fetchval(
            "SELECT embedding_space FROM pending_memories WHERE id=$1", pending.id
        )
        assert pending_space == stored_space


@pytest.mark.integration
@pytest.mark.db
async def test_approve_all_keeps_original_space_across_model_switch(db_pool, monkeypatch):
    """Batch approval completes every row under its original space."""
    import machina.memory.approval as approval_module
    import machina.memory.identity as identity_module
    import machina.memory.store as store_module
    from machina.memory.approval import MemoryApprovalGate

    async with isolated_schema(db_pool) as (_schema, database):
        monkeypatch.setattr(store_module, "db", database)
        monkeypatch.setattr(identity_module, "db", database)
        monkeypatch.setattr(approval_module, "db", database)
        gate = MemoryApprovalGate(enabled=True)
        agent_id = f"m1b-approve-all-{uuid.uuid4()}"

        _use_embedder(monkeypatch, "model-a")
        for index in range(2):
            await gate.submit(
                content=f"Pending memory {index}",
                category="fact",
                agent_id=agent_id,
                embedding=list(UNIT_E0),
            )

        _use_embedder(monkeypatch, "model-b")
        assert await gate.approve_all(agent_id=agent_id) == 2

        spaces = await database.fetch(
            "SELECT embedding_space FROM memories WHERE agent_id=$1", agent_id
        )
        assert len(spaces) == 2
        assert all(row["embedding_space"] and "model-a" in row["embedding_space"] for row in spaces)


@pytest.mark.integration
@pytest.mark.db
async def test_existing_database_gains_pending_embedding_space_on_first_use(db_pool, monkeypatch):
    """A pre-migration pending_memories table is upgraded additively and idempotently."""
    import machina.memory.approval as approval_module
    import machina.memory.identity as identity_module
    import machina.memory.store as store_module
    from machina.memory.approval import MemoryApprovalGate

    async with isolated_schema(db_pool) as (schema, database):
        # Simulate an existing installation that predates this follow-up.
        await database.execute("ALTER TABLE pending_memories DROP COLUMN IF EXISTS embedding_space")

        monkeypatch.setattr(store_module, "db", database)
        monkeypatch.setattr(identity_module, "db", database)
        monkeypatch.setattr(approval_module, "db", database)
        _use_embedder(monkeypatch, "model-a")

        # The first embedding-backed gate use applies the additive migration.
        gate = MemoryApprovalGate(enabled=True)
        pending = await gate.submit(
            content="First pending write after upgrade",
            category="fact",
            embedding=list(UNIT_E0),
        )

        column_type = await database.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='pending_memories' "
            "AND column_name='embedding_space'",
            schema,
        )
        assert column_type == "text"
        tagged = await database.fetchval(
            "SELECT embedding_space FROM pending_memories WHERE id=$1", pending.id
        )
        assert tagged and "model-a" in tagged

        # A later gate instance (fresh process/memo) still completes the row
        # under the original space after another config switch.
        _use_embedder(monkeypatch, "model-b")
        second = MemoryApprovalGate(enabled=True)
        memory = await second.approve(pending.id)
        assert memory is not None
        stored_space = await database.fetchval(
            "SELECT embedding_space FROM memories WHERE id=$1", memory.id
        )
        assert stored_space == tagged


async def test_pending_migration_failure_degrades_to_pre_migration_shapes(monkeypatch):
    """Where the additive DDL cannot run, the gate keeps its pre-migration path."""
    from machina.memory.approval import MemoryApprovalGate
    from tests.support.database import attach_connection

    def _failing_db():
        mock_db = AsyncMock()
        attach_connection(mock_db)
        executed: list[tuple[str, tuple]] = []

        async def fake_execute(query, *args):
            executed.append((query, args))
            if "ALTER TABLE pending_memories" in query:
                raise RuntimeError("permission denied for DDL")
            return "OK"

        mock_db.execute = fake_execute
        return mock_db, executed

    gate = MemoryApprovalGate(enabled=True)
    mock_db, executed = _failing_db()
    _use_embedder(monkeypatch, "model-a")
    with patch("machina.memory.approval.db", mock_db):
        pending = await gate.submit(
            content="degraded pending write",
            category="fact",
            embedding=list(UNIT_E0),
        )
    assert pending is not None
    insert_query, insert_args = next(
        (query, args) for query, args in executed if "INSERT INTO pending_memories" in query
    )
    assert "embedding_space" not in insert_query
    assert insert_args[-1] is not None  # the embedding itself is still stored

    second = MemoryApprovalGate(enabled=True)
    mock_db, _ = _failing_db()
    mock_db.fetchrow = AsyncMock(return_value=_pending_row(id=pending.id))
    mock_store = AsyncMock()
    mock_store.add = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
    with (
        patch("machina.memory.approval.db", mock_db),
        patch("machina.memory.approval.memory_store", mock_store),
    ):
        memory = await second.approve(pending.id)
    assert memory is not None
    select_query = mock_db.fetchrow.await_args.args[0]
    assert "embedding" in select_query
    assert "embedding_space" not in select_query
    assert mock_store.add.await_args.kwargs["embedding_space"] is None
