#!/usr/bin/env python
"""Audit finding: rotating one nonempty Cohere key to another keeps the old
client — rag.get_rag_pipeline tracks only key PRESENCE (bool), not identity.

Fake CohereReranker records the key at construction (CohereReranker.api_key
in the real class); _cohere_configured reads the same mutable holder.
"""
import asyncio
import sys
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

import machina.rag.reranker as reranker_module
import machina.tools.rag as rag

state = {"key": "cohere-key-ONE"}

rag._cohere_configured = lambda: bool(state["key"])


class FakeCohereReranker:
    def __init__(self, *args, **kwargs) -> None:
        self.api_key = state["key"]


reranker_module.CohereReranker = FakeCohereReranker


async def main() -> None:
    pipeline_1 = await rag.get_rag_pipeline()
    key_1 = getattr(pipeline_1._reranker, "api_key", None)
    print(f"pipeline 1 built with key: {key_1}")

    state["key"] = "cohere-key-TWO"
    pipeline_2 = await rag.get_rag_pipeline()
    key_2 = getattr(pipeline_2._reranker, "api_key", None)
    print(f"after rotation to {state['key']!r}")
    print(f"same pipeline object reused: {pipeline_1 is pipeline_2}")
    print(f"pipeline 2 reranker still uses key: {key_2}")

    stale = pipeline_1 is pipeline_2 and key_2 == "cohere-key-ONE"
    print("CLAIM CONFIRMED (old client retained after key rotation)"
          if stale else "CLAIM NOT REPRODUCED")
    sys.exit(0 if stale else 1)


asyncio.run(main())
