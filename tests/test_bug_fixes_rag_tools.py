"""Tests for Bug 12 (RAG ILIKE fallback) and Bug 13 (cd in ALLOWED_COMMANDS).

Bug 12: _bm25_search ILIKE fallback casts payload_msgpack::text which produces
hex instead of readable text. Fix: remove the broken fallback entirely.

Bug 13: 'cd' in ALLOWED_COMMANDS fails because it's a shell builtin and can't
work with shell=False. Fix: remove 'cd' from the allowlist.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import asyncpg
import pytest

from machina.rag.search import HybridSearch
from machina.tools.terminal import ALLOWED_COMMANDS, terminal

# ===========================================================================
# Bug 12: ILIKE fallback produces hex from payload_msgpack::text
# ===========================================================================


class TestBug12Bm25ILikeFallback:
    """Tests that the broken ILIKE fallback is removed."""

    @pytest.fixture
    def searcher(self):
        return HybridSearch()

    async def test_bm25_no_ilike_fallback_on_missing_column(self, searcher):
        """When search_text column is missing, _bm25_search should return
        empty results instead of attempting the broken ILIKE fallback."""
        mock_db = AsyncMock()
        # First call (FTS) raises UndefinedColumnError
        mock_db.fetch = AsyncMock(side_effect=asyncpg.UndefinedColumnError("column does not exist"))

        results = await searcher._bm25_search(
            session_id=uuid.uuid4(),
            query_text="test query",
            top_k=10,
            db=mock_db,
        )

        # Should return empty list, not attempt ILIKE
        assert results == []
        # fetch should only be called once (the FTS attempt)
        assert mock_db.fetch.call_count == 1

    async def test_bm25_no_payload_msgpack_text_cast(self, searcher):
        """Verify the ILIKE fallback query is completely removed from source."""
        import inspect

        source = inspect.getsource(searcher._bm25_search)
        # The broken pattern should not exist
        assert "payload_msgpack::text" not in source
        assert "ILIKE" not in source

    async def test_bm25_normal_path_still_works(self, searcher):
        """Normal FTS path should still work when search_text exists."""
        mock_db = AsyncMock()
        mock_db.fetch = AsyncMock(return_value=[])

        results = await searcher._bm25_search(
            session_id=uuid.uuid4(),
            query_text="test",
            top_k=10,
            db=mock_db,
        )

        assert results == []
        # Should have made the FTS call
        assert mock_db.fetch.call_count == 1
        # Verify it's the FTS query (contains ts_rank)
        call_args = mock_db.fetch.call_args
        query = call_args[0][0]
        assert "ts_rank" in query
        assert "to_tsquery" in query  # plainto_tsquery or to_tsquery via build_or_tsquery

    async def test_bm25_returns_results_on_success(self, searcher):
        """When FTS succeeds, results should be returned properly."""
        mock_db = AsyncMock()
        mock_row = {
            "id": uuid.uuid4(),
            "session_id": uuid.uuid4(),
            "agent_id": "test",
            "chunk_type": "document",
            "payload_msgpack": b"\x81\xa4text\xa4test",
            "token_count": 1,
            "embedding": None,
            "created_at": None,
            "accessed_at": None,
            "rank": 0.8,
        }
        mock_db.fetch = AsyncMock(return_value=[mock_row])

        results = await searcher._bm25_search(
            session_id=uuid.uuid4(),
            query_text="test",
            top_k=10,
            db=mock_db,
        )

        assert len(results) == 1
        assert results[0][1] == 0.8  # rank


# ===========================================================================
# Bug 13: cd in ALLOWED_COMMANDS fails with shell=False
# ===========================================================================


class TestBug13CdNotInAllowlist:
    """Tests that 'cd' is removed from ALLOWED_COMMANDS."""

    def test_cd_not_in_allowed_commands(self):
        """'cd' should not be in ALLOWED_COMMANDS."""
        assert "cd" not in ALLOWED_COMMANDS

    def test_other_commands_still_present(self):
        """Other common commands should still be in the allowlist."""
        expected = {
            "git",
            "ls",
            "cat",
            "grep",
            "find",
            "pytest",
            "echo",
            "pwd",
            "mkdir",
            "touch",
            "head",
            "tail",
            "wc",
            "diff",
        }
        assert expected.issubset(ALLOWED_COMMANDS)

    async def test_cd_command_rejected(self, monkeypatch):
        """Attempting to run 'cd' should raise ValidationError (ask-mode allowlist)."""
        # Full mode deliberately permits host commands; pin ask behavior here.
        monkeypatch.setenv("MACHINA_EXECUTION_MODE", "ask")
        from machina.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="not in the allowlist"):
            await terminal("cd /tmp")

    async def test_other_commands_not_affected(self, monkeypatch):
        """Removing 'cd' should not affect other allowed commands."""
        monkeypatch.setenv("MACHINA_TERMINAL_SANDBOX", "local")
        result = await terminal("git --version")
        assert isinstance(result, str)


def test_shared_pipeline_logs_traceback_on_cohere_failure(monkeypatch, caplog):
    """A failed Cohere build degrades to passthrough with its traceback logged."""
    from machina.core.config import config
    from machina.rag.reranker import IdentityReranker
    from machina.tools import rag as rag_tools

    real_get = config.get
    state = {"reranker_backend": "cohere", "cohere_api_key": "cohere-key-X"}

    def fake_get(key=None):
        if key in state:
            return state[key]
        return real_get(key)

    class BoomCohereReranker:
        DEFAULT_MODEL = "rerank-v3.5"

        def __init__(self, *args, **kwargs):
            raise RuntimeError("cohere init boom")

    monkeypatch.setattr(config, "get", fake_get)
    monkeypatch.setattr("machina.rag.reranker.CohereReranker", BoomCohereReranker)
    pipeline = rag_tools._build_shared_pipeline()
    assert isinstance(pipeline._reranker, IdentityReranker)
    assert "cohere unavailable" in pipeline._reranker.reason
    assert any("RuntimeError" in (record.exc_text or "") for record in caplog.records)


def test_shared_pipeline_rebuilds_when_cohere_key_identity_changes(monkeypatch):
    """Rotating one nonempty Cohere key to another must rebuild the shared
    pipeline: the cached client tracked key PRESENCE, so the old client bound
    to the old key served all later requests."""
    import asyncio

    from machina.core.config import config
    from machina.tools import rag as rag_tools

    keys = {"value": "cohere-key-ONE"}
    real_get = config.get

    def fake_get(key=None):
        if key == "cohere_api_key":
            return keys["value"]
        if key == "reranker_backend":
            return "cohere"
        return real_get(key)

    class FakeCohereReranker:
        DEFAULT_MODEL = "rerank-v3.5"

        def __init__(self, *args, **kwargs):
            self.api_key = keys["value"]

    monkeypatch.setattr(config, "get", fake_get)
    monkeypatch.setattr("machina.rag.reranker.CohereReranker", FakeCohereReranker)
    monkeypatch.setattr(rag_tools, "_rag_pipeline", None)
    monkeypatch.setattr(rag_tools, "_rag_pipeline_identity", None, raising=False)

    first = asyncio.run(rag_tools.get_rag_pipeline())
    assert first._reranker.api_key == "cohere-key-ONE"
    # Identity unchanged: the shared pipeline is reused.
    assert asyncio.run(rag_tools.get_rag_pipeline()) is first

    # Rotated key: a reranker bound to the NEW identity is built.
    keys["value"] = "cohere-key-TWO"
    second = asyncio.run(rag_tools.get_rag_pipeline())
    assert second is not first
    assert second._reranker.api_key == "cohere-key-TWO"
    assert asyncio.run(rag_tools.get_rag_pipeline()) is second
