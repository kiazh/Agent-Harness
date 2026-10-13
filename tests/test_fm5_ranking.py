"""FM5 W1-W4 + N1: authority-aware memory/RAG ranking.

The RF5 audit: keyword scoring ties (match_count/len(keywords)) were broken
by SQL importance ordering, candidates were pre-truncated by importance, and
memory/RAG ranking had no authority/recency demotion. These tests pin:

- catalog/echo content shares query terms, is DEMOTED (never dropped), and
  loses to a real instruction even when the catalog has a 0.9 importance or
  a higher keyword fraction;
- relevance is primary; recency and importance only break ties;
- sparse candidate selection orders by match count before the LIMIT;
- RAG ``_finish_search`` applies the same demotion after RRF/rerank;
- ``session_id`` is documented and pinned as attribution-only (N1).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from machina.core.models import ContextChunk
from machina.memory.models import MemoryEntry
from machina.memory.retriever import MemoryRetriever
from machina.rag.pipeline import RAGConfig, RAGPipeline
from machina.rag.search import SearchResult

INSTRUCTION = "Deploy with read-only scope, do not edit files outside /tmp"
CATALOG = "Skill tdd-guide [0:500/1200]: deploy scope read files workflow steps"
ECHO = "[live] prior | session=5f625662 chunk=9d5f | deploy scope read files echoed catalog text"
AGENTS = "Available agents: rag-pipeline-reviewer review orchestration code no changes"


def _entry(
    content: str,
    importance: float = 0.5,
    *,
    created_at: datetime | None = None,
    last_accessed: datetime | None = None,
) -> MemoryEntry:
    return MemoryEntry(
        id=uuid.uuid4(),
        session_id=None,
        agent_id="harness",
        content=content,
        category="fact",
        importance=importance,
        created_at=created_at or datetime.now(UTC),
        last_accessed=last_accessed,
    )


def _row(entry: MemoryEntry) -> dict:
    return {
        "id": entry.id,
        "session_id": entry.session_id,
        "agent_id": entry.agent_id,
        "content": entry.content,
        "category": entry.category,
        "importance": entry.importance,
        "created_at": entry.created_at,
        "last_accessed": entry.last_accessed,
        "access_count": entry.access_count,
        "embedding": None,
        "explicitly_important": entry.explicitly_important,
        "base_strength": entry.base_strength,
    }


# --- W1/W3: merge ranking ---------------------------------------------------


def test_equal_relevance_tie_broken_by_authority_not_importance():
    retriever = MemoryRetriever()
    catalog = _entry(CATALOG, 0.9)
    instruction = _entry(INSTRUCTION, 0.1)
    merged = retriever._merge_results([], [(catalog, 1.0), (instruction, 1.0)])
    assert merged[0].memory.content == INSTRUCTION
    # Demoted, never dropped.
    assert {m.memory.content for m in merged} == {CATALOG, INSTRUCTION}


def test_echo_of_prior_recall_is_demoted():
    retriever = MemoryRetriever()
    echo = _entry(ECHO, 0.9)
    instruction = _entry(INSTRUCTION, 0.1)
    merged = retriever._merge_results([], [(echo, 1.0), (instruction, 1.0)])
    assert merged[0].memory.content == INSTRUCTION


def test_agents_catalog_is_demoted():
    retriever = MemoryRetriever()
    catalog = _entry(AGENTS, 0.9)
    instruction = _entry("review orchestration code no changes", 0.1)
    merged = retriever._merge_results([], [(catalog, 1.0), (instruction, 1.0)])
    assert merged[0].memory.content == "review orchestration code no changes"


def test_demoted_catalog_with_full_match_loses_to_partial_instruction():
    """A 1.0 keyword catalog must not outrank a 0.75 keyword instruction."""
    retriever = MemoryRetriever()
    catalog = _entry(CATALOG, 0.9)
    instruction = _entry(INSTRUCTION, 0.1)
    merged = retriever._merge_results([], [(catalog, 1.0), (instruction, 0.75)])
    assert merged[0].memory.content == INSTRUCTION


def test_relevance_is_primary_over_recency_and_importance():
    retriever = MemoryRetriever()
    now = datetime.now(UTC)
    strong = _entry("deploy scope read files", 0.1, created_at=now - timedelta(days=30))
    weak = _entry("deploy scope", 0.9, created_at=now)
    merged = retriever._merge_results([], [(weak, 0.5), (strong, 1.0)])
    assert merged[0].memory.content == "deploy scope read files"


def test_recency_breaks_equal_relevance_tie():
    retriever = MemoryRetriever()
    now = datetime.now(UTC)
    old = _entry("deploy scope read files", 0.5, created_at=now - timedelta(days=10))
    new = _entry("deploy scope read files", 0.5, created_at=now)
    merged = retriever._merge_results([], [(old, 1.0), (new, 1.0)])
    assert merged[0].memory is not None
    assert merged[0].memory.created_at == new.created_at


def test_importance_breaks_equal_relevance_and_recency_tie():
    retriever = MemoryRetriever()
    now = datetime.now(UTC)
    low = _entry("deploy scope read files", 0.1, created_at=now)
    high = _entry("deploy scope read files", 0.9, created_at=now)
    merged = retriever._merge_results([], [(low, 1.0), (high, 1.0)])
    assert merged[0].memory.importance == 0.9


# --- W1: end-to-end retrieve over the eval-shaped pollution ------------------


async def test_retrieve_end_to_end_ranks_instruction_first(monkeypatch):
    import machina.memory.retriever as retriever_mod

    now = datetime.now(UTC)
    rows = [
        _row(_entry(CATALOG, 0.9, created_at=now)),
        _row(_entry(ECHO, 0.8, created_at=now)),
        _row(_entry(INSTRUCTION, 0.1, created_at=now)),
    ]
    fetch = AsyncMock(return_value=rows)
    monkeypatch.setattr(retriever_mod, "db", type("DB", (), {"fetch": fetch})())
    res = await MemoryRetriever().retrieve(
        "deploy scope read-only files", agent_id="harness", persona_enabled=False
    )
    assert res
    assert res[0].memory.content == INSTRUCTION


# --- W2: sparse candidate selection -----------------------------------------


async def test_keyword_sql_orders_by_match_count_before_importance():
    retriever = MemoryRetriever()
    mock_db = AsyncMock()
    mock_db.fetch = AsyncMock(return_value=[])
    with patch("machina.memory.retriever.db", mock_db):
        await retriever._keyword_search("deploy scope read files")
    query = mock_db.fetch.call_args[0][0]
    # Relevance (keyword match count) dominates; importance may only tie-break.
    assert "keyword_matches DESC" in query
    assert query.index("keyword_matches DESC") < query.index("importance DESC")
    # ILIKE fallback for partial tokens must stay.
    assert "ILIKE" in query


async def test_keyword_search_overfetches_before_demotion():
    """The SQL LIMIT must leave headroom for Python-side demotion."""
    retriever = MemoryRetriever()
    mock_db = AsyncMock()
    mock_db.fetch = AsyncMock(return_value=[])
    with patch("machina.memory.retriever.db", mock_db):
        await retriever._keyword_search("deploy scope read files", limit=10)
    limit_param = mock_db.fetch.call_args[0][-1]
    assert limit_param >= 20


# --- W4: RAG _finish_search demotion ----------------------------------------


def _search_result(session_id: uuid.UUID, text: str, score: float) -> SearchResult:
    chunk = ContextChunk(
        id=uuid.uuid4(),
        session_id=session_id,
        agent_id="harness",
        chunk_type="document",
        payload={"text": text, "metadata": {}},
    )
    return SearchResult(chunk=chunk, score=score)


async def test_finish_search_demotes_catalog_after_fusion():
    sid = uuid.uuid4()
    results = [
        _search_result(sid, CATALOG, 0.9),
        _search_result(sid, ECHO, 0.8),
        _search_result(sid, INSTRUCTION, 0.1),
    ]
    pipe = RAGPipeline(config=RAGConfig(enable_embeddings=False, enable_reranking=False))
    out = await pipe._finish_search(
        results, "deploy scope read-only files", sid, 3, False, ("k",), time.monotonic(), "x"
    )
    assert [r.chunk.payload["text"] for r in out] == [INSTRUCTION, CATALOG, ECHO]


async def test_rag_search_demotes_catalog_order_end_to_end():
    sid = uuid.uuid4()

    class _FakeSearch:
        async def search_text(self, **kwargs):
            return [
                _search_result(sid, CATALOG, 0.9),
                _search_result(sid, ECHO, 0.8),
                _search_result(sid, INSTRUCTION, 0.1),
            ]

    pipe = RAGPipeline(
        search=_FakeSearch(),
        config=RAGConfig(enable_embeddings=False, enable_reranking=False),
    )
    out = await pipe.search("deploy scope read-only files", sid, top_k=3)
    assert out[0].chunk.payload["text"] == INSTRUCTION
    assert len(out) == 3


# --- N1: session_id semantics ------------------------------------------------


async def test_session_id_is_attribution_only(monkeypatch):
    """N1: session_id never scopes memory retrieval; document + pin that."""
    import machina.memory.retriever as retriever_mod

    other_session = uuid.uuid4()
    entry = _entry("deploy scope read files", 0.5)
    entry.session_id = other_session
    fetch = AsyncMock(return_value=[_row(entry)])
    monkeypatch.setattr(retriever_mod, "db", type("DB", (), {"fetch": fetch})())
    res = await MemoryRetriever().retrieve(
        "deploy scope read files",
        session_id=uuid.uuid4(),  # a different session: must not filter the row out
        agent_id="harness",
        persona_enabled=False,
    )
    assert any(r.memory.session_id == other_session for r in res)
    doc = MemoryRetriever.retrieve.__doc__ or ""
    assert "attribution" in doc.lower()
