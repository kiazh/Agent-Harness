"""Pins for the local reranker backends and the reranker factory.

Covers the backend selection matrix (auto/local/cohere/http/none), the
LocalOnnxReranker fastembed call shape with a stubbed runtime (no model
download), the HTTP reranker's llama.cpp-compatible parse and honest
passthrough fallback, weighted RRF fusion, and the shared-pipeline identity
rebuild on reranker selection changes.
"""

from __future__ import annotations

import importlib
import sys
import types
import uuid
from types import SimpleNamespace

import httpx
import pytest

import machina.rag.reranker as reranker_module
from machina.rag.reranker import (
    CohereReranker,
    HttpReranker,
    IdentityReranker,
    LocalOnnxReranker,
    bound_reranker_identity,
    build_reranker,
    reranker_selection_identity,
    resolve_reranker_selection,
)

# NOTE: `import machina.core.config as config_module` binds the Config
# SINGLETON (machina.core re-exports it) — resolve the real module object.
config_module = importlib.import_module("machina.core.config")


class _FakeConfig:
    """Minimal config stand-in so resolution tests never touch backends."""

    def __init__(self, **values: str) -> None:
        self.values = values

    def get(self, key: str):
        return self.values.get(key, "")


def _configure(monkeypatch, *, available: bool = True, **values: str) -> None:
    monkeypatch.setattr(config_module, "config", _FakeConfig(**values), raising=False)
    monkeypatch.setattr(reranker_module, "reranker_available", lambda: available, raising=False)


# ─── Factory selection matrix ───────────────────────────────────────────────


def test_reranker_config_defaults_and_validation():
    """Measured defaults: reranking opt-in, dense RRF weight at 0.25."""
    assert config_module.DEFAULTS["reranker_backend"] == "none"
    assert config_module.DEFAULTS["rrf_keyword_weight"] == 1.0
    assert config_module.DEFAULTS["rrf_dense_weight"] == 0.25
    assert config_module.validate_value("reranker_backend", "LOCAL") == "local"
    with pytest.raises(ValueError, match="reranker_backend"):
        config_module.validate_value("reranker_backend", "openai")
    assert config_module.validate_value("reranker_timeout", "30") == 30.0
    with pytest.raises(ValueError, match="reranker_timeout"):
        config_module.validate_value("reranker_timeout", "0.2")
    with pytest.raises(ValueError, match="rrf_dense_weight"):
        config_module.validate_value("rrf_dense_weight", "11")


@pytest.mark.parametrize(
    ("backend", "model", "url", "cohere_key", "available", "expected"),
    [
        # none disables reranking regardless of everything else.
        ("none", "", "", "k", True, "none"),
        # explicit cohere requires a key; missing -> honest none.
        ("cohere", "", "", "k", False, "cohere"),
        ("cohere", "", "", "", True, "none"),
        # explicit local requires the fastembed runtime.
        ("local", "custom/model", "", "", True, "local"),
        ("local", "", "", "", False, "none"),
        # explicit http requires a url.
        ("http", "bge-reranker-v2-m3", "http://127.0.0.1:8080/", "", False, "http"),
        ("http", "", "", "", True, "none"),
        # auto: local-first beats a configured Cohere key (zero-API rule).
        ("auto", "", "", "", True, "local"),
        ("auto", "", "", "k", True, "local"),
        # auto: Cohere key is the fallback when the local runtime is absent.
        ("auto", "", "", "k", False, "cohere"),
        # auto: nothing available -> passthrough with a reason.
        ("auto", "", "", "", False, "none"),
        # unknown values fail closed.
        ("bogus", "", "", "k", True, "none"),
    ],
)
async def test_reranker_factory_selection_matrix(
    monkeypatch, backend, model, url, cohere_key, available, expected
):
    _configure(
        monkeypatch,
        available=available,
        reranker_backend=backend,
        reranker_local_model=model,
        reranker_http_url=url,
        cohere_api_key=cohere_key,
    )
    reranker = build_reranker()
    try:
        if expected == "local":
            assert isinstance(reranker, LocalOnnxReranker)
            assert reranker.model_name == (model or LocalOnnxReranker.DEFAULT_MODEL)
        elif expected == "cohere":
            assert isinstance(reranker, CohereReranker)
        elif expected == "http":
            assert isinstance(reranker, HttpReranker)
            # Instance normalizes the trailing slash for stable identity.
            assert reranker._url == "http://127.0.0.1:8080"
        else:
            assert isinstance(reranker, IdentityReranker)
            assert reranker.backend == "passthrough"
            assert reranker.reason
    finally:
        close = getattr(reranker, "close", None)
        if callable(close):
            await close()


