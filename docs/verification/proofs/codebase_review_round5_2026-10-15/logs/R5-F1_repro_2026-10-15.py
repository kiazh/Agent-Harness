"""R5-F1 RED: a stale consolidation cursor publishes an out-of-order checkpoint.

Finding follow-up 1 (consolidation cursor lock scope): agent.py reads
``mem_consolidated_up_to`` BEFORE the consolidator's advisory lock and persists
the new checkpoint AFTER the lock releases. Two cross-process workers can
therefore read the same stale cursor; one advances the durable cursor past a
range the other is still extracting, and the stale worker then republishes an
OLDER checkpoint — regressing the watermark.

This script drives the real ``_consolidate_memories`` / ``MemoryConsolidator``
path against a private ``isolated_schema`` (real PostgreSQL + real advisory
lock). Only the provider boundary (LLM completion) is faked. A controllable
gate stalls the stale worker between its cursor read and its lock acquisition,
then releases it after two other workers have advanced the cursor.

Exit 0 (RED reproduced) when the durable cursor ends BELOW the checkpoint the
newer workers already committed; exit 1 (not reproduced) otherwise.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import machina.core.agent as agentmod  # noqa: E402
import machina.core.context as ctxmod  # noqa: E402
import machina.core.session as sessmod  # noqa: E402
import machina.core.usage as usagemod  # noqa: E402
import machina.memory.consolidator as consmod  # noqa: E402
from machina.core.agent import BaseReActAgent  # noqa: E402
from machina.core.serialization import payload_to_msgpack  # noqa: E402
from machina.memory.consolidator import MemoryConsolidator  # noqa: E402
from machina.memory.store import MemoryStore  # noqa: E402
from tests.support.schema import isolated_schema  # noqa: E402


class _StallingConsolidator(MemoryConsolidator):
    """Real consolidator that stalls the *caller* just before the lock.

    Subclasses ``MemoryConsolidator`` so production feature detection (and any
    isinstance-based capability check) treats it as the built-in consolidator.
    """

    def __init__(self, gate: asyncio.Event, reached: asyncio.Event) -> None:
        super().__init__(llm_provider=object(), store=MemoryStore())
        self._gate = gate
        self._reached = reached

    async def consolidate_session(self, *args, **kwargs):
        self._reached.set()
        await self._gate.wait()
        return await super().consolidate_session(*args, **kwargs)


def _agent(consolidator) -> BaseReActAgent:
    agent = BaseReActAgent.__new__(BaseReActAgent)
    agent.agent_id = "harness"
    agent.memory_consolidator = consolidator
    return agent


async def _read_cursor(database, session_id) -> dict | None:
    row = await database.fetchval("SELECT state_msgpack FROM sessions WHERE id = $1", session_id)
    if not row:
        return None
    import msgpack

    state = msgpack.unpackb(row, raw=False)
    return state.get("mem_consolidated_up_to")


async def main() -> int:
    from machina.db.connection import Database

    dsn = os.environ["MACHINA_TEST_DATABASE_URL"]
    administrator = Database(dsn=dsn)
    await administrator.connect()
    try:
        async with isolated_schema(administrator) as (_schema, database):
            ctxmod.db = database
            sessmod.db = database
            consmod.db = database
            consmod.MAX_CHUNKS_PER_CONSOLIDATION = 2

            async def _fake_complete(*args, **kwargs):
                return SimpleNamespace(content="[]")

            usagemod.usage_store.complete_call = _fake_complete

            session_id = await database.fetchval(
                "INSERT INTO sessions (agent_id) VALUES ('harness') RETURNING id"
            )
            base = datetime.now(UTC) - timedelta(hours=1)
            await database.executemany(
                """INSERT INTO context_chunks
                   (session_id, agent_id, chunk_type, payload_msgpack, created_at)
                   VALUES ($1, 'harness', 'user_message', $2, $3)""",
                [
                    (session_id, payload_to_msgpack({"content": f"chunk {i}"}), base + timedelta(seconds=i))
                    for i in range(6)
                ],
            )
            ids = [
                row["id"]
                for row in await database.fetch(
                    "SELECT id FROM context_chunks WHERE session_id = $1 ORDER BY created_at ASC, id ASC",
                    session_id,
                )
            ]

            # Prime: process the oldest page (c1,c2) -> durable cursor c2.
            await _agent(MemoryConsolidator(llm_provider=object(), store=MemoryStore()))._consolidate_memories(
                session_id
            )
            primed = await _read_cursor(database, session_id)
            print(f"primed cursor = {primed}")

            gate = asyncio.Event()
            reached = asyncio.Event()
            stale = asyncio.create_task(
                _agent(_StallingConsolidator(gate, reached))._consolidate_memories(session_id)
            )
            await asyncio.wait_for(reached.wait(), 5)
            # Stale worker has now read cursor c2 and is parked before its lock.
            stale_read = await _read_cursor(database, session_id)
            print(f"stale worker read cursor = {stale_read}")

            # Two healthy workers advance the durable cursor c2 -> c4 -> c6.
            await _agent(MemoryConsolidator(llm_provider=object(), store=MemoryStore()))._consolidate_memories(
                session_id
            )
            mid = await _read_cursor(database, session_id)
            await _agent(MemoryConsolidator(llm_provider=object(), store=MemoryStore()))._consolidate_memories(
                session_id
            )
            advanced = await _read_cursor(database, session_id)
            print(f"healthy workers advanced cursor: {mid} -> {advanced}")

            # Release the stale worker; it republishes its older checkpoint.
            gate.set()
            await asyncio.wait_for(stale, 10)
            final = await _read_cursor(database, session_id)
            print(f"final cursor = {final}")

            expected_newest = str(ids[5])
            regressed = final is not None and final.get("id") != expected_newest
            if regressed:
                print("RED REPRODUCED: stale cursor regressed the durable checkpoint")
                print(f"  expected newest {expected_newest}, got {final.get('id')}")
                return 0
            print("NOT REPRODUCED: durable checkpoint stayed monotonic")
            return 1
    finally:
        await administrator.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
