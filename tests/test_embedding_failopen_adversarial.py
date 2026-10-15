"""R4-021 adversarial E2E: a malformed embeddings 200 must never fail open.

The real shipped path is driven end to end — the real ``OpenAIEmbedder``
(request build, cache, response handling), the real shared-embedder factory
and ``machina.memory.embeddings.embed_query``, and the real
``MemoryRetriever.retrieve`` branch selection — with only the network
boundary replaced (``httpx.MockTransport``). Nothing inside the fixed seam
is mocked.

Before the fix (RED run captured in
``docs/verification/proofs/codebase_review_round4_2026-10-14/R4-021_zero_vector.md``):

- a 200 missing ``data`` (or carrying ``"data": []``) made ``embed()``
  fabricate ``[0.0] * dimensions`` and write it to the LRU cache,
- ``embed_query`` returned that zero vector as a successful embedding, so
  ``MemoryRetriever.retrieve`` started a dense search with a directionless
  vector ("poisoned" retrieval),
- a short batch response cached its first row before the strict-zip
  ``ValueError`` fired.

After the fix: a typed ``ProviderError`` (the module's existing error
surface) is raised before anything is cached, the memory boundary degrades
honestly to ``None`` + keyword-only, dense retrieval stays off, and a later
valid response still works.
"""

from __future__ import annotations

import importlib
import json

import httpx
import pytest
import pytest_asyncio

import machina.memory.retriever as retriever_module
import machina.rag.embedder as embedder_module
from machina.core.exceptions import ProviderError
from machina.memory.embeddings import embed_query
from machina.memory.retriever import MemoryRetriever
from machina.rag.embedder import OpenAIEmbedder, close_shared_embedder

config_module = importlib.import_module("machina.core.config")

VALID_VECTOR = [0.11, 0.22, 0.33, 0.44]


class _FakeConfig:
    """Minimal config stand-in: selects the API backend against a stub host."""

    def __init__(self, **values: str) -> None:
        self.values = values

    def get(self, key: str) -> str:
        return self.values.get(key, "")


@pytest_asyncio.fixture(autouse=True)
async def _reset_shared_embedder():
    await close_shared_embedder()
    yield
    await close_shared_embedder()


async def _shared_openai_embedder(
    monkeypatch, responder
) -> tuple[OpenAIEmbedder, list[httpx.Request]]:
    """Real factory + real shared embedder; only the HTTP transport is faked."""
    monkeypatch.setattr(
        config_module,
        "config",
        _FakeConfig(
            embedding_backend="openai",
            embedding_model="test-embed",
            embedding_base_url="http://embeddings.test/v1",
        ),
        raising=False,
    )
    embedder = embedder_module.get_shared_embedder()
    assert isinstance(embedder, OpenAIEmbedder), "factory must build the real API embedder"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responder(request)

    await embedder._client.aclose()
    embedder._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=embedder._base_url
    )
    return embedder, requests


class _RecordingStore:
    """Records which retrieval branches the real retriever starts.

    The store is not the seam under test; it exists to observe whether the
    dense branch was entered and with which query vector.
    """

    def __init__(self) -> None:
        self.dense_queries: list[list[float]] = []

    async def search_by_embedding(self, embedding, agent_id=None, category=None, limit=10):
        self.dense_queries.append(list(embedding))
        return []

    async def batch_update_access(self, ids):  # pragma: no cover - no candidates expected
        raise AssertionError("no candidate should reach access updates")


class _EmptyFetchDb:
    """Stands in for the module-level lazy DB: records keyword FTS probes."""

    def __init__(self) -> None:
        self.fetch_calls = 0

    async def fetch(self, *_args, **_kwargs):
        self.fetch_calls += 1
        return []


# ─── (a)/(b) malformed 200 payloads: typed error, never cached ──────────────


@pytest.mark.parametrize(
    "payload",
    [
        {"model": "test-embed", "usage": {"total_tokens": 3}},  # no "data"
        {"data": []},  # explicit empty list
        {"data": None},
        {"data": {"index": 0, "embedding": [0.1]}},  # wrong container type
        {"data": "oops"},
        [],  # non-object JSON body
    ],
    ids=["no-data-key", "empty-data-list", "null-data", "dict-data", "string-data", "list-body"],
)
async def test_malformed_200_raises_typed_error_and_never_caches(monkeypatch, payload):
    embedder, requests = await _shared_openai_embedder(
        monkeypatch, lambda request: httpx.Response(200, json=payload)
    )

    with pytest.raises(ProviderError, match="usable vector"):
        await embedder.embed("poison attempt")

    # Nothing fabricated, nothing cached, and the memory boundary degrades
    # honestly instead of returning a "successful" zero-vector embedding.
    assert embedder.cache_size == 0
    assert await embed_query("poison attempt") is None
    assert embedder.cache_size == 0
    # The retry actually reached the network again (the failure was not cached).
    assert len(requests) == 2
    assert all(json.loads(request.content)["input"] == ["poison attempt"] for request in requests)


