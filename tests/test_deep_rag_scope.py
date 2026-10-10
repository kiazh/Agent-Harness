"""Exercise RAG scope and embedding safety through real retrieval components."""

import uuid
from datetime import UTC, datetime

import msgpack
import pytest

from ah.rag.embedder import Embedder
from ah.rag.pipeline import RAGConfig, RAGPipeline

DEFAULT_MODEL = "text-embedding-3-small"
OTHER_MODEL = "text-embedding-ada-002"
VECTOR = [1.0] + [0.0] * 1535


class QueryEmbedder(Embedder):
    def __init__(self, model=DEFAULT_MODEL, keyword_only=False, vector=None):
        self._model = model
        self.keyword_only = keyword_only
        self.vector = VECTOR if vector is None else vector

    async def embed(self, text):
        if self.keyword_only:
            raise RuntimeError("embedding provider unavailable")
        return list(self.vector)

    async def embed_batch(self, texts):
        return [await self.embed(text) for text in texts]

    @property
    def dimensions(self):
        return len(self.vector)

    @property
    def model_name(self):
        return self._model


class ChunkRows:
    """Controlled SQL boundary, preserving scope, NULL, and pagination semantics."""

    def __init__(self):
        self.session_id = uuid.uuid4()
        self.rows = []
        self.guard_error = None

    def add(self, model=DEFAULT_MODEL, chunk_type="document", dense=True, payload=None):
        row = {
            "id": uuid.UUID(int=len(self.rows) + 1),
            "session_id": self.session_id,
            "agent_id": "owner",
            "chunk_type": chunk_type,
            "payload_msgpack": msgpack.packb(
                payload
                if payload is not None
                else {
                    "text": "shared query",
                    "metadata": {"embedding_model": model, "embedding_dims": 1536},
                },
                use_bin_type=True,
            ),
            "token_count": 2,
            "embedding": str(VECTOR) if dense else None,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "accessed_at": None,
            "similarity": 0.9,
            "rank": 0.9,
        }
        self.rows.append(row)
        return row

    async def fetch(self, query, *args):
        if "embedding <=>" in query:
            vector, sid, limit, scope = args
            assert len(vector.strip("[]").split(",")) == 1536
            dense = True
        elif "ts_rank" in query:
            sid, text, limit, scope = args
            assert isinstance(text, str)
            dense = False
        else:
            if self.guard_error:
                raise self.guard_error
            assert "payload_msgpack" in query and "embedding IS NOT NULL" in query
            sid = args[0]
            scope = args[1] if "ANY(" in query else ["document"]
            rows = [
                r
                for r in self.rows
                if r["session_id"] == sid
                and r["chunk_type"] in scope
                and r["embedding"] is not None
            ]
            if "LIMIT 25" in query:
                # Current implementation samples distinct full payloads, not models.
                unique = {r["payload_msgpack"]: r for r in reversed(rows)}
                return sorted(unique.values(), key=lambda r: r["id"])[:25]
            if "id >" in query:
                _, _, cursor, limit = args
                rows = [r for r in rows if cursor is None or r["id"] > cursor]
                return sorted(rows, key=lambda r: r["id"])[:limit]
            return rows
        assert sid == self.session_id
        return [
            r
            for r in self.rows
            if r["session_id"] == sid
            and r["chunk_type"] in scope
            and (not dense or r["embedding"] is not None)
        ][:limit]


@pytest.fixture
def rows(monkeypatch):
    boundary = ChunkRows()
    monkeypatch.setattr("ah.rag.pipeline.db", boundary)
    return boundary


def pipeline(mode="hybrid", model=DEFAULT_MODEL, vector=None):
    return RAGPipeline(
        embedder=QueryEmbedder(model, keyword_only=mode == "keyword", vector=vector),
        config=RAGConfig(enable_hybrid_search=mode != "dense"),
    )


@pytest.mark.parametrize("mode", ["hybrid", "dense", "keyword"])
@pytest.mark.parametrize("combined_first", [False, True])
async def test_cached_search_keeps_document_and_combined_scopes_separate(
    rows, mode, combined_first
):
    # Omitting scope from the cache key leaks conversations or hides combined results.
    rows.add()
    rows.add(chunk_type="user_message")
    pipe = pipeline(mode)
    scopes = [(None, {"document"}), (("document", "user_message"), {"document", "user_message"})]
    if combined_first:
        scopes.reverse()
    for scope, expected in scopes:
        results = await pipe.search(
            "shared query", rows.session_id, top_k=3, rerank=False, chunk_types=scope
        )
        assert {result.chunk.chunk_type for result in results} == expected


@pytest.mark.parametrize("mode", ["hybrid", "dense"])
async def test_mixed_model_index_rejects_even_when_query_model_is_present(rows, mode):
    rows.add()
    rows.add(OTHER_MODEL)
    with pytest.raises(ValueError, match="embedding space mismatch"):
        await pipeline(mode).search("shared query", rows.session_id, rerank=False)