def test_build_reranker_never_raises_on_constructor_failure(monkeypatch):
    _configure(monkeypatch, available=True, reranker_backend="local")

    class BoomReranker:
        DEFAULT_MODEL = "boom/model"

        def __init__(self, *args, **kwargs):
            raise RuntimeError("fastembed boom")

    monkeypatch.setattr(reranker_module, "LocalOnnxReranker", BoomReranker)
    reranker = build_reranker()
    assert isinstance(reranker, IdentityReranker)
    assert "local unavailable" in reranker.reason


def test_reranker_selection_identity_is_stable_and_rotates_keys(monkeypatch):
    _configure(
        monkeypatch,
        available=True,
        reranker_backend="cohere",
        cohere_api_key="key-one",
        reranker_timeout="30",
    )
    first = reranker_selection_identity(resolve_reranker_selection())
    assert first[0] == "cohere" and first[3]
    # Full-length digest: the bound instance uses the very same calculation
    # (R17 — a 16-char configured digest vs 64-char bound rebuilt forever).
    assert len(first[3]) == 64
    # Configured timeout participates for API-backed rerankers (R20).
    assert first[4] == 30.0

    _configure(
        monkeypatch,
        available=True,
        reranker_backend="cohere",
        cohere_api_key="key-two",
        reranker_timeout="30",
    )
    second = reranker_selection_identity(resolve_reranker_selection())
    assert second != first

    _configure(
        monkeypatch,
        available=True,
        reranker_backend="http",
        reranker_http_url="http://127.0.0.1:8080/",
        reranker_timeout="77",
    )
    identity = reranker_selection_identity(resolve_reranker_selection())
    assert identity == ("http", "", "http://127.0.0.1:8080", "", 77.0)

    # The fastembed backend has no request timeout: a timeout change must
    # not rebuild it (identity reports 0.0 for local).
    _configure(
        monkeypatch,
        available=True,
        reranker_backend="local",
        reranker_timeout="77",
    )
    local_identity = reranker_selection_identity(resolve_reranker_selection())
    assert local_identity[4] == 0.0


# ─── LocalOnnxReranker with a stubbed fastembed runtime ─────────────────────


class _FakeTextCrossEncoder:
    """Deterministic stand-in for fastembed's TextCrossEncoder."""

    mode = "scores"  # scores | raise | short

    def __init__(self, model_name, **kwargs):
        self.model_name = model_name

    def rerank(self, query, documents, batch_size=64):
        if self.mode == "raise":
            raise RuntimeError("onnx session exploded")
        texts = list(documents)
        if self.mode == "short":
            texts = texts[:-1] if texts else texts
        for text in texts:
            yield float(len(text))


def _stub_fastembed(monkeypatch) -> None:
    fastembed_mod = types.ModuleType("fastembed")
    rerank_mod = types.ModuleType("fastembed.rerank")
    cross_encoder_mod = types.ModuleType("fastembed.rerank.cross_encoder")
    cross_encoder_mod.TextCrossEncoder = _FakeTextCrossEncoder
    fastembed_mod.rerank = rerank_mod
    rerank_mod.cross_encoder = cross_encoder_mod
    monkeypatch.setitem(sys.modules, "fastembed", fastembed_mod)
    monkeypatch.setitem(sys.modules, "fastembed.rerank", rerank_mod)
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", cross_encoder_mod)


