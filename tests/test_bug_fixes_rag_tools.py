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

from ah.rag.search import HybridSearch
from ah.tools.terminal import ALLOWED_COMMANDS, terminal

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
        assert "plainto_tsquery" in query

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

    async def test_cd_command_rejected(self):
        """Attempting to run 'cd' should raise ValidationError."""
        from ah.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="not in the allowlist"):
            await terminal("cd /tmp")

    async def test_cd_with_path_rejected(self):
        """'cd' with any path should be rejected."""
        from ah.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="not in the allowlist"):
            await terminal("cd ..")

    async def test_other_commands_not_affected(self):
        """Removing 'cd' should not affect other allowed commands."""
        # pwd should still work (it's a real binary)
        result = await terminal("pwd")
        assert isinstance(result, str)
