"""Pins for the local/zero-API embedding backend and the embedder factory.

Covers the backend selection matrix, keyless OpenAI-compatible local
servers, the shared cached embedder used by memory retrieval, and honest
keyword-only fallback when the local runtime is absent.
"""

from __future__ import annotations

import importlib
import json
import uuid

import httpx
import msgpack
import pytest
import pytest_asyncio

import machina.rag.embedder as embedder_module
from machina.core.config import DEFAULTS, Config, validate_value
from machina.memory.embeddings import embed_query, embed_texts, embedding_status
from machina.rag.embedder import (
    EmbeddingSettings,
    LocalEmbedder,
    OnnxEmbedder,
    OpenAIEmbedder,
    build_embedder,
    close_shared_embedder,
    resolve_embedding_settings,
)

# NOTE: `import machina.core.config as config_module` binds the Config
# SINGLETON (machina.core re-exports it) — resolve the real module object.
config_module = importlib.import_module("machina.core.config")


class _FakeConfig:
    """Minimal config stand-in so resolution tests never touch secrets/backends."""

    def __init__(self, **values: str) -> None:
        self.values = values

    def get(self, key: str) -> str:
        return self.values.get(key, "")


@pytest_asyncio.fixture(autouse=True)
async def _reset_shared_embedder():
    await close_shared_embedder()
    yield
    await close_shared_embedder()


def _configure(
    monkeypatch,
    *,
    backend: str = "auto",
    model: str = "",
    base_url: str = "",
    openai_key: str = "",
    openrouter_key: str = "",
    local_available: bool = True,
    onnx_available: bool = False,
) -> None:
    fake = _FakeConfig(
        embedding_backend=backend,
        embedding_model=model,
        embedding_base_url=base_url,
        openai_api_key=openai_key,
        openrouter_api_key=openrouter_key,
    )
    monkeypatch.setattr(config_module, "config", fake, raising=False)
    monkeypatch.setattr(
        embedder_module, "local_embedder_available", lambda: local_available, raising=False
    )
    monkeypatch.setattr(
        embedder_module, "onnx_embedder_available", lambda: onnx_available, raising=False
    )


# ─── Config surface ─────────────────────────────────────────────────────────


def test_embedding_config_defaults_and_validation():
    assert DEFAULTS["embedding_backend"] == "off"
    assert DEFAULTS["embedding_model"] == ""
    assert DEFAULTS["embedding_base_url"] == ""
    assert validate_value("embedding_backend", "local") == "local"
    assert validate_value("embedding_backend", "ONNX") == "onnx"
    assert validate_value("embedding_backend", "OFF") == "off"
    with pytest.raises(ValueError, match="embedding_backend"):
        validate_value("embedding_backend", "cohere")


def test_embedding_config_env_resolution(monkeypatch):
    monkeypatch.setenv("MACHINA_EMBEDDING_BACKEND", "off")
    monkeypatch.setenv("MACHINA_EMBEDDING_MODEL", "minishlab/potion-base-8M")
    monkeypatch.setenv("MACHINA_EMBEDDING_BASE_URL", "http://127.0.0.1:8080/v1")
    cfg = Config()
    assert cfg.get("embedding_backend") == "off"
    assert cfg.get("embedding_model") == "minishlab/potion-base-8M"
    assert cfg.get("embedding_base_url") == "http://127.0.0.1:8080/v1"


# ─── Factory selection matrix ───────────────────────────────────────────────