async def test_local_reranker_scores_and_uses_i_worker_thread(monkeypatch):
    _stub_fastembed(monkeypatch)
    reranker = LocalOnnxReranker(model="stub/reranker")
    assert reranker.backend == "local-onnx"
    assert reranker._model is None  # construction never loads

    try:
        results = await reranker.rerank("q", ["short", "a much longer passage"], top_k=2)
        assert [r.index for r in results] == [1, 0]
        assert [r.score for r in results] == [21.0, 5.0]
        assert results[0].text == "a much longer passage"

        top_one = await reranker.rerank("q", ["short", "a much longer passage"], top_k=1)
        assert [r.index for r in top_one] == [1]
        assert await reranker.rerank("q", []) == []
    finally:
        await reranker.close()


@pytest.mark.parametrize("mode", ["raise", "short"])
async def test_local_reranker_degrades_to_passthrough(monkeypatch, mode):
    _stub_fastembed(monkeypatch)
    reranker = LocalOnnxReranker(model="stub/reranker")
    try:
        # Force the load so the stub instance's mode applies.
        reranker._load_model().mode = mode
        results = await reranker.rerank("q", ["one", "two"], top_k=2)
        assert [r.index for r in results] == [0, 1]
        assert all(r.score is None for r in results)
    finally:
        await reranker.close()


async def test_local_reranker_validates_scores(monkeypatch):
    _stub_fastembed(monkeypatch)
    reranker = LocalOnnxReranker(model="stub/reranker")

    def bad_scores(query, documents, batch_size=64):
        for _ in documents:
            yield float("nan")

    try:
        reranker._load_model().rerank = bad_scores
        results = await reranker.rerank("q", ["one", "two"], top_k=2)
        assert all(r.score is None for r in results)
    finally:
        await reranker.close()


# ─── HttpReranker: llama.cpp /v1/rerank parse + fallback ────────────────────


def _http_reranker(handler) -> HttpReranker:
    reranker = HttpReranker(url="http://stub:8080", model="bge-reranker-v2-m3")
    reranker._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://stub:8080"
    )
    return reranker


async def test_http_reranker_parses_results():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["payload"] = request.read()
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                ]
            },
        )

    reranker = _http_reranker(handler)
    try:
        results = await reranker.rerank("q", ["first", "second"], top_k=2)
        assert seen["path"] == "/v1/rerank"
        assert [r.index for r in results] == [1, 0]
        assert [r.score for r in results] == [0.9, 0.1]
    finally:
        await reranker.close()


async def test_http_reranker_retries_rerank_alias_on_404():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/rerank":
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 1.0}]})

    reranker = _http_reranker(handler)
    try:
        results = await reranker.rerank("q", ["only"], top_k=1)
        assert paths == ["/v1/rerank", "/rerank"]
        assert [r.index for r in results] == [0]
    finally:
        await reranker.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": "boom"}),
        httpx.Response(200, json={"results": [{"index": 7, "relevance_score": 0.5}]}),
        httpx.Response(200, json={"results": []}),
    ],
)
async def test_http_reranker_degrades_to_passthrough(response):
    reranker = _http_reranker(lambda request: response)
    try:
        results = await reranker.rerank("q", ["one", "two"], top_k=2)
        assert [r.index for r in results] == [0, 1]
        assert all(r.score is None for r in results)
    finally:
        await reranker.close()


# ─── Pipeline status mapping ────────────────────────────────────────────────


class _NoopEmbedder:
    async def embed(self, text):
        return [0.0]

    async def embed_batch(self, texts):
        return [[0.0] for _ in texts]

    @property
    def dimensions(self):
        return 1

    @property
    def model_name(self):
        return "noop"


