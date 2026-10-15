"""R5-F1: consolidation cursor read+commit must sit inside the serialization lock.

Finding follow-up 1 (consolidation cursor lock scope): ``agent.py`` read
``mem_consolidated_up_to`` BEFORE the consolidator's PostgreSQL advisory lock
and persisted the new checkpoint AFTER that lock released. Two cross-process
workers could therefore read one stale cursor; a newer worker advanced the
watermark past a range the stale worker was still extracting, and the stale
worker then republished an OLDER checkpoint — regressing the watermark.

The primary proof is a real-PostgreSQL adversarial interleaving over a private
``isolated_schema``: a controlled gate parks one worker between its cursor read
and its lock acquisition while two healthy workers advance the cursor, then
releases it. The durable watermark must never move backwards.

Only the provider boundary (LLM completion) is faked; the schema, the advisory
lock, session state and context chunks are real.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import msgpack
import pytest
import pytest_asyncio

from tests.support.schema import isolated_schema


def _agent(consolidator):
    from machina.core.agent import BaseReActAgent

    agent = BaseReActAgent.__new__(BaseReActAgent)
    agent.agent_id = "harness"
    agent.memory_consolidator = consolidator
    return agent


def _plain_consolidator():
    from machina.memory.consolidator import MemoryConsolidator
    from machina.memory.store import MemoryStore

    return MemoryConsolidator(llm_provider=object(), store=MemoryStore())


async def _read_cursor(database, session_id) -> dict | None:
    raw = await database.fetchval("SELECT state_msgpack FROM sessions WHERE id = $1", session_id)
    if not raw:
        return None
    state = msgpack.unpackb(raw, raw=False)
    return state.get("mem_consolidated_up_to")


async def _seed_chunks(database, session_id, count: int) -> list:
    from machina.core.serialization import payload_to_msgpack

    base = datetime.now(UTC) - timedelta(hours=1)
    await database.executemany(
        """INSERT INTO context_chunks
           (session_id, agent_id, chunk_type, payload_msgpack, created_at)
           VALUES ($1, 'harness', 'user_message', $2, $3)""",
        [
            (session_id, payload_to_msgpack({"content": f"chunk {i}"}), base + timedelta(seconds=i))
            for i in range(count)
        ],
    )
    rows = await database.fetch(
        "SELECT id FROM context_chunks WHERE session_id = $1 ORDER BY created_at ASC, id ASC",
        session_id,
    )
    return [row["id"] for row in rows]


@pytest_asyncio.fixture
async def cursor_env(db_pool, monkeypatch):
    """Private schema wired into the agent + consolidator modules.

    A built-in ``MemoryConsolidator`` is the production consolidator under
    test; the only boundary faked is the LLM completion (returns an empty
    extraction so every fully-processed page validly advances the checkpoint).
    """
    async with isolated_schema(db_pool) as (_schema, database):
        import machina.core.context as ctxmod
        import machina.core.session as sessmod
        import machina.core.usage as usagemod
        import machina.memory.consolidator as consmod

        monkeypatch.setattr(ctxmod, "db", database)
        monkeypatch.setattr(sessmod, "db", database)
        monkeypatch.setattr(consmod, "db", database)
        # Two chunks per page makes the ascending-page interleave deterministic.
        monkeypatch.setattr(consmod, "MAX_CHUNKS_PER_CONSOLIDATION", 2)

        async def _fake_complete(*args, **kwargs):
            return SimpleNamespace(content="[]")

        monkeypatch.setattr(usagemod.usage_store, "complete_call", _fake_complete)
        yield database


@pytest.mark.integration
@pytest.mark.db
async def test_stale_worker_cannot_regress_durable_cursor(cursor_env):
    """A worker parked after a stale read must not republish an older cursor.

    Reproduced N=5 times; the durable watermark is monotonic on every run.
    """
    database = cursor_env
    from machina.memory.consolidator import MemoryConsolidator

    for _ in range(5):
        session_id = await database.fetchval(
            "INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id"
        )
        ids = await _seed_chunks(database, session_id, 6)

        # Prime the oldest page -> durable cursor = ids[1].
        await _agent(_plain_consolidator())._consolidate_memories(session_id)
        primed = await _read_cursor(database, session_id)
        assert primed is not None and primed["id"] == str(ids[1])

        class _Gated(MemoryConsolidator):
            """Real consolidator that parks the CALLER just before the lock."""

            def __init__(self, gate, reached):
                super().__init__(llm_provider=object())
                self._gate = gate
                self._reached = reached

            async def consolidate_session(self, *args, **kwargs):
                self._reached.set()
                await self._gate.wait()
                return await super().consolidate_session(*args, **kwargs)

        gate = asyncio.Event()
        reached = asyncio.Event()
        stale = asyncio.create_task(_agent(_Gated(gate, reached))._consolidate_memories(session_id))
        await asyncio.wait_for(reached.wait(), 10)
        # Confirm the stale worker actually observed the pre-advance cursor.
        observed = await _read_cursor(database, session_id)
        assert observed["id"] == str(ids[1]), "stale worker must read before advancing"

        # Two healthy workers advance the durable cursor ids[1] -> ids[3] -> ids[5].
        await _agent(_plain_consolidator())._consolidate_memories(session_id)
        mid = await _read_cursor(database, session_id)
        await _agent(_plain_consolidator())._consolidate_memories(session_id)
        advanced = await _read_cursor(database, session_id)
        assert mid["id"] == str(ids[3])
        assert advanced["id"] == str(ids[5])

        # Release the stale worker; it must not move the watermark backwards.
        gate.set()
        await asyncio.wait_for(stale, 20)
        final = await _read_cursor(database, session_id)
        assert final["id"] == str(ids[5]), (
            f"stale cursor regressed the watermark: expected {ids[5]}, got {final['id']}"
        )


@pytest.mark.integration
async def test_cursor_reader_overrides_since_and_commits_checkpoint(monkeypatch):
    """Contract: ``cursor_reader`` supplies the window; ``checkpoint_writer`` commits it."""
    from unittest.mock import AsyncMock

    import machina.memory.consolidator as consmod
    from machina.core.models import ContextChunk
    from machina.memory.consolidator import MemoryConsolidator

    chunk = ContextChunk(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        agent_id="harness",
        chunk_type="user_message",
        payload={"content": "hello"},
        token_count=1,
        created_at=datetime.now(UTC),
    )
    reads: list[int] = []
    commits: list[dict] = []

    async def cursor_reader():
        reads.append(1)
        return None

    async def checkpoint_writer(checkpoint):
        commits.append(checkpoint)

    class _Ctx:
        async def get_chunks_since(self, *args, **kwargs):
            return [chunk]

        async def get_chunks(self, *args, **kwargs):
            return [chunk]

    class _Store:
        async def search(self, **kwargs):
            return []

    monkeypatch.setattr(consmod, "context_manager", _Ctx())
    monkeypatch.setattr(consmod, "db", SimpleNamespace(connected=False))
    consolidator = MemoryConsolidator(store=_Store())
    consolidator._extract_memories = AsyncMock(return_value=[])

    # A ``since`` that would match nothing proves the reader is authoritative.
    result = await consolidator.consolidate_session(
        uuid.uuid4(),
        "harness",
        since=(datetime.now(UTC) + timedelta(days=1), uuid.uuid4()),
        cursor_reader=cursor_reader,
        checkpoint_writer=checkpoint_writer,
    )

    assert reads == [1], "cursor_reader must be evaluated (overriding since)"
    assert result.status == "empty" and result.checkpoint is not None
    assert commits == [result.checkpoint], "checkpoint must be committed via the writer"
    assert commits[0]["id"] == str(chunk.id)


@pytest.mark.integration
@pytest.mark.db
async def test_same_created_at_batch_straddling_page_is_not_skipped(cursor_env):
    """HELD-55 F1: a batch split by the page LIMIT must not be skipped.

    ``get_chunks_since`` orders by ``(created_at, event_seq NULLS LAST, id)``
    but the cursor predicate/checkpoint used only ``(created_at, id)``. A
    same-``created_at`` batch (one transaction -> identical created_at, random
    UUIDs) straddling the ``LIMIT`` lets the id-max of the processed prefix
    exceed unprocessed ids, which then never satisfy ``(created_at, id) >
    checkpoint``. RED before the 3-key cursor: the second page returns nothing
    and the watermark stalls on the first page's id-max.
    """
    database = cursor_env
    from machina.core.serialization import payload_to_msgpack

    session_id = await database.fetchval(
        "INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id"
    )
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # Ids chosen so max(id of the first page) > id of an unprocessed row:
    # the two earliest events carry the two largest UUIDs.
    rows = [
        (uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"), 1, "e0"),
        (uuid.UUID("ffffffff-ffff-ffff-ffff-fffffffffffe"), 2, "e1"),
        (uuid.UUID("00000000-0000-0000-0000-000000000002"), 3, "e2"),
        (uuid.UUID("00000000-0000-0000-0000-000000000003"), 4, "e3"),
    ]
    for chunk_id, seq, label in rows:
        await database.execute(
            """INSERT INTO context_chunks
               (id, session_id, agent_id, chunk_type, payload_msgpack, created_at, event_seq)
               VALUES ($1, $2, 'harness', 'user_message', $3, $4, $5)""",
            chunk_id,
            session_id,
            payload_to_msgpack({"content": label}),
            base,
            seq,
        )

    agent = _agent(_plain_consolidator())
    await agent._consolidate_memories(session_id)  # page 1 -> e0, e1
    await agent._consolidate_memories(session_id)  # page 2 must reach e2, e3

    final = await _read_cursor(database, session_id)
    assert final is not None
    assert final["id"] == str(rows[3][0]), (
        f"same-created_at page boundary skipped rows: cursor {final['id']} != {rows[3][0]}"
    )
    assert final.get("seq") == 4, "the 3-key cursor must carry the ordinal"


@pytest.mark.integration
@pytest.mark.db
async def test_null_ordinal_checkpoint_does_not_replay_compacted_summary(cursor_env):
    from machina.core.context import NULL_EVENT_SEQ_ASC, context_manager
    from machina.core.serialization import payload_to_msgpack

    database = cursor_env
    sid = await database.fetchval("INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id")
    at = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        (uuid.UUID(int=100), 1, "summary"),
        (uuid.UUID(int=1), None, "legacy-1"),
        (uuid.UUID(int=2), None, "legacy-2"),
    ]
    for chunk_id, seq, label in rows:
        await database.execute(
            "INSERT INTO context_chunks (id, session_id, agent_id, chunk_type, payload_msgpack, created_at, event_seq) "
            "VALUES ($1, $2, 'harness', 'user_message', $3, $4, $5)",
            chunk_id,
            sid,
            payload_to_msgpack({"content": label}),
            at,
            seq,
        )
    agent = _agent(_plain_consolidator())
    for _ in range(4):
        await agent._consolidate_memories(sid)
    cursor = await _read_cursor(database, sid)
    assert cursor["id"] == str(uuid.UUID(int=2))
    assert cursor["seq"] == NULL_EVENT_SEQ_ASC
    assert (
        await context_manager.get_chunks_since(
            sid, since_time=at, since_id=uuid.UUID(int=2), since_seq=cursor["seq"]
        )
        == []
    )
    legacy = await context_manager.get_chunks_since(sid, since_time=at, since_id=uuid.UUID(int=2))
    assert [chunk.id for chunk in legacy] == [uuid.UUID(int=100)]