@pytest.mark.parametrize(
    (
        "backend",
        "model",
        "base_url",
        "openai_key",
        "openrouter_key",
        "local_available",
        "onnx_available",
        "expected",
    ),
    [
        # off forces None regardless of everything else.
        ("off", "", "", "", "", True, True, None),
        ("off", "", "http://localhost:1234/v1", "sk-x", "", True, True, None),
        # auto: no endpoint, no keys -> onnx when fastembed is installed.
        ("auto", "", "", "", "", True, True, "onnx"),
        ("auto", "", "", "", "", False, True, "onnx"),
        # auto: onnx has precedence over the static model2vec runtime.
        ("auto", "", "", "", "", True, False, "local"),
        ("auto", "", "", "", "", False, False, None),
        # auto: local-first — the on-host runtimes win over API credentials.
        ("auto", "", "", "sk-test", "", True, True, "onnx"),
        ("auto", "", "", "", "or-test", True, False, "local"),
        # auto: API credentials are the fallback when no local runtime exists.
        ("auto", "", "", "sk-test", "", False, False, "openai"),
        ("auto", "", "", "", "or-test", False, False, "openai"),
        # auto: an explicit endpoint is honored first and keyless.
        ("auto", "", "http://127.0.0.1:8080/v1", "", "", False, True, "openai"),
        # auto: an explicit model is honored by whichever backend wins.
        ("auto", "custom-embed", "", "sk-test", "", True, True, "onnx"),
        ("auto", "other-potion", "", "", "", True, False, "local"),
        # local forces the runtime; missing dependency -> honest None.
        ("local", "", "", "", "", True, True, "local"),
        ("local", "minishlab/potion-base-4M", "", "", "", True, False, "local"),
        ("local", "", "", "", "", False, True, None),
        # onnx forces fastembed; missing dependency -> honest None.
        ("onnx", "", "", "", "", True, True, "onnx"),
        ("onnx", "BAAI/bge-base-en-v1.5", "", "", "", False, True, "onnx"),
        ("onnx", "", "", "", "", True, False, None),
        # openai forces the API adapter; without key or base_url -> None.
        ("openai", "", "", "sk-test", "", True, True, "openai"),
        ("openai", "", "http://localhost:11434/v1", "", "", False, False, "openai"),
        ("openai", "", "", "", "", True, True, None),
    ],
)
async def test_factory_selection_matrix(
    monkeypatch,
    backend,
    model,
    base_url,
    openai_key,
    openrouter_key,
    local_available,
    onnx_available,
    expected,
):
    _configure(
        monkeypatch,
        backend=backend,
        model=model,
        base_url=base_url,
        openai_key=openai_key,
        openrouter_key=openrouter_key,
        local_available=local_available,
        onnx_available=onnx_available,
    )
    embedder = build_embedder()
    if expected is None:
        assert embedder is None
        return
    if expected == "onnx":
        assert isinstance(embedder, OnnxEmbedder)
        assert embedder.model_name == (model or OnnxEmbedder.DEFAULT_MODEL)
    elif expected == "local":
        assert isinstance(embedder, LocalEmbedder)
        assert embedder.model_name == (model or LocalEmbedder.DEFAULT_MODEL)
    else:
        assert isinstance(embedder, OpenAIEmbedder)
        assert embedder.model_name == (model or OpenAIEmbedder.DEFAULT_MODEL)
        if base_url:
            assert embedder._base_url == base_url
    await embedder.close()


def test_resolve_reports_reason_when_local_dependency_missing(monkeypatch):
    _configure(monkeypatch, backend="local", local_available=False)
    settings = resolve_embedding_settings()
    assert settings.kind == "none"
    assert "model2vec" in settings.reason
    assert build_embedder() is None


def test_resolve_reports_reason_when_onnx_dependency_missing(monkeypatch):
    _configure(monkeypatch, backend="onnx", onnx_available=False)
    settings = resolve_embedding_settings()
    assert settings.kind == "none"
    assert "fastembed" in settings.reason
    assert build_embedder() is None


async def test_auto_considers_model_backend_compatibility(monkeypatch):
    """R18: auto + fastembed must not forward an explicit model the ONNX
    runtime cannot serve. fastembed rejects those ids at load, so search
    silently degraded to keyword-only for previously working model2vec/API
    configurations; the compatible backend must win instead."""
    monkeypatch.setattr(
        embedder_module,
        "_fastembed_supports_model",
        lambda model: model == "minishlab/potion-base-8M",
        raising=False,
    )

    # model2vec id fastembed rejects -> the model2vec runtime (not ONNX).
    _configure(
        monkeypatch,
        backend="auto",
        model="minishlab/potion-base-4M",
        onnx_available=True,
    )
    settings = resolve_embedding_settings()
    assert (settings.kind, settings.model) == ("local", "minishlab/potion-base-4M")
    embedder = build_embedder(settings)
    assert isinstance(embedder, LocalEmbedder)
    await embedder.close()

    # A fastembed-served id stays on ONNX (ruled out as an incompatibility).
    _configure(
        monkeypatch,
        backend="auto",
        model="minishlab/potion-base-8M",
        onnx_available=True,
    )
    settings = resolve_embedding_settings()
    assert (settings.kind, settings.model) == ("onnx", "minishlab/potion-base-8M")

    # OpenAI API id -> the API adapter when credentials exist, not ONNX.
    _configure(
        monkeypatch,
        backend="auto",
        model="text-embedding-3-small",
        openai_key="sk-test",
        onnx_available=True,
    )
    settings = resolve_embedding_settings()
    assert (settings.kind, settings.model) == ("openai", "text-embedding-3-small")

    # Without credentials nothing can serve that id: honest keyword-only.
    _configure(
        monkeypatch,
        backend="auto",
        model="text-embedding-3-small",
        onnx_available=True,
    )
    settings = resolve_embedding_settings()
    assert settings.kind == "none"
    assert "text-embedding-3-small" in settings.reason