def test_pipeline_reranker_identity_mapping():
    from machina.rag.pipeline import RAGPipeline

    local = RAGPipeline(embedder=_NoopEmbedder(), reranker=LocalOnnxReranker(model="m1"))
    assert local.reranker_identity() == {"reranker": "local-onnx", "detail": "m1"}

    http = RAGPipeline(
        embedder=_NoopEmbedder(), reranker=HttpReranker(url="http://stub:9", model="m2")
    )
    assert http.reranker_identity() == {"reranker": "local-http", "detail": "m2"}

    identity = IdentityReranker(reason="no backend")
    passthrough = RAGPipeline(embedder=_NoopEmbedder(), reranker=identity)
    assert passthrough.reranker_identity() == {"reranker": "passthrough", "detail": "no backend"}

    class _Foreign:
        async def rerank(self, query, documents, top_k=10):
            return []

        @property
        def model_name(self):
            return "foreign"

    foreign = RAGPipeline(embedder=_NoopEmbedder(), reranker=_Foreign())
    assert foreign.reranker_identity() == {"reranker": "passthrough", "detail": "_Foreign"}


# ─── Weighted RRF fusion ────────────────────────────────────────────────────


def _chunk():
    return SimpleNamespace(id=uuid.uuid4())


def test_rrf_fuse_applies_branch_weights():
    from machina.rag.search import rrf_fuse

    a, b, c, d = _chunk(), _chunk(), _chunk(), _chunk()
    fused = rrf_fuse(
        [(a, 0.9), (b, 0.8)],
        [(c, 0.7), (d, 0.6)],
        rrf_k=60,
        dense_weight=3.0,
        keyword_weight=0.5,
    )
    by_id = {result.chunk.id: result for result in fused}
    assert by_id[a.id].rrf_score == pytest.approx(3.0 / 61)
    assert by_id[b.id].rrf_score == pytest.approx(3.0 / 62)
    assert by_id[c.id].rrf_score == pytest.approx(0.5 / 61)
    assert by_id[d.id].rrf_score == pytest.approx(0.5 / 62)
    assert by_id[a.id].dense_score == 0.9
    assert by_id[c.id].sparse_score == 0.7


def test_hybrid_search_passes_weights_to_fusion():
    from machina.rag.search import HybridSearch

    dense_chunk, sparse_chunk = _chunk(), _chunk()
    search = HybridSearch(dense_weight=0.25, keyword_weight=2.0)
    fused = search._rrf_fuse([(dense_chunk, 1.0)], [(sparse_chunk, 1.0)])
    by_id = {result.chunk.id: result for result in fused}
    assert by_id[dense_chunk.id].rrf_score == pytest.approx(0.25 / 61)
    assert by_id[sparse_chunk.id].rrf_score == pytest.approx(2.0 / 61)


def test_default_rag_config_reads_weights_and_explicit_config_wins(monkeypatch):
    from machina.rag.pipeline import RAGConfig, RAGPipeline, _default_rag_config

    monkeypatch.setattr(
        config_module,
        "config",
        _FakeConfig(rrf_keyword_weight="1.5", rrf_dense_weight="0.25"),
        raising=False,
    )
    default = _default_rag_config()
    assert default.rrf_keyword_weight == 1.5
    assert default.rrf_dense_weight == 0.25

    pipeline = RAGPipeline(embedder=_NoopEmbedder())
    assert pipeline.config.rrf_dense_weight == 0.25
    assert pipeline._search._dense_weight == 0.25
    assert pipeline._search._keyword_weight == 1.5

    explicit = RAGPipeline(embedder=_NoopEmbedder(), config=RAGConfig(rrf_dense_weight=0.5))
    assert explicit._search._dense_weight == 0.5
    assert explicit._search._keyword_weight == 1.0


# ─── Shared pipeline identity rebuilds ──────────────────────────────────────


