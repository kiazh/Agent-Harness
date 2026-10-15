"""Holistic-review regression tests, lane L2R (2026-10-15).

Findings covered:

* H02 — streaming redaction emitted the tail of long secret-shaped tokens
  once the token grew past the partial-pattern caps while its complete
  pattern is unbounded (redaction.py).
* H05 — explicit semantic memory searches never requested dense retrieval
  (tools/memory.py, api/routes/memory.py, gateway/features/memory.py).
* H06 — total memory retrieval failure was reported as a valid empty
  result (retriever.py, tools/memory.py).

All secret values below are synthetic; no database, network, or real
credentials are used.
"""

from __future__ import annotations

import pytest

from machina.memory.redaction import StreamingSecretRedactor, redact_secrets


def _stream(text: str, chunks: list[str]) -> str:
    redactor = StreamingSecretRedactor()
    return "".join(redactor.feed(chunk) for chunk in chunks) + redactor.flush()


def _stream_sized(text: str, size: int) -> str:
    return _stream(text, [text[i : i + size] for i in range(0, len(text), size)])


# --- H02: long secret-shaped tokens must not emit their tail ----------------
#
# Every case pairs text with the secret payload it contains. The complete
# redaction patterns for these formats are unbounded ({20,}, {16,}, ...), so
# a still-growing token can exceed any finite partial-prefix cap. Batch
# redaction removes the whole token; streaming must match that decision at
# every chunking instead of dropping recognition mid-token.


def _long_token_cases() -> list:
    sk = "sk-" + "a" * 210 + "b" * 80
    bearer = "a" * 510 + "b" * 40
    password = "x" * 250 + "y" * 30
    generic = "a" * 350 + "b" * 30
    return [
        pytest.param(
            sk + " done.",
            sk,
            id="sk-beyond-200-cap",
        ),
        pytest.param(
            "Bearer " + bearer + " done.",
            bearer,
            id="bearer-beyond-500-cap",
        ),
        pytest.param(
            "password=" + password + " done.",
            password,
            id="password-beyond-200-cap",
        ),
        pytest.param(
            "token=" + generic + " done.",
            generic,
            id="generic-assignment-beyond-300-cap",
        ),
        pytest.param(
            'api_key="' + generic + '" done.',
            generic,
            id="quoted-key-beyond-300-cap",
        ),
        pytest.param(
            "postgres://u:" + generic + "@host/db done.",
            generic,
            id="db-userinfo-beyond-300-cap",
        ),
        pytest.param(
            "password" + " " * 15 + "= hunter2synthetic",
            "hunter2synthetic",
            id="assignment-gap-beyond-10-spaces",
        ),
    ]