def test_provider_qualified_ids_are_recognized_as_api_models():
    """Re-verification: ``openai/text-embedding-3-small`` is an API model id.

    The provider prefix names the API provider and the remainder is the API
    model id; no local runtime can serve the qualified form. Model-runtime
    publishers (minishlab, BAAI) are not API providers and stay unqualified.
    """
    assert embedder_module._openai_api_model("openai/text-embedding-3-small") is True
    assert embedder_module._openai_api_model("openai/text-embedding-3-large") is True
    assert embedder_module._openai_api_model("together/mistral-embed") is True
    assert embedder_module._openai_api_model("deepseek/deepseek-embedding") is True
    assert embedder_module._openai_api_model("minishlab/potion-base-8M") is False
    assert embedder_module._openai_api_model("BAAI/bge-small-en-v1.5") is False

    assert embedder_module._model_fits_onnx("openai/text-embedding-3-small") is False
    assert embedder_module._model_fits_local("openai/text-embedding-3-small") is False
    assert embedder_module._model_fits_onnx("BAAI/bge-small-en-v1.5") is True


async def test_auto_routes_provider_qualified_api_ids_to_the_api(monkeypatch):
    """With fastembed installed, auto must not forward a provider-qualified
    API id to ONNX (fastembed rejects it at load, degrading to keyword-only).
    Credentials route it to the API backend with the remainder as the model;
    without credentials the result is the honest ``none`` plus a reason, and
    fastembed-supported ids keep ONNX."""
    monkeypatch.setattr(
        embedder_module, "_fastembed_supports_model", lambda model: False, raising=False
    )

    _configure(
        monkeypatch,
        backend="auto",
        model="openai/text-embedding-3-small",
        openai_key="sk-test",
        onnx_available=True,
    )
    settings = resolve_embedding_settings()
    assert (settings.kind, settings.model) == ("openai", "text-embedding-3-small")
    embedder = build_embedder(settings)
    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder.model_name == "text-embedding-3-small"
    await embedder.close()

    # No credentials and no compatible runtime: keyword-only with a reason.
    _configure(
        monkeypatch,
        backend="auto",
        model="openai/text-embedding-3-small",
        onnx_available=True,
        local_available=True,
    )
    settings = resolve_embedding_settings()
    assert settings.kind == "none"
    assert "openai/text-embedding-3-small" in settings.reason

    # fastembed-supported model ids keep the ONNX runtime, provider-qualified
    # publishers included.
    monkeypatch.setattr(
        embedder_module,
        "_fastembed_supports_model",
        lambda model: model == "minishlab/potion-base-8M",
        raising=False,
    )
    _configure(
        monkeypatch,
        backend="auto",
        model="minishlab/potion-base-8M",
        onnx_available=True,
    )
    settings = resolve_embedding_settings()
    assert (settings.kind, settings.model) == ("onnx", "minishlab/potion-base-8M")

    _configure(
        monkeypatch,
        backend="auto",
        model="BAAI/bge-small-en-v1.5",
        onnx_available=True,
    )
    settings = resolve_embedding_settings()
    assert (settings.kind, settings.model) == ("onnx", "BAAI/bge-small-en-v1.5")


def test_fastembed_runtime_rejects_the_review_models():
    """The compatibility check tracks fastembed's real supported list: at
    0.9, potion-base-4M and text-embedding-3-small are rejected while
    potion-base-8M is supported."""
    pytest.importorskip("fastembed")
    assert embedder_module._fastembed_supports_model("minishlab/potion-base-4M") is False
    assert embedder_module._fastembed_supports_model("text-embedding-3-small") is False
    assert embedder_module._fastembed_supports_model("minishlab/potion-base-8M") is True


# ─── Keyless OpenAI-compatible local server ─────────────────────────────────