async def test_shared_pipeline_rebuilds_on_reranker_selection_change(monkeypatch):
    import machina.rag.pipeline as pipeline_module
    from machina.tools import rag as rag_tools

    state = {
        "reranker_backend": "local",
        "reranker_local_model": "model-A",
        "reranker_http_url": "",
        "reranker_http_model": "",
        "cohere_api_key": "",
        "embedding_backend": "off",
        "embedding_model": "",
        "embedding_base_url": "",
    }
    fake = _FakeConfig(**state)
    monkeypatch.setattr(config_module, "config", fake, raising=False)
    monkeypatch.setattr(reranker_module, "reranker_available", lambda: True, raising=False)
    monkeypatch.setattr(pipeline_module, "build_embedder", lambda settings=None: None)
    monkeypatch.setattr(rag_tools, "_rag_pipeline", None)
    monkeypatch.setattr(rag_tools, "_rag_pipeline_identity", None, raising=False)

    first = await rag_tools.get_rag_pipeline()
    assert isinstance(first._reranker, LocalOnnxReranker)
    assert first._reranker.model_name == "model-A"
    assert await rag_tools.get_rag_pipeline() is first

    fake.values["reranker_local_model"] = "model-B"
    second = await rag_tools.get_rag_pipeline()
    assert second is not first
    assert second._reranker.model_name == "model-B"
    assert await rag_tools.get_rag_pipeline() is second

    fake.values["reranker_backend"] = "none"
    third = await rag_tools.get_rag_pipeline()
    assert third is not second
    assert isinstance(third._reranker, IdentityReranker)
    # A passthrough build is not retried on every request.
    assert await rag_tools.get_rag_pipeline() is third


async def test_shared_pipeline_reuse_with_real_cohere_backend_marker(monkeypatch):
    """R17: with the real Cohere backend marker, two consecutive reads of an
    identical configuration must return the SAME pipeline.

    The configured identity used a 16-char credential digest while the bound
    instance reported 64 characters, so every call rebuilt the pipeline and
    dropped its caches. Both sides must use one shared calculation.
    """
    from machina.tools import rag as rag_tools

    state = {
        "reranker_backend": "cohere",
        "cohere_api_key": "cohere-key-ONE",
        "embedding_backend": "off",
        "embedding_model": "",
        "embedding_base_url": "",
        "reranker_timeout": "10",
        "rrf_keyword_weight": "1.0",
        "rrf_dense_weight": "0.25",
    }
    fake = _FakeConfig(**state)
    monkeypatch.setattr(config_module, "config", fake, raising=False)
    monkeypatch.setattr(rag_tools, "_rag_pipeline", None, raising=False)
    monkeypatch.setattr(rag_tools, "_rag_pipeline_identity", None, raising=False)

    built: list = []
    try:
        first = await rag_tools.get_rag_pipeline()
        built.append(first)
        assert isinstance(first._reranker, CohereReranker)
        assert first._reranker.backend == "cohere"
        # One shared calculation for configured and bound identity.
        assert rag_tools._built_reranker_identity(first) == rag_tools._current_reranker_identity()
        assert await rag_tools.get_rag_pipeline() is first

        # A rotated key rebuilds exactly once and then reuses again.
        fake.values["cohere_api_key"] = "cohere-key-TWO"
        second = await rag_tools.get_rag_pipeline()
        built.append(second)
        assert second is not first
        assert second._reranker.api_key == "cohere-key-TWO"
        assert await rag_tools.get_rag_pipeline() is second
    finally:
        for pipeline in built:
            await pipeline._reranker.close()


async def test_shared_pipeline_rebuilds_on_rrf_weight_change(monkeypatch):
    """R19: RRF weights were read only at construction and omitted from the
    shared identity, so 1.0 -> 0.25 kept serving the old 1.0 pipeline."""
    from machina.tools import rag as rag_tools

    state = {
        "reranker_backend": "none",
        "embedding_backend": "off",
        "embedding_model": "",
        "embedding_base_url": "",
        "rrf_keyword_weight": "1.0",
        "rrf_dense_weight": "1.0",
    }
    fake = _FakeConfig(**state)
    monkeypatch.setattr(config_module, "config", fake, raising=False)
    monkeypatch.setattr(rag_tools, "_rag_pipeline", None, raising=False)
    monkeypatch.setattr(rag_tools, "_rag_pipeline_identity", None, raising=False)

    first = await rag_tools.get_rag_pipeline()
    assert first._search._dense_weight == 1.0
    assert await rag_tools.get_rag_pipeline() is first

    fake.values["rrf_dense_weight"] = "0.25"
    second = await rag_tools.get_rag_pipeline()
    assert second is not first
    assert second._search._dense_weight == 0.25
    assert await rag_tools.get_rag_pipeline() is second


