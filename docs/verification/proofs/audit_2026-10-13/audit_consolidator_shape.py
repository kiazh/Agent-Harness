#!/usr/bin/env python
"""Audit finding: valid JSON of the wrong shape ({}) becomes an empty
successful extraction and advances the checkpoint; malformed JSON does not."""
import asyncio
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

from machina.core.context import context_manager
from machina.core.usage import usage_store
from machina.memory.consolidator import MemoryConsolidator

chunk = SimpleNamespace(
    chunk_type="user_message",
    payload={"content": "Remember: user prefers dark mode"},
    created_at=datetime.now(UTC),
    id=uuid.uuid4(),
    token_count=5,
)


async def fake_get_chunks_since(session_id, since_time, since_id, limit):
    return [chunk]


context_manager.get_chunks_since = fake_get_chunks_since


class FakeStore:
    async def search(self, agent_id, limit):
        return []

    async def search_by_embedding(self, **kwargs):
        return []

    async def add(self, **kwargs):
        raise AssertionError("no writes expected")


async def main() -> None:
    consolidator = MemoryConsolidator(llm_provider=object(), store=FakeStore())

    async def empty_object(*args, **kwargs):
        return SimpleNamespace(content="{}")

    usage_store.complete_call = empty_object
    result = await consolidator._consolidate_session_impl(uuid.uuid4(), "harness")
    print(f"LLM returns '{{}}'      -> status={result.status!r} "
          f"checkpoint={'set' if result.checkpoint is not None else 'None'}")

    async def malformed(*args, **kwargs):
        return SimpleNamespace(content="not json at all")

    usage_store.complete_call = malformed
    result_bad = await consolidator._consolidate_session_impl(uuid.uuid4(), "harness")
    print(f"LLM returns 'not json' -> status={result_bad.status!r} "
          f"checkpoint={'set' if result_bad.checkpoint is not None else 'None'}")

    confirmed = (
        result.status == "empty"
        and result.checkpoint is not None
        and result_bad.status == "failed"
        and result_bad.checkpoint is None
    )
    print("CLAIM CONFIRMED (wrong-shape {} advances the checkpoint)"
          if confirmed else "CLAIM NOT REPRODUCED")
    sys.exit(0 if confirmed else 1)


asyncio.run(main())