async def test_keyless_local_server_embeds_without_authorization(monkeypatch):
    _configure(monkeypatch, backend="auto", base_url="http://127.0.0.1:8080/v1")
    embedder = build_embedder()
    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder.api_key == ""

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.25] * 4}]})

    embedder._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=embedder._base_url
    )
    try:
        vector = await embedder.embed("hello local server")
    finally:
        await embedder.close()
    assert vector == [0.25] * 4
    assert seen["url"].endswith("/embeddings")
    assert seen["auth"] is None
    assert seen["payload"]["model"] == OpenAIEmbedder.DEFAULT_MODEL


async def test_explicit_base_url_is_not_redirected_to_openrouter(monkeypatch):
    _configure(
        monkeypatch, backend="auto", base_url="http://127.0.0.1:8080/v1", openrouter_key="or-x"
    )
    embedder = build_embedder()
    assert isinstance(embedder, OpenAIEmbedder)
    try:
        assert embedder._base_url == "http://127.0.0.1:8080/v1"
        # The OpenRouter credential must not leak to the local endpoint.
        assert embedder.api_key == ""
    finally:
        await embedder.close()


# ─── LocalEmbedder laziness and shape ───────────────────────────────────────


def test_local_embedder_is_lazy_and_reports_dimensions():
    embedder = LocalEmbedder()
    assert embedder._model is None
    # Known model ids answer without a (potentially network-touching) load.
    assert embedder.native_dimensions == 256
    assert embedder.dimensions == 1536
    assert embedder._model is None


@pytest.mark.skipif(
    not embedder_module.local_embedder_available(), reason="model2vec not installed"
)
async def test_local_embedder_roundtrip_and_zero_padding():
    embedder = LocalEmbedder(batch_size=8)
    try:
        vectors = await embedder.embed_batch(
            ["The telescope observed a nebula", "I like pancakes for breakfast"]
        )
        assert len(vectors) == 2
        assert all(len(vector) == 1536 for vector in vectors)
        assert vectors[0][:256] != vectors[1][:256]
        assert all(value == 0.0 for value in vectors[0][256:])
        single = await embedder.embed("The telescope observed a nebula")
        assert single == vectors[0]
        empty = await embedder.embed_batch(["", "text"])
        assert empty[0] == [0.0] * 1536
    finally:
        await embedder.close()


# ─── OnnxEmbedder laziness, shape and stubbed fastembed path ────────────────


def test_onnx_embedder_is_lazy_and_reports_dimensions():
    embedder = OnnxEmbedder()
    assert embedder._model is None
    # Known model ids answer without a (potentially network-touching) load.
    assert embedder.native_dimensions == 384
    assert embedder.dimensions == 1536
    assert embedder._model is None


async def test_onnx_embedder_roundtrip_with_stubbed_fastembed(monkeypatch):
    """Pins the fastembed call shape without a model download."""
    import sys
    import types

    class _FakeTextEmbedding:
        embedding_size = 384

        def __init__(self, model_name, **kwargs):
            self.model_name = model_name

        def embed(self, texts, batch_size=64):
            for text in texts:
                vector = [float(len(text))] * 384
                yield _NpArray(vector)

    class _NpArray(list):
        def tolist(self):
            return list(self)

    fake_module = types.ModuleType("fastembed")
    fake_module.TextEmbedding = _FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake_module)

    embedder = OnnxEmbedder(model="stub/model", batch_size=2)
    assert embedder._model is None
    try:
        vectors = await embedder.embed_batch(["abc", "de"])
        assert len(vectors) == 2
        assert all(len(vector) == 1536 for vector in vectors)
        assert vectors[0][:384] == [3.0] * 384
        assert vectors[1][:384] == [2.0] * 384
        assert all(value == 0.0 for value in vectors[0][384:])
    finally:
        await embedder.close()
    assert embedder._model is None


async def test_onnx_embedder_unknown_model_requires_loaded_width(monkeypatch):
    """An unknown model id must fail honestly when the stub lacks a width."""
    import sys
    import types

    class _FakeTextEmbedding:
        embedding_size = None

        def __init__(self, model_name, **kwargs):
            pass

        def embed(self, texts, batch_size=64):
            return iter(())

    fake_module = types.ModuleType("fastembed")
    fake_module.TextEmbedding = _FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake_module)

    embedder = OnnxEmbedder(model="stub/no-width")
    with pytest.raises(ValueError, match="embedding width"):
        _ = embedder.dimensions


# ─── Shared cached embedder used by memory retrieval ────────────────────────