async def test_reranker_timeout_flows_to_clients_and_rebuilds(monkeypatch):
    """R20: the configured reranker_timeout reaches factory-built HTTP and
    Cohere clients and participates in the shared identity, so a runtime
    update rebuilds the pipeline (77 was previously ignored: 10s/30s)."""
    from machina.tools import rag as rag_tools

    state = {
        "reranker_backend": "http",
        "reranker_http_url": "http://127.0.0.1:8080",
        "reranker_http_model": "",
        "reranker_timeout": "10",
        "embedding_backend": "off",
        "embedding_model": "",
        "embedding_base_url": "",
        "rrf_keyword_weight": "1.0",
        "rrf_dense_weight": "0.25",
    }
    fake = _FakeConfig(**state)
    monkeypatch.setattr(config_module, "config", fake, raising=False)
    monkeypatch.setattr(rag_tools, "_rag_pipeline", None, raising=False)
    monkeypatch.setattr(rag_tools, "_rag_pipeline_identity", None, raising=False)

    settings = resolve_reranker_selection()
    assert settings.timeout == 10.0
    reranker = build_reranker(settings)
    try:
        assert isinstance(reranker, HttpReranker)
        assert reranker._timeout == 10.0
        # Configured and bound identities agree (shared calculation).
        assert reranker_selection_identity(settings) == bound_reranker_identity(reranker)
    finally:
        await reranker.close()

    built: list = []
    try:
        first = await rag_tools.get_rag_pipeline()
        built.append(first)
        assert first._reranker._timeout == 10.0
        assert await rag_tools.get_rag_pipeline() is first

        fake.values["reranker_timeout"] = "77"
        second = await rag_tools.get_rag_pipeline()
        built.append(second)
        assert second is not first
        assert second._reranker._timeout == 77.0
        assert await rag_tools.get_rag_pipeline() is second
    finally:
        for pipeline in built:
            await pipeline._reranker.close()

    # The same configured value reaches the Cohere client.
    fake.values.update(
        {
            "reranker_backend": "cohere",
            "cohere_api_key": "cohere-key-X",
            "reranker_timeout": "77",
        }
    )
    settings = resolve_reranker_selection()
    reranker = build_reranker(settings)
    try:
        assert isinstance(reranker, CohereReranker)
        assert reranker._timeout == 77.0
    finally:
        await reranker.close()


# ─── Harness helpers ────────────────────────────────────────────────────────


def test_archive_chunk_text_handles_both_payload_shapes():
    from machina.research.locomo import _archive_chunk_text

    assert _archive_chunk_text(SimpleNamespace(payload={"content": "turn"})) == "turn"
    assert _archive_chunk_text(SimpleNamespace(payload={"text": "doc"})) == "doc"
    assert _archive_chunk_text(SimpleNamespace(payload={})) == ""
    assert _archive_chunk_text(SimpleNamespace(payload=None)) == ""


def test_candidate_chunks_accepts_results_and_tuples():
    from machina.research.locomo import _candidate_chunks

    chunk = _chunk()
    result = SimpleNamespace(chunk=chunk)
    assert _candidate_chunks([result]) == [chunk]
    assert _candidate_chunks([(chunk, 0.5)]) == [chunk]
    assert _candidate_chunks([result, (chunk, 0.5)]) == [chunk, chunk]


def test_rrf_weights_read_config_and_degrade(monkeypatch):
    from machina.research.locomo import _rrf_weights

    monkeypatch.setattr(
        config_module,
        "config",
        _FakeConfig(rrf_keyword_weight="2.0", rrf_dense_weight="0.5"),
        raising=False,
    )
    assert _rrf_weights() == (2.0, 0.5)

    class _Boom:
        def get(self, key):
            raise RuntimeError("config boom")

    monkeypatch.setattr(config_module, "config", _Boom(), raising=False)
    assert _rrf_weights() == (1.0, 1.0)
