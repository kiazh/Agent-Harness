"""Embedding backfills keep vector identity and payload metadata together."""

import msgpack
import pytest

from ah.rag.pipeline import RAGConfig, RAGPipeline
from tests.test_deep_rag_scope import ChunkRows, QueryEmbedder


class BackfillRows(ChunkRows):
    async def fetch(self, query, *args):
        if "embedding IS NULL" in query:
            assert args == (self.session_id,)
            return [row.copy() for row in self.rows if row["embedding"] is None]
        return await super().fetch(query, *args)

    async def executemany(self, query, records):
        assert "UPDATE context_chunks" in query and "embedding = $1" in query
        for record in records:
            if "payload_msgpack = $3" in query:
                embedding, search_text, payload, row_id = record
            else:
                embedding, search_text, row_id = record
                payload = None
            row = next(row for row in self.rows if row["id"] == row_id)
            row["embedding"] = embedding
            row["search_text"] = search_text
            if payload is not None:
                row["payload_msgpack"] = payload


@pytest.fixture
def rows(monkeypatch):
    boundary = BackfillRows()
    monkeypatch.setattr("ah.rag.pipeline.db", boundary)
    return boundary


@pytest.mark.parametrize(
    ("model", "original_model"),
    [
        ("text-embedding-3-small", "keyword-only"),
        ("text-embedding-ada-002", "keyword-only"),
        ("text-embedding-ada-002", None),
    ],
)
async def test_backfilled_documents_are_searchable_in_the_actual_embedding_model(
    rows, model, original_model
):
    metadata = {"labels": ["review"], "source": "notes.md"}
    if original_model is not None:
        metadata.update(embedding_model=original_model, embedding_dims=0)
    row = rows.add(
        dense=False,
        payload={"text": "shared query", "source": "notes.md", "metadata": metadata},
    )
    pipe = RAGPipeline(embedder=QueryEmbedder(model), config=RAGConfig(enable_hybrid_search=False))

    assert await pipe.index_session_context(rows.session_id) == [row["id"]]
    results = await pipe.search("shared query", rows.session_id, rerank=False)

    assert len(results) == 1
    assert results[0].chunk.payload == {
        "text": "shared query",
        "source": "notes.md",
        "metadata": {
            "labels": ["review"],
            "source": "notes.md",
            "embedding_model": model,
            "embedding_dims": 1536,
        },
    }


async def test_failed_backfill_keeps_keyword_only_metadata_with_null_vectors(rows):
    row = rows.add(
        dense=False,
        payload={"text": "shared query", "metadata": {"labels": ["review"]}},
    )
    embedder = QueryEmbedder("text-embedding-ada-002", keyword_only=True)
    pipe = RAGPipeline(embedder=embedder)

    await pipe.index_session_context(rows.session_id)
    results = await pipe.search("shared query", rows.session_id, rerank=False)

    assert row["embedding"] is None
    assert len(results) == 1
    assert results[0].chunk.payload["metadata"] == {
        "labels": ["review"],
        "embedding_model": "keyword-only",
        "embedding_dims": 0,
    }
    assert msgpack.unpackb(row["payload_msgpack"], raw=False)["text"] == "shared query"