@pytest.mark.parametrize(("text", "secret"), _long_token_cases())
def test_h02_long_token_stream_matches_batch(text: str, secret: str) -> None:
    batch = redact_secrets(text).text
    assert secret not in batch, f"batch failed to redact {secret[:16]!r}"
    for size in (1, 7, 40):
        output = _stream_sized(text, size)
        assert output == batch, f"chunks of {size} diverged: {output!r}"
    for split in range(1, len(text), max(1, len(text) // 17)):
        output = _stream(text, [text[:split], text[split:]])
        assert output == batch, f"2-way split at {split} diverged: {output!r}"


def test_h02_reviewer_diagnostic_long_sk_tail_never_emitted() -> None:
    """Exact reviewer repro: `sk-` + 210×a, then 80×b + delimiter."""
    first, second = "sk-" + "a" * 210, "b" * 80 + " done."
    batch = redact_secrets(first + second).text
    assert batch == "[REDACTED_OPENAI_KEY] done."

    redactor = StreamingSecretRedactor()
    output = redactor.feed(first) + redactor.feed(second) + redactor.flush()

    assert output == batch
    assert "b" * 8 not in output


def test_h02_stream_state_survives_delimiter_across_many_chunks() -> None:
    """The token is recognized until its delimiter, however it is chunked."""
    text = "leak " + "sk-" + "a" * 640 + " note: done."
    batch = redact_secrets(text).text
    assert "a" * 32 not in batch
    for size in (1, 2, 5, 13, 64, 211):
        assert _stream_sized(text, size) == batch, f"chunks of {size}"


# --- H05: explicit search entry points must prepare dense retrieval ---------
#
# The retriever starts dense search only when given a query_embedding, so the
# tool/API/gateway surfaces must prepare it through embed_query() exactly like
# ordinary agent preparation does. When no embedder is configured the search
# stays keyword-only and the response says so honestly.


@pytest.mark.asyncio
async def test_h05_recall_tool_prepares_query_embedding(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from machina.memory import embeddings
    from machina.tools import memory as memory_tools

    seen: dict = {}

    async def fake_retrieve(self, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(embeddings, "embed_query", AsyncMock(return_value=[0.25, 0.5]))
    monkeypatch.setattr(memory_tools.MemoryRetriever, "retrieve", fake_retrieve)

    result = await memory_tools.recall("paraphrased query")

    assert seen.get("query_embedding") == [0.25, 0.5]
    assert "No memories found" in result


@pytest.mark.asyncio
async def test_h05_recall_tool_keyword_only_without_embedder(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from machina.memory import embeddings
    from machina.tools import memory as memory_tools

    seen: dict = {}

    async def fake_retrieve(self, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(embeddings, "embed_query", AsyncMock(return_value=None))
    monkeypatch.setattr(memory_tools.MemoryRetriever, "retrieve", fake_retrieve)

    await memory_tools.recall("paraphrased query")

    # None keeps the retriever's keyword-only contract intact.
    assert seen.get("query_embedding") is None


@pytest.mark.asyncio
async def test_h05_api_search_requests_dense_and_reports_mode(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from machina.api.routes import memory as memory_routes
    from machina.memory import embeddings

    seen: dict = {}

    async def fake_retrieve(self, query, **kwargs):
        seen["query"] = query
        seen.update(kwargs)
        return []

    monkeypatch.setattr(embeddings, "embed_query", AsyncMock(return_value=[0.25]))
    monkeypatch.setattr("machina.memory.retriever.MemoryRetriever.retrieve", fake_retrieve)

    response = await memory_routes.search_memory(query="paraphrase")

    assert seen.get("query_embedding") == [0.25]
    assert response["mode"] == "dense+sparse"


@pytest.mark.asyncio
async def test_h05_api_search_reports_keyword_only_without_embedder(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from machina.api.routes import memory as memory_routes
    from machina.memory import embeddings

    async def fake_retrieve(self, query, **kwargs):
        return []

    monkeypatch.setattr(embeddings, "embed_query", AsyncMock(return_value=None))
    monkeypatch.setattr("machina.memory.retriever.MemoryRetriever.retrieve", fake_retrieve)

    response = await memory_routes.search_memory(query="paraphrase")

    assert response["mode"] == "keyword-only"
    assert response["memories"] == []


@pytest.mark.asyncio
async def test_h05_gateway_search_requests_dense_and_reports_mode(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from machina.gateway.features import memory_search
    from machina.memory import embeddings

    seen: dict = {}

    async def fake_retrieve(self, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(embeddings, "embed_query", AsyncMock(return_value=[0.5]))
    monkeypatch.setattr("machina.memory.retriever.MemoryRetriever.retrieve", fake_retrieve)

    gw = MagicMock()
    gw.require_db = MagicMock()
    gw.get_session = AsyncMock()

    result = await memory_search(gw, {"query": "hello world"})

    assert seen.get("query_embedding") == [0.5]
    assert result["mode"] == "dense+sparse"
    assert result["results"] == []


# --- H06: total retrieval failure must not look like an empty result --------
#
# Partial success is preserved (some branch returned results), but when every
# attempted branch fails the retriever must raise a deliberate typed error so
# the tool/API/gateway surfaces report an outage instead of "no memories".


def _fact_entry(content: str = "stored memory"):
    import uuid

    from machina.memory.models import MemoryEntry

    return MemoryEntry(
        id=uuid.uuid4(), session_id=None, agent_id="harness", content=content, category="fact"
    )


async def _failing_keyword(*args, **kwargs):
    raise ConnectionError("synthetic store unavailable")


@pytest.mark.asyncio
async def test_h06_all_branches_failing_raises_typed_error(monkeypatch) -> None:
    from machina.memory.retriever import MemoryRetrievalError, MemoryRetriever

    retriever = MemoryRetriever(rerank=False)
    monkeypatch.setattr(retriever, "_keyword_search", _failing_keyword)

    with pytest.raises(MemoryRetrievalError):
        await retriever.retrieve("anything")


@pytest.mark.asyncio
async def test_h06_dense_and_sparse_both_failing_raises(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from machina.memory.retriever import MemoryRetrievalError, MemoryRetriever

    store = MagicMock()
    store.search_by_embedding = AsyncMock(side_effect=ConnectionError("dense down"))
    retriever = MemoryRetriever(store=store, rerank=False)
    monkeypatch.setattr(retriever, "_keyword_search", _failing_keyword)

    with pytest.raises(MemoryRetrievalError):
        await retriever.retrieve("anything", query_embedding=[0.1, 0.2])


@pytest.mark.asyncio
async def test_h06_partial_success_kept_when_keyword_branch_fails(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from machina.memory.retriever import MemoryRetriever

    entry = _fact_entry("dense-only survivor")
    store = MagicMock()
    store.search_by_embedding = AsyncMock(return_value=[(entry, 0.9)])
    store.batch_update_access = AsyncMock()
    retriever = MemoryRetriever(store=store, rerank=False)
    monkeypatch.setattr(retriever, "_keyword_search", _failing_keyword)

    results = await retriever.retrieve("anything", query_embedding=[0.1, 0.2])

    assert [rm.memory.id for rm in results] == [entry.id]


@pytest.mark.asyncio
async def test_h06_partial_success_kept_when_dense_branch_fails(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from machina.memory.retriever import MemoryRetriever

    entry = _fact_entry("keyword-only survivor")
    store = MagicMock()
    store.search_by_embedding = AsyncMock(side_effect=ConnectionError("dense down"))
    retriever = MemoryRetriever(store=store, rerank=False)

    async def keyword_hit(**kwargs):
        return [(entry, 1.0)]

    monkeypatch.setattr(retriever, "_keyword_search", keyword_hit)

    results = await retriever.retrieve("anything", query_embedding=[0.1, 0.2])

    assert [rm.memory.id for rm in results] == [entry.id]


@pytest.mark.asyncio
async def test_h06_genuinely_empty_result_stays_empty(monkeypatch) -> None:
    from machina.memory.retriever import MemoryRetriever

    retriever = MemoryRetriever(rerank=False)

    async def keyword_empty(**kwargs):
        return []

    monkeypatch.setattr(retriever, "_keyword_search", keyword_empty)

    assert await retriever.retrieve("anything") == []


@pytest.mark.asyncio
async def test_h06_recall_tool_surfaces_total_failure_as_error(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from machina.core.exceptions import ToolError
    from machina.memory import embeddings
    from machina.memory.retriever import MemoryRetriever
    from machina.tools import memory as memory_tools

    monkeypatch.setattr(embeddings, "embed_query", AsyncMock(return_value=None))
    monkeypatch.setattr(MemoryRetriever, "_keyword_search", _failing_keyword)

    with pytest.raises(ToolError, match="retrieval"):
        await memory_tools.recall("needle")


@pytest.mark.asyncio
async def test_h06_api_search_surfaces_total_failure_as_503(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from machina.api.routes import memory as memory_routes
    from machina.memory import embeddings
    from machina.memory.retriever import MemoryRetriever

    monkeypatch.setattr(embeddings, "embed_query", AsyncMock(return_value=None))
    monkeypatch.setattr(MemoryRetriever, "_keyword_search", _failing_keyword)

    with pytest.raises(HTTPException) as excinfo:
        await memory_routes.search_memory(query="needle", limit=5)

    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_h05_api_http_reports_mode_and_uses_embedding(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from httpx import ASGITransport, AsyncClient

    from machina.api.app import create_app
    from machina.memory.retriever import MemoryRetriever

    monkeypatch.setenv("MACHINA_API_KEY", "mode-key")
    seen: dict = {}

    async def fake_retrieve(self, query, **kwargs):
        seen.update(kwargs)
        return []

    mock_prepare = AsyncMock(return_value=[0.7])
    with (
        patch("machina.api.app.db.connect", new_callable=AsyncMock),
        patch("machina.api.app.db.close", new_callable=AsyncMock),
        patch.object(MemoryRetriever, "prepare_query_embedding", mock_prepare),
        patch.object(MemoryRetriever, "retrieve", fake_retrieve),
    ):
        app = create_app()
        app.state._rpc_gateway = MagicMock()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/memory/search",
                params={"query": "hello"},
                headers={"Authorization": "Bearer mode-key"},
            )

    assert resp.status_code == 200
    assert resp.json() == {"memories": [], "mode": "dense+sparse"}
    assert seen.get("query_embedding") == [0.7]


@pytest.mark.asyncio
async def test_h06_api_http_surfaces_total_failure_as_503(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from httpx import ASGITransport, AsyncClient

    from machina.api.app import create_app
    from machina.memory.retriever import MemoryRetriever

    monkeypatch.setenv("MACHINA_API_KEY", "fail-key")
    failing = AsyncMock(side_effect=ConnectionError("synthetic store unavailable"))
    with (
        patch("machina.api.app.db.connect", new_callable=AsyncMock),
        patch("machina.api.app.db.close", new_callable=AsyncMock),
        patch.object(MemoryRetriever, "prepare_query_embedding", AsyncMock(return_value=None)),
        patch.object(MemoryRetriever, "_keyword_search", failing),
    ):
        app = create_app()
        app.state._rpc_gateway = MagicMock()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/memory/search",
                params={"query": "hello"},
                headers={"Authorization": "Bearer fail-key"},
            )

    assert resp.status_code == 503
    assert "retrieval unavailable" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_h06_gateway_search_surfaces_total_failure_as_rpc_error(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from machina.gateway.errors import OPERATION_FAILED, RpcError
    from machina.gateway.features import memory_search
    from machina.memory import embeddings
    from machina.memory.retriever import MemoryRetriever

    monkeypatch.setattr(embeddings, "embed_query", AsyncMock(return_value=None))
    monkeypatch.setattr(MemoryRetriever, "_keyword_search", _failing_keyword)

    gw = MagicMock()
    gw.require_db = MagicMock()
    gw.get_session = AsyncMock()

    with pytest.raises(RpcError) as excinfo:
        await memory_search(gw, {"query": "needle"})

    assert excinfo.value.code == OPERATION_FAILED
