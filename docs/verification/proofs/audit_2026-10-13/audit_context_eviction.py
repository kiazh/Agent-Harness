#!/usr/bin/env python
"""Audit finding: the protected-newest-ten eviction subquery protects
documents even though counts/candidates exclude them; two concurrent callers
can evict all active conversations.

Deterministic concurrent fixture: caller A is paused before its candidate
SELECT until caller B (which computed its bounds against the same stale
totals) finishes evicting the first ten conversations. A then evicts the
remaining ten. Uses the dedicated machina_test database only.
"""
import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import msgpack

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

DSN = "postgresql://postgres:postgres@localhost:5432/machina_test"

from machina.db.connection import db as pg
import machina.core.context as context_module
from machina.core.context import ContextManager


async def main() -> None:
    pg.dsn = DSN
    await pg.connect()
    await pg.initialize_schema()

    session_id = uuid.uuid4()
    await pg.execute(
        "INSERT INTO sessions (id, title, agent_id) VALUES ($1, $2, $3)",
        session_id,
        "audit-evict",
        "harness",
    )
    now = datetime.now(UTC)

    async def insert_chunk(kind: str, idx: int, created: datetime, source=None) -> None:
        payload = msgpack.packb({"text": f"{kind} {idx}"}, use_bin_type=True)
        await pg.execute(
            """INSERT INTO context_chunks
               (session_id, agent_id, chunk_type, payload_msgpack, token_count,
                created_at, search_text, document_source)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            session_id,
            "harness",
            kind,
            payload,
            10,
            created,
            f"{kind} {idx}",
            source,
        )

    for i in range(20):  # 20 conversations, oldest
        await insert_chunk("user_message", i, now - timedelta(minutes=100 - i))
    for i in range(10):  # 10 documents, newest
        await insert_chunk("document", i, now - timedelta(minutes=10 - i * 0.5), source=f"doc-{i}.md")

    real_db = context_module.db
    coordinator: dict = {"task_a": None, "paused": False}
    b_done = asyncio.Event()

    class CoordinatedDb:
        def __getattr__(self, name):
            return getattr(real_db, name)

        async def fetch(self, *args, **kwargs):
            if asyncio.current_task() is coordinator["task_a"] and not coordinator["paused"]:
                coordinator["paused"] = True
                await b_done.wait()
            return await real_db.fetch(*args, **kwargs)

    context_module.db = CoordinatedDb()  # type: ignore[assignment]

    task_a = asyncio.create_task(ContextManager().evict_old_chunks(session_id, max_chunks=10))
    coordinator["task_a"] = task_a
    await asyncio.sleep(0)  # let A reach its candidate SELECT and pause
    await asyncio.sleep(0)

    evicted_b = await ContextManager().evict_old_chunks(session_id, max_chunks=10)
    b_done.set()
    evicted_a = await task_a
    context_module.db = real_db  # type: ignore[assignment]

    conversations_left = await pg.fetchval(
        "SELECT COUNT(*) FROM context_chunks WHERE session_id = $1 AND chunk_type <> 'document'",
        session_id,
    )
    documents_left = await pg.fetchval(
        "SELECT COUNT(*) FROM context_chunks WHERE session_id = $1 AND chunk_type = 'document'",
        session_id,
    )
    archived = await pg.fetchval(
        "SELECT COUNT(*) FROM context_archive WHERE session_id = $1", session_id
    )
    print(f"A evicted {evicted_a}, B evicted {evicted_b} (each bound: 10)")
    print(f"conversations left: {conversations_left} (started with 20)")
    print(f"documents left: {documents_left} (protected newest ten)")
    print(f"archived rows for session: {archived}")

    await pg.execute("DELETE FROM context_chunks WHERE session_id = $1", session_id)
    await pg.execute("DELETE FROM context_archive WHERE session_id = $1", session_id)
    await pg.execute("DELETE FROM sessions WHERE id = $1", session_id)
    await pg.close()

    confirmed = int(conversations_left) == 0 and int(archived) == 20
    print("CLAIM CONFIRMED (two callers evicted all 20 conversations)"
          if confirmed else "CLAIM NOT REPRODUCED")
    sys.exit(0 if confirmed else 1)


asyncio.run(main())