# ─── (b) retrieval consequence: no dense search with a zero vector ──────────


async def test_empty_data_list_keeps_zero_vector_out_of_dense_retrieval(monkeypatch):
    embedder, _requests = await _shared_openai_embedder(
        monkeypatch, lambda request: httpx.Response(200, json={"data": []})
    )

    with pytest.raises(ProviderError, match="usable vector"):
        await embedder.embed("poison query")
    assert embedder.cache_size == 0

    query_embedding = await embed_query("poison query")
    # RED (unfixed): [0.0] * 1536 — truthy, so the retriever ran dense search.
    assert query_embedding is None

    retriever_db = _EmptyFetchDb()
    monkeypatch.setattr(retriever_module, "db", retriever_db)
    store = _RecordingStore()
    retriever = MemoryRetriever(store=store, rerank=False, persona_enabled=False)
    results = await retriever.retrieve(
        query="poison query", agent_id="agent-a", query_embedding=query_embedding
    )

    assert results == []
    # RED (unfixed): the dense branch was entered with the zero vector.
    assert store.dense_queries == []
    # The keyword branch still ran: honest degradation, not silent starvation.
    assert retriever_db.fetch_calls == 1


# ─── (c) valid embedding: sanity + recovery after a failure ─────────────────


async def test_valid_response_caches_and_recovers_after_malformed(monkeypatch):
    responses = [
        httpx.Response(200, json={"model": "test-embed"}),  # malformed first
        httpx.Response(200, json={"data": [{"index": 0, "embedding": VALID_VECTOR}]}),
    ]
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        response = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return response

    embedder, requests = await _shared_openai_embedder(monkeypatch, responder)

    with pytest.raises(ProviderError):
        await embedder.embed("recover me")
    assert embedder.cache_size == 0

    vector = await embedder.embed("recover me")
    assert vector == VALID_VECTOR
    assert any(vector)  # a real direction, not the fabricated zero vector
    assert embedder.cache_size == 1
    assert embedder._cache[embedder._cache_key("recover me")] == VALID_VECTOR

    # The memory boundary sees the recovered vector and serves the cached
    # copy without a third network call.
    assert await embed_query("recover me") == VALID_VECTOR
    assert len(requests) == 2


# ─── (d) batch short result: regression (still raises, no partial cache) ────


async def test_batch_short_result_raises_without_partial_cache_writes(monkeypatch):
    responses = [
        httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.25] * 4}]}),
        httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": [0.25] * 4},
                    {"index": 1, "embedding": [0.75] * 4},
                ]
            },
        ),
    ]
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        response = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return response

    embedder, requests = await _shared_openai_embedder(monkeypatch, responder)

    with pytest.raises(ProviderError, match="usable vector"):
        await embedder.embed_batch(["alpha", "beta"])
    # RED (unfixed): zip(strict=True) raised only after caching row "alpha".
    assert embedder.cache_size == 0

    # The batch path recovers once the provider returns a full response.
    vectors = await embedder.embed_batch(["alpha", "beta"])
    assert vectors == [[0.25] * 4, [0.75] * 4]
    assert embedder.cache_size == 2

    # Fully cached now: no further network traffic.
    assert await embedder.embed_batch(["alpha", "beta"]) == vectors
    assert len(requests) == 2


# ─── genuine empty input: zero-vector contract unchanged, no network ────────


async def test_genuine_empty_input_keeps_zero_vector_contract_and_no_network(monkeypatch):
    def responder(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        pytest.fail("empty inputs must never reach the embeddings API")

    embedder, requests = await _shared_openai_embedder(monkeypatch, responder)
    zeros = [0.0] * embedder.dimensions

    assert await embedder.embed("") == zeros
    assert await embedder.embed_batch([""]) == [zeros]
    assert await embedder.embed_batch([]) == []
    # The retrieval boundary already treats blank queries as unavailable.
    assert await embed_query("") is None
    assert await embed_query("   ") is None
    assert requests == []
    assert embedder.cache_size == 0
