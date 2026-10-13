"""Retrieval failures must preserve evidence and measurable ranking quality."""

import time
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest

from machina.core.models import ContextChunk
from machina.rag.pipeline import RAGPipeline
from machina.rag.reranker import CohereReranker, IdentityReranker, RerankResult
from machina.rag.search import SearchResult


def candidates():
    sid = uuid.uuid4()
    return sid, [
        SearchResult(
            chunk=ContextChunk(
                id=uuid.uuid4(),
                session_id=sid,
                agent_id="harness",
                chunk_type="document",
                payload={"text": f"evidence {i}", "metadata": {"source": f"doc{i}"}},
                token_count=3,
            ),
            score=0.03 - i * 0.005,
        )
        for i in range(3)
    ]


async def finish(pipeline, sid, results):
    return await pipeline._finish_search(
        results, "query", sid, 2, True, ("test",), time.monotonic(), "none"
    )


async def test_passthrough_preserves_retrieval_scores():
    sid, results = candidates()
    original = [r.score for r in results]
    output = await finish(RAGPipeline(reranker=IdentityReranker()), sid, results)
    assert [r.score for r in output] == original[:2]


@pytest.mark.parametrize(
    "bad",
    [
        [RerankResult(-1, 0.9)],
        [RerankResult(0, 0.9), RerankResult(0, 0.8)],
        [RerankResult(0, float("nan"))],
        [RerankResult(99, 0.9)],
        [],
    ],
)
async def test_invalid_reranking_preserves_candidates(bad):
    sid, results = candidates()
    pipeline = RAGPipeline(reranker=AsyncMock())
    pipeline._reranker.rerank.return_value = bad
    output = await finish(pipeline, sid, results)
    assert [r.chunk.id for r in output] == [r.chunk.id for r in results[:2]]
    assert [r.score for r in output] == pytest.approx([0.03, 0.025])


async def test_malformed_cohere_response_falls_back_without_fake_scores():
    reranker = CohereReranker(api_key="fake")
    await reranker._client.aclose()
    reranker._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"results": [{"index": -1, "relevance_score": 0.9}]}
            )
        ),
        base_url="https://example.test",
    )
    try:
        results = await reranker.rerank("query", ["first", "second"])
        assert [r.index for r in results] == [0, 1]
        assert all(r.score is None for r in results)
    finally:
        await reranker.close()


async def test_document_tool_returns_passage_and_chunk_identity(monkeypatch):
    from machina.tools import rag

    sid, results = candidates()
    results[0].chunk.payload["text"] = (
        "intro " * 45 + "Decisive answer after character 200.\nKeep this heading."
    )
    pipeline = AsyncMock()
    pipeline.search.return_value = results[:1]
    monkeypatch.setattr(rag, "get_rag_pipeline", AsyncMock(return_value=pipeline))
    monkeypatch.setattr(rag, "active_agent_scope", AsyncMock(return_value=("harness", sid)))
    output = await rag.search_documents("query", str(sid))
    assert "Decisive answer after character 200.\nKeep this heading." in output
    assert str(results[0].chunk.id) in output
    assert "truncated" not in output


async def test_document_evaluation_uses_retrieved_sources_and_deduplicates():
    from machina.rag.evaluation import RetrievalCase, evaluate_retrieval

    sid, results = candidates()
    pipeline = AsyncMock()
    pipeline.reranker_identity = lambda: {"model": "identity"}
    pipeline.search.side_effect = [[results[0], results[0], results[1]], []]
    report = await evaluate_retrieval(
        pipeline,
        sid,
        [
            RetrievalCase("ranked", "question", ("doc1", "doc2")),
            RetrievalCase("absent", "unknown", ()),
        ],
        top_k=3,
    )
    row = report["cases"][0]
    assert row["recall_at_k"] == 0.5
    assert row["mrr"] == 1 / 3  # duplicates do not promote rank
    assert row["ndcg_at_k"] == pytest.approx(0.5 / (1 + 1 / __import__("math").log2(3)))
    assert report["no_answer_false_positive_rate"] == 0
    assert report["cases"][1]["recall_at_k"] is None
    assert pipeline.search.await_args_list[0].kwargs["session_id"] == sid


