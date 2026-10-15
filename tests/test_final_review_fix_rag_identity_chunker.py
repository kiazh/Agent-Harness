"""Final-review fix evidence (2026-10-15): F08 shared-RAG embedding identity
and the P3-1 chunker leading-separator coverage hole.

Both pins are RED on the pre-fix tree and GREEN after the fix.

* **F08** — the shared RAG pipeline identity tracked the *configured*
  embedding backend/model/base_url but neither the *resolved* selection
  (effective availability: keyword-only vs. dense) nor a credential
  fingerprint. A rotated/cleared key therefore kept the old client serving,
  and a keyword-only pipeline stayed keyword-only after a credential
  appeared. The fix reuses the shared embedder's identity helper
  (``_embedder_identity``) rather than duplicating a digest.
* **P3-1** — ``_split_recursive`` could drop a *leading* separator of a
  recursed part, leaving one source character covered by no chunk. Coverage
  only (the offsets guard withholds overlap across the hole, so nothing is
  fabricated).

Run: ``pytest tests/test_final_review_fix_rag_identity_chunker.py``.
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, MagicMock

from machina.rag.chunker import RecursiveCharacterTextSplitter
from machina.rag.embedder import _embedder_identity, resolve_embedding_settings

# ``import machina.core.config`` binds the Config singleton; resolve the module.
config_module = importlib.import_module("machina.core.config")


# ─── F08 harness ────────────────────────────────────────────────────────────


class _FakeConfig:
    """Minimal config stand-in so the identity never touches real backends."""

    def __init__(self, **values: str) -> None:
        self.values = values

    def get(self, key: str):
        return self.values.get(key, "")


def _setup_config(monkeypatch, **overrides: str) -> None:
    state = {
        "reranker_backend": "none",
        "embedding_backend": "openai",
        "embedding_model": "",
        "embedding_base_url": "https://api.openai.com/v1",
        "openai_api_key": "key-ONE",
        "openrouter_api_key": "",
        "rrf_keyword_weight": "1.0",
        "rrf_dense_weight": "1.0",
    }
    state.update(overrides)
    monkeypatch.setattr(config_module, "config", _FakeConfig(**state), raising=False)


def _stub_pipeline_embedder(monkeypatch) -> list:
    """replace ``pipeline.build_embedder`` with one that mirrors selection.

    It returns ``None`` for a keyword-only resolution (like the real factory)
    and a close-tracking fake otherwise, so the pipeline's owned client can be
    asserted closed on rebuild.
    """
    built: list = []

    def fake_build(settings=None):
        resolved = settings or resolve_embedding_settings()
        if resolved.kind == "none":
            return None
        embedder = MagicMock()
        embedder.model_name = resolved.model
        embedder.close = AsyncMock()
        built.append(embedder)
        return embedder

    monkeypatch.setattr("machina.rag.pipeline.build_embedder", fake_build)
    return built


def _reset_shared_pipeline(monkeypatch) -> None:
    import machina.tools.rag as rag_tools

    monkeypatch.setattr(rag_tools, "_rag_pipeline", None, raising=False)
    monkeypatch.setattr(rag_tools, "_rag_pipeline_identity", None, raising=False)


# ─── F08: rotated credential rebuilds the shared pipeline ───────────────────


async def test_shared_pipeline_identity_uses_shared_embedder_helper(monkeypatch):
    """Reuse, not duplicate: the pipeline identity is the embedder's identity."""
    import machina.tools.rag as rag_tools

    _setup_config(monkeypatch, openai_api_key="key-ONE")
    assert rag_tools._current_embedding_identity() == _embedder_identity(
        resolve_embedding_settings()
    )


async def test_shared_pipeline_rebuilds_on_embedding_key_rotation(monkeypatch):
    """F08: rotating a nonempty key must rebuild (the digest changes)."""
    import machina.tools.rag as rag_tools

    _setup_config(monkeypatch, openai_api_key="key-ONE")
    built = _stub_pipeline_embedder(monkeypatch)
    _reset_shared_pipeline(monkeypatch)

    first = await rag_tools.get_rag_pipeline()
    assert first._embedder is not None
    assert await rag_tools.get_rag_pipeline() is first
    assert len(built) == 1

    # Same backend/model/base_url, only the credential rotated.
    _setup_config(monkeypatch, openai_api_key="key-TWO")
    second = await rag_tools.get_rag_pipeline()
    assert second is not first
    assert len(built) == 2
    assert await rag_tools.get_rag_pipeline() is second