async def test_incompatible_model_beyond_initial_payloads_is_rejected(rows):
    # A payload LIMIT can hide the incompatible model after many distinct documents.
    for index in range(300):
        rows.add(payload={"text": str(index), "metadata": {"embedding_model": DEFAULT_MODEL}})
    rows.add(OTHER_MODEL)
    with pytest.raises(ValueError, match="embedding space mismatch"):
        await pipeline().search("shared query", rows.session_id, rerank=False)


async def test_combined_scope_checks_conversation_embedding_models(rows):
    rows.add()
    rows.add(OTHER_MODEL, chunk_type="user_message")
    with pytest.raises(ValueError, match="embedding space mismatch"):
        await pipeline().search(
            "shared query",
            rows.session_id,
            rerank=False,
            chunk_types=("document", "user_message"),
        )


async def test_conversation_scope_ignores_incompatible_documents(rows):
    rows.add(OTHER_MODEL)
    rows.add(chunk_type="user_message")
    results = await pipeline().search(
        "shared query", rows.session_id, rerank=False, chunk_types=("user_message",)
    )
    assert [result.chunk.chunk_type for result in results] == ["user_message"]


async def test_document_scope_ignores_incompatible_conversations(rows):
    rows.add()
    rows.add(OTHER_MODEL, chunk_type="user_message")
    results = await pipeline().search("shared query", rows.session_id, rerank=False)
    assert [result.chunk.chunk_type for result in results] == ["document"]


async def test_null_embedding_rows_do_not_block_keyword_matches(rows):
    rows.add()
    keyword = rows.add("keyword-only", dense=False)
    results = await pipeline().search("shared query", rows.session_id, rerank=False)
    assert {result.chunk.id for result in results} == {rows.rows[0]["id"], keyword["id"]}


async def test_database_error_cannot_skip_embedding_space_verification(rows):
    rows.add(OTHER_MODEL)
    rows.guard_error = RuntimeError("database query failed")
    with pytest.raises((RuntimeError, ValueError), match="database|verify"):
        await pipeline().search("shared query", rows.session_id, rerank=False)


@pytest.mark.parametrize(
    "payload", [b"invalid", {"metadata": []}, {"metadata": {"embedding_model": 9}}]
)
async def test_unreadable_embedding_metadata_cannot_skip_verification(rows, payload):
    row = rows.add()
    row["payload_msgpack"] = (
        payload if isinstance(payload, bytes) else msgpack.packb(payload, use_bin_type=True)
    )
    with pytest.raises(ValueError, match="embedding|metadata"):
        await pipeline().search("shared query", rows.session_id, rerank=False)


@pytest.mark.parametrize("model", [DEFAULT_MODEL, OTHER_MODEL])
async def test_legacy_rows_use_the_documented_default_embedding_model(rows, model):
    rows.add(payload={"text": "shared query"})
    if model == DEFAULT_MODEL:
        results = await pipeline(model=model).search("shared query", rows.session_id, rerank=False)
        assert len(results) == 1
    else:
        with pytest.raises(ValueError, match="embedding space mismatch"):
            await pipeline(model=model).search("shared query", rows.session_id, rerank=False)


async def test_invalid_query_dimension_is_rejected_before_dense_retrieval(rows):
    rows.add()
    with pytest.raises(ValueError, match="embedding dimension mismatch"):
        await pipeline(vector=[1.0]).search("shared query", rows.session_id, rerank=False)


@pytest.mark.parametrize("read", ["fresh", "cached"])
@pytest.mark.parametrize("mutation", ["score", "nested_payload"])
async def test_returned_results_cannot_mutate_cached_rag_snapshot(rows, read, mutation):
    # Sharing SearchResult or nested payload objects at either cache boundary
    # lets one caller corrupt later callers' results without changing the index.
    rows.add(
        payload={
            "text": "shared query",
            "metadata": {
                "embedding_model": DEFAULT_MODEL,
                "embedding_dims": 1536,
                "annotations": {"labels": ["review"]},
            },
        }
    )
    pipe = pipeline("dense")
    results = await pipe.search("shared query", rows.session_id, top_k=1, rerank=False)
    if read == "cached":
        results = await pipe.search("shared query", rows.session_id, top_k=1, rerank=False)
    if mutation == "score":
        results[0].score = -1.0
    else:
        results[0].chunk.payload["metadata"]["annotations"]["labels"].append("unpersisted")

    for _ in range(2):
        later = await pipe.search("shared query", rows.session_id, top_k=1, rerank=False)
        assert later[0].score == 0.9
        assert later[0].chunk.payload["metadata"]["annotations"]["labels"] == ["review"]