async def test_document_benchmark_real_postgres(db_pool, monkeypatch):
    import json
    from pathlib import Path

    from machina.rag import evaluation
    from machina.rag import pipeline as pipeline_module
    from machina.rag.pipeline import RAGConfig

    monkeypatch.setattr("machina.db.connection.db", db_pool)
    monkeypatch.setattr(pipeline_module, "db", db_pool)
    root = Path(__file__).resolve().parents[1]
    dataset = json.loads((root / "tests/data/rag/machina_docs_v1.json").read_text())
    pipeline = RAGPipeline(config=RAGConfig(enable_embeddings=False, enable_reranking=False))
    report = await evaluation.benchmark_documents(pipeline, dataset, root)
    assert report["means"]["recall_at_k"] == 1.0
    assert report["means"]["mrr"] >= 0.5
    assert report["no_answer_false_positive_rate"] == 0
    assert len(report["document_sha256"]) == 3
    assert (
        await db_pool.fetchval("SELECT count(*) FROM sessions WHERE title = 'RAG evaluation'") == 0
    )


async def test_real_reranker_records_score_provenance():
    sid, results = candidates()
    pipeline = RAGPipeline(reranker=AsyncMock())
    pipeline._reranker.rerank.return_value = [RerankResult(2, 0.98), RerankResult(0, 0.6)]
    output = await finish(pipeline, sid, results)
    assert [r.chunk.id for r in output] == [results[2].chunk.id, results[0].chunk.id]
    assert output[0].score == output[0].rerank_score == 0.98
    assert output[0].rrf_score == results[2].rrf_score


async def test_reranker_exception_preserves_candidates():
    sid, results = candidates()
    pipeline = RAGPipeline(reranker=AsyncMock())
    pipeline._reranker.rerank.side_effect = RuntimeError("offline")
    output = await finish(pipeline, sid, results)
    assert output == results[:2]
    assert all(r.rerank_score is None for r in output)


async def test_whitespace_query_does_not_call_embedder():
    embedder = AsyncMock()
    pipeline = RAGPipeline(embedder=embedder)
    assert await pipeline.search(" \n\t", uuid.uuid4()) == []
    embedder.embed.assert_not_awaited()


async def test_ten_document_passages_fit_agent_message_budget(monkeypatch):
    from machina.tools import rag

    sid, results = candidates()
    results = [
        SearchResult(
            ContextChunk(
                id=uuid.uuid4(),
                session_id=sid,
                agent_id="harness",
                chunk_type="document",
                payload={
                    "text": f"evidence-{i} " + "x" * 10000,
                    "metadata": {"source": "p" * 1000},
                },
            ),
            0.03,
        )
        for i in range(10)
    ]
    pipeline = AsyncMock()
    pipeline.search.return_value = results
    monkeypatch.setattr(rag, "get_rag_pipeline", AsyncMock(return_value=pipeline))
    monkeypatch.setattr(rag, "active_agent_scope", AsyncMock(return_value=("harness", sid)))
    output = await rag.search_documents("q" * 1000, str(sid), top_k=10)
    assert len(output) <= 8500
    assert str(results[-1].chunk.id) in output[:9000]
    assert "evidence-9" in output[:9000]
    assert output.count("passage truncated") == 10


async def test_embedding_failure_is_degraded_and_survives_cache():
    sid, results = candidates()
    embedder = AsyncMock()
    embedder.model_name = "fake-model"
    embedder.embed.side_effect = RuntimeError("offline")
    search = AsyncMock()
    search.search_text.return_value = results
    pipeline = RAGPipeline(embedder=embedder, search=search)
    for _ in range(2):
        output = await pipeline.search("query", sid, top_k=2)
        assert all(r.retrieval_mode == "keyword-only" and r.retrieval_degraded for r in output)
    embedder.embed.assert_awaited_once()
