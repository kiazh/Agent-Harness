#!/usr/bin/env python
"""Audit finding: repeated reranking indices are accepted; [0,0,0] returns
the same memory three times (lower impact: production callers do not inject
an LLM reranker today)."""
import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

import machina.memory.retriever as retriever_module
from machina.memory.models import MemoryEntry, RetrievedMemory
from machina.memory.retriever import MemoryRetriever

candidates = [
    RetrievedMemory(
        memory=MemoryEntry(
            id=uuid.uuid4(), session_id=None, agent_id="harness",
            content=f"memory-{i}", category="fact",
        ),
        score=1.0 - i * 0.1,
        source="dense",
    )
    for i in range(3)
]


async def fake_complete(*args, **kwargs):
    return SimpleNamespace(content="[0,0,0]")


retriever_module.usage_store.complete_call = fake_complete


async def main() -> None:
    retriever = MemoryRetriever(llm_provider=object())
    out = await retriever._rerank("query", candidates, uuid.uuid4(), "harness")
    counts: dict = {}
    for rm in out:
        counts[str(rm.memory.id)] = counts.get(str(rm.memory.id), 0) + 1
    first_id = str(candidates[0].memory.id)
    print("input candidate ids:", [str(c.memory.id)[:8] for c in candidates])
    print("output ids:", [str(rm.memory.id)[:8] for rm in out])
    print(f"output length: {len(out)}; candidate 0 repeated: {counts.get(first_id, 0)}x")
    confirmed = counts.get(first_id, 0) == 3
    print("CLAIM CONFIRMED (duplicate indices duplicated the memory)"
          if confirmed else "CLAIM NOT REPRODUCED")
    sys.exit(0 if confirmed else 1)


asyncio.run(main())
