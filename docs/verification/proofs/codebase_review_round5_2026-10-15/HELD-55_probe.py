"""HELD-55 probe — batched conversation history must replay in event order.

Real multi-chunk batch against a real (isolated) PostgreSQL schema. No writer
logic is mocked: this drives ``ContextManager.add_chunks_batch`` directly and
reads back through the public retrieval paths that resume prompts, exports and
consolidation consume.

Expected (chronological replay) order of a batch inserted e0..eN-1:
  * newest-first readers -> eN-1, eN-2, ..., e0
  * oldest-first reader  -> e0, e1, ..., eN-1

Pre-fix, every row in the batch shares one transaction ``created_at`` and the
tie is broken by the random client-side UUID, so replay is scrambled. The probe
also prints the raw-id order to show the UUID tie-break is the mechanism.

Run with the repository root on PYTHONPATH:
    PYTHONPATH=. python docs/verification/proofs/.../HELD-55_probe.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
import msgpack

import machina.core.context as ctxmod
from machina.core.context import ContextManager
from tests.support.schema import isolated_schema

N = 8


async def main() -> int:
    if not os.environ.get("MACHINA_TEST_DATABASE_URL"):
        print("MACHINA_TEST_DATABASE_URL not set", file=sys.stderr)
        return 2
    admin = await asyncpg.connect(os.environ["MACHINA_TEST_DATABASE_URL"])
    failures = 0
    try:
        async with isolated_schema(admin, initialize=True) as (_schema, database):
            ctxmod.db = database
            cm = ContextManager()
            session_id = await database.fetchval(
                "INSERT INTO sessions(title) VALUES ('HELD-55') RETURNING id"
            )

            batch = [
                {
                    "session_id": session_id,
                    "agent_id": "harness",
                    "chunk_type": "user_message",
                    "payload": {"content": f"e{i}"},
                    "token_count": 1,
                }
                for i in range(N)
            ]
            await cm.add_chunks_batch(batch)

            rows = await database.fetch(
                "SELECT id, created_at, payload_msgpack FROM context_chunks WHERE session_id=$1",
                session_id,
            )
            distinct_ts = {r["created_at"] for r in rows}
            print(f"rows={len(rows)} distinct_created_at={len(distinct_ts)}")
            assert len(distinct_ts) == 1, "probe premise: batch shares one created_at"

            recent = await cm.get_recent_context(session_id, limit=N)
            recent_order = [r["payload"].get("content") for r in recent]
            chunks_order = [
                c.payload.get("content") for c in await cm.get_chunks(session_id, limit=N)
            ]
            since_order = [
                c.payload.get("content")
                for c in await cm.get_chunks_since(session_id, limit=N)
            ]

            expected_newest_first = [f"e{i}" for i in reversed(range(N))]
            expected_oldest_first = [f"e{i}" for i in range(N)]

            print("expected newest-first:", expected_newest_first)
            print("get_recent_context   :", recent_order)
            print("get_chunks           :", chunks_order)
            print("expected oldest-first:", expected_oldest_first)
            print("get_chunks_since     :", since_order)

            by_id = {
                str(r["id"]): msgpack.unpackb(r["payload_msgpack"], raw=False).get("content")
                for r in rows
            }
            id_desc_order = [by_id[k] for k in sorted(by_id, reverse=True)]
            print("raw UUID DESC (proxy):", id_desc_order)

            if recent_order != expected_newest_first:
                print("VERDICT: SCRAMBLED (get_recent_context)")
                failures += 1
            if chunks_order != expected_newest_first:
                print("VERDICT: SCRAMBLED (get_chunks)")
                failures += 1
            if since_order != expected_oldest_first:
                print("VERDICT: SCRAMBLED (get_chunks_since)")
                failures += 1
            if failures == 0:
                print("VERDICT: IN_ORDER")
    finally:
        await admin.close()
    return failures


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) == 0 else 1)
