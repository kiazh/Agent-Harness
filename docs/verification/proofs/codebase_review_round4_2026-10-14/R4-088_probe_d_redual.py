"""R4-088(d) RED probe (pre-fix): a dual retrieval-branch failure returns [].

Runs the real RAGPipeline -> HybridSearch.search path against a SQL boundary
where BOTH retrieval queries raise. The unfixed code logs the failure and
returns an empty list — indistinguishable from a legitimately empty index.
"""

import asyncio
import uuid

from machina.rag.embedder import Embedder
from machina.rag.pipeline import RAGConfig, RAGPipeline


class _FixedEmbedder(Embedder):
    model_name = "text-embedding-3-small"

    async def embed(self, text):
        return [1.0] + [0.0] * 1535

    async def embed_batch(self, texts):
        return [await self.embed(text) for text in texts]

    @property
    def dimensions(self):
        return 1536


class _FailBoth:
    session_id = uuid.uuid4()

    async def fetch(self, query, *args):
        if "embedding <=>" in query:
            raise RuntimeError("dense retrieval backend down")
        if "ts_rank" in query:
            raise RuntimeError("keyword retrieval backend down")
        return []


async def main():
    import machina.rag.pipeline as pipeline_module

    boundary = _FailBoth()
    pipeline_module.db = boundary
    pipe = RAGPipeline(
        embedder=_FixedEmbedder(),
        config=RAGConfig(enable_embeddings=True, enable_reranking=False),
    )
    results = await pipe.search("shared query", boundary.session_id, rerank=False)
    print(f"pre-fix dual-failure search returned: {results!r}")
    assert results == [], "probe expected the unfixed confident-empty behavior"
    print("RED: [] returned for a dual retrieval outage (indistinguishable from no matches)")


asyncio.run(main())