async def test_embed_query_reuses_one_shared_embedder(monkeypatch):
    _configure(monkeypatch, backend="local", model="fake-model")
    built: list = []

    class _FakeEmbedder:
        model_name = "fake-model"

        def __init__(self) -> None:
            self.closed = False

        async def embed(self, text: str) -> list[float]:
            return [0.1, 0.2]

        async def embed_batch(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2] for _ in texts]

        async def close(self) -> None:
            self.closed = True

    def fake_build(settings=None):
        built.append(settings)
        return _FakeEmbedder()

    monkeypatch.setattr(embedder_module, "build_embedder", fake_build, raising=False)

    assert await embed_query("one") == [0.1, 0.2]
    assert await embed_query("two") == [0.1, 0.2]
    assert await embed_texts(["a", "b"]) == [[0.1, 0.2], [0.1, 0.2]]
    # One construction for all calls; no per-query close (no model reload).
    assert len(built) == 1
    shared = embedder_module.get_shared_embedder()
    assert shared is not None and shared.closed is False

    await close_shared_embedder()
    assert shared.closed is True


async def test_embed_query_returns_none_when_unavailable(monkeypatch):
    _configure(monkeypatch, backend="off")
    assert await embed_query("hello") is None
    assert await embed_texts(["hello"]) is None


def test_embedding_status_reports_backend_and_model(monkeypatch):
    _configure(monkeypatch, backend="local", model="minishlab/potion-base-8M")
    status = embedding_status()
    assert status["mode"] == "dense+sparse"
    assert status["backend"] == "local"
    assert status["model"] == "minishlab/potion-base-8M"

    _configure(monkeypatch, backend="local", local_available=False)
    fallback = embedding_status()
    assert fallback["mode"] == "keyword-only"
    assert "model2vec" in fallback["reason"]


async def test_pipeline_default_selection_uses_factory(monkeypatch):
    import machina.rag.pipeline as pipeline_module

    marker = object()
    monkeypatch.setattr(pipeline_module, "build_embedder", lambda settings=None: marker)
    pipeline = pipeline_module.RAGPipeline()
    assert pipeline._embedder is marker


async def test_shared_pipeline_rebuilds_on_embedding_config_change(monkeypatch):
    import machina.tools.rag as rag_tools

    monkeypatch.setattr("machina.rag.pipeline.build_embedder", lambda settings=None: None)
    monkeypatch.setattr(rag_tools, "_rag_pipeline", None)
    monkeypatch.setattr(rag_tools, "_rag_pipeline_identity", None, raising=False)

    from machina.core.config import config

    state = {"embedding_backend": "auto", "embedding_model": "", "embedding_base_url": ""}
    real_get = config.get

    def fake_get(key=None):
        if key in state:
            return state[key]
        return real_get(key)

    monkeypatch.setattr(config, "get", fake_get)
    first = await rag_tools.get_rag_pipeline()
    assert await rag_tools.get_rag_pipeline() is first
    state["embedding_backend"] = "local"
    second = await rag_tools.get_rag_pipeline()
    assert second is not first


# ─── Read-only dense archive search ─────────────────────────────────────────


async def test_search_archive_dense_is_read_only(monkeypatch):
    import machina.core.context as context_module

    session_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    payload = msgpack.packb({"content": "hi", "speaker": "Ada"}, use_bin_type=True)
    captured: dict = {}

    class _FakeDB:
        async def fetch(self, sql, *args):
            captured["sql"] = sql
            captured["args"] = args
            return [
                {
                    "chunk_id": chunk_id,
                    "session_id": session_id,
                    "agent_id": "benchmark",
                    "chunk_type": "dialogue",
                    "payload_msgpack": payload,
                    "token_count": 3,
                    "archived_at": None,
                    "original_created_at": None,
                    "similarity": 0.9,
                }
            ]

        async def execute(self, *args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("dense archive search must stay read-only")

    monkeypatch.setattr(context_module, "db", _FakeDB())
    rows = await context_module.ContextManager().search_archive_dense(session_id, [0.1, 0.2], 3)
    assert rows[0][0].id == chunk_id
    assert rows[0][1] == 0.9
    assert "context_archive" in captured["sql"]
    assert "ORDER BY embedding" in captured["sql"]
    assert captured["args"][-1] == 3


def test_embedding_settings_dataclass_shape():
    settings = EmbeddingSettings("none", reason="x")
    assert settings.kind == "none" and settings.backend == "auto" and settings.reason == "x"