async def test_shared_pipeline_rebuilds_when_embedding_key_appears(monkeypatch):
    """F08: a keyword-only pipeline must go dense once a key appears."""
    import machina.tools.rag as rag_tools

    # openai backend with no key and no base_url resolves to keyword-only.
    _setup_config(monkeypatch, openai_api_key="", embedding_base_url="")
    built = _stub_pipeline_embedder(monkeypatch)
    _reset_shared_pipeline(monkeypatch)

    first = await rag_tools.get_rag_pipeline()
    assert first._embedder is None, "expected an honest keyword-only pipeline"
    assert await rag_tools.get_rag_pipeline() is first
    assert built == []

    # A credential appears; nothing else in the old identity changed.
    _setup_config(monkeypatch, openai_api_key="key-NEW", embedding_base_url="")
    second = await rag_tools.get_rag_pipeline()
    assert second is not first
    assert second._embedder is not None, "dense retrieval must be enabled"
    assert len(built) == 1
    assert await rag_tools.get_rag_pipeline() is second


async def test_replaced_pipeline_owned_embedder_is_closed(monkeypatch):
    """F08: the superseded pipeline's owned client is closed safely."""
    import machina.tools.rag as rag_tools

    _setup_config(monkeypatch, openai_api_key="key-ONE")
    _stub_pipeline_embedder(monkeypatch)
    _reset_shared_pipeline(monkeypatch)

    first = await rag_tools.get_rag_pipeline()
    old_embedder = first._embedder
    assert old_embedder is not None

    _setup_config(monkeypatch, openai_api_key="key-TWO")
    second = await rag_tools.get_rag_pipeline()
    assert second is not first
    old_embedder.close.assert_awaited_once()


# ─── P3-1: recursion retains the dropped leading separator ───────────────────


def _interior_coverage_holes(source: str, chunks) -> list[int]:
    """Indices between the first and last emission covered by no chunk."""
    spans = sorted((c.start, c.end) for c in chunks if c.start is not None and c.end is not None)
    assert spans, "expected offset-tracked chunks"
    covered = [False] * len(source)
    for start, end in spans:
        for index in range(start, end):
            covered[index] = True
    return [index for index in range(spans[0][0], spans[-1][1]) if not covered[index]]


def test_recursive_split_retains_dropped_leading_separator():
    """``"aaa\\n\\n\\n" + "W"*3000`` must not leave a source char uncovered.

    Pre-fix the recursed part ``"\\nW…"`` dropped its leading ``"\\n"``: the
    outer emission ended at index 5 and the first recursion emission began at
    index 6, so index 5 was covered by no chunk.
    """
    source = "aaa\n\n\n" + "W" * 3000
    splitter = RecursiveCharacterTextSplitter(chunk_size=512, chunk_overlap=76)
    chunks = splitter.split_text(source)

    # No fabrication: every emitted chunk (and its span) is exact source.
    for chunk in chunks:
        assert chunk.text in source
        if chunk.start is not None and chunk.end is not None:
            assert source[chunk.start : chunk.end] == chunk.text

    holes = _interior_coverage_holes(source, chunks)
    assert holes == [], f"source characters covered by no chunk: {holes}"


def test_recursive_split_leading_separator_keeps_adjacency():
    """The retained separator makes consecutive emissions abut in the source."""
    source = "aaa\n\n\n" + "W" * 3000
    splitter = RecursiveCharacterTextSplitter(chunk_size=512, chunk_overlap=76)
    chunks = splitter.split_text(source)

    tracked = [c for c in chunks if c.start is not None and c.end is not None]
    for prev, cur in zip(tracked, tracked[1:]):
        # Overlap may reach backward (cur.start < prev.end) but never leave a
        # forward gap: the dropped separator is now carried by a chunk.
        assert cur.start <= prev.end, f"forward coverage gap: {prev.end} -> {cur.start}"
