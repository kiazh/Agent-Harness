"""S3 retrieval evaluation harness (llamaindex-pattern, offline).

Catalog-polluted fixture queries (tests/fixtures/session_5f625662/
retrieval_eval_queries.json — new file only) measure two llamaindex-style
scores on memory/RAG/recall paths:

- relevancy: does the injected top-1 chunk address the query? (shares a
  content keyword with the query)
- faithfulness: is the injected top-1 the latest real instruction?
  (top-1 preview equals the query's expected instruction verbatim)

Polluters share query terms with HIGH raw scores; the real instruction has
a LOW raw score — naive ranking fails faithfulness, demotion logic rescues
it. Baselines are recorded in test_eval_baseline_recorded; recall tuning in
session_recall.py is allowed ONLY on eval improvement with zero regressions
in test_recall_filters / test_session_recall.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

FIX = Path(__file__).parent / "fixtures" / "session_5f625662" / "retrieval_eval_queries.json"

_STOP = frozenset(
    "a an the to of for and or in on with using use me my please can could would should help i we you it this that is are be do does how want need write build create make implement review code task when only just".split()
)


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.casefold()) if w not in _STOP and len(w) > 2}


def _load():
    return json.loads(FIX.read_text())["queries"]


def _relevant(top: str, query: str) -> int:
    return int(bool(_keywords(top) & _keywords(query)))


def _faithful(top: str, instruction: str) -> int:
    return int(top == instruction)


# --- recall path (SessionRecall.discover, mocked DB) --------------------------


def _recall_rows(query_obj):
    sid = uuid.uuid4()
    now = datetime.now(UTC)
    rows = [
        {
            "session_id": sid,
            "chunk_id": uuid.uuid4(),
            "title": "t",
            "source": "archive",
            "preview": query_obj["catalog"][0],
            "occurred_at": now,
            "score": 0.9,
            "chunk_type": "tool_call",
        },
        {
            "session_id": sid,
            "chunk_id": uuid.uuid4(),
            "title": "t",
            "source": "archive",
            "preview": query_obj["catalog"][1],
            "occurred_at": now,
            "score": 0.8,
            "chunk_type": "assistant_message",
        },
        {
            "session_id": sid,
            "chunk_id": uuid.uuid4(),
            "title": "t",
            "source": "archive",
            "preview": query_obj["instruction"],
            "occurred_at": now,
            "score": 0.1,
            "chunk_type": "user_message",
        },
    ]
    return rows


async def _eval_recall(monkeypatch):
    from machina.core.session_recall import SessionRecall

    scores = []
    for q in _load():
        fetch = AsyncMock(return_value=_recall_rows(q))
        monkeypatch.setattr("machina.core.session_recall.db", SimpleNamespace(fetch=fetch))
        hits = await SessionRecall().discover("harness", q["query"])
        assert hits, q["id"]
        top = hits[0].preview
        scores.append((_relevant(top, q["query"]), _faithful(top, q["instruction"])))
    return scores


# --- memory path (MemoryRetriever keyword search, mocked DB) ------------------


def _memory_rows(query_obj):
    now = datetime.now(UTC)
    agent = "harness"
    sid = uuid.uuid4()

    def _row(content, importance):
        return {
            "id": uuid.uuid4(),
            "session_id": sid,
            "agent_id": agent,
            "content": content,
            "category": "fact",
            "importance": importance,
            "created_at": now,
            "last_accessed": now,
            "access_count": 0,
            "embedding": None,
            "explicitly_important": False,
            "base_strength": 1.0,
        }

    # SQL-rank order: polluters first (naive rank dominated by shared terms).
    return [
        _row(query_obj["catalog"][0], 0.9),
        _row(query_obj["catalog"][1], 0.8),
        _row(query_obj["instruction"], 0.1),
    ]


async def _eval_memory(monkeypatch):
    import machina.memory.retriever as retriever_mod
    from machina.memory.retriever import MemoryRetriever

    scores = []
    for q in _load():
        fetch = AsyncMock(return_value=_memory_rows(q))
        monkeypatch.setattr(retriever_mod, "db", SimpleNamespace(fetch=fetch))
        res = await MemoryRetriever().retrieve(
            q["query"], agent_id="harness", persona_enabled=False
        )
        assert res, q["id"]
        top = res[0].memory.content
        scores.append((_relevant(top, q["query"]), _faithful(top, q["instruction"])))
    return scores


# --- RAG path (pipeline orchestration over a canned polluted backend) ---------


class _FakeSearch:
    def __init__(self, results):
        self._results = results

    async def search_text(self, **kwargs):
        return list(self._results)


async def _eval_rag():
    import uuid as _uuid

    from machina.core.models import ContextChunk
    from machina.rag.pipeline import RAGConfig, RAGPipeline
    from machina.rag.search import SearchResult

    scores = []
    for q in _load():
        sid = _uuid.uuid4()

        def _chunk(text):
            return ContextChunk(
                id=_uuid.uuid4(),
                session_id=sid,
                agent_id="harness",
                chunk_type="document",
                payload={"text": text},
            )

        fake = _FakeSearch(
            [
                SearchResult(chunk=_chunk(q["catalog"][0]), score=0.9),
                SearchResult(chunk=_chunk(q["catalog"][1]), score=0.8),
                SearchResult(chunk=_chunk(q["instruction"]), score=0.1),
            ]
        )
        pipe = RAGPipeline(
            search=fake, config=RAGConfig(enable_embeddings=False, enable_reranking=False)
        )
        out = await pipe.search(q["query"], sid, top_k=3)
        assert out, q["id"]
        top = out[0].chunk.payload["text"]
        scores.append((_relevant(top, q["query"]), _faithful(top, q["instruction"])))
    return scores


def _mean(scores, idx):
    return sum(s[idx] for s in scores) / len(scores) if scores else 0.0


async def test_recall_path_eval(monkeypatch):
    scores = await _eval_recall(monkeypatch)
    assert _mean(scores, 0) == 1.0  # relevancy: top-1 always addresses the query
    assert _mean(scores, 1) == 1.0  # faithfulness: catalog demotion rescues the instruction


async def test_memory_path_eval_baseline(monkeypatch):
    scores = await _eval_memory(monkeypatch)
    # Baseline (recorded, not gated): keyword overlap ranks polluters first.
    assert _mean(scores, 0) == 1.0
    print(f"\n[eval] memory relevancy={_mean(scores, 0):.2f} faithfulness={_mean(scores, 1):.2f}")


async def test_rag_path_eval_baseline():
    scores = await _eval_rag()
    # Baseline (recorded, not gated): pipeline preserves retrieval order.
    assert _mean(scores, 0) == 1.0
    print(f"\n[eval] rag relevancy={_mean(scores, 0):.2f} faithfulness={_mean(scores, 1):.2f}")


async def test_eval_baseline_recorded(monkeypatch):
    recall = await _eval_recall(monkeypatch)
    memory = await _eval_memory(monkeypatch)
    rag = await _eval_rag()
    report = {
        "recall": {"relevancy": _mean(recall, 0), "faithfulness": _mean(recall, 1)},
        "memory": {"relevancy": _mean(memory, 0), "faithfulness": _mean(memory, 1)},
        "rag": {"relevancy": _mean(rag, 0), "faithfulness": _mean(rag, 1)},
    }
    print(f"\n[eval-baseline] {report}")
    assert report["recall"] == {"relevancy": 1.0, "faithfulness": 1.0}
    assert report["memory"]["relevancy"] == 1.0
    assert report["rag"]["relevancy"] == 1.0
