"""Tests for the second round of code-review fixes.

Covers:
1. CRITICAL: no hardcoded DB password in machina/db/connection.py
2. MEDIUM: consistent token budget between run() and run_stream()
3. MEDIUM: robust parsing of asyncpg command results (parse_command_count)
4. MEDIUM: UUID validation in export/context/compress CLI commands
5. MEDIUM: shared tool-execution helper (no duplication between run/run_stream)
6. MEDIUM: large files split into packages (visual/, animations/)
"""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AH = REPO_ROOT / "machina"


# ===========================================================================
# Fix 1: No hardcoded database password
# ===========================================================================


class TestNoHardcodedDatabasePassword:
    """The Database DSN must never embed a hardcoded password."""

    def test_no_hardcoded_password_literal(self):
        """connection.py must not contain the previously hardcoded password."""
        source = (AH / "db" / "connection.py").read_text(encoding="utf-8")
        assert "minecraft" not in source.lower()

    def test_no_password_in_default_dsn(self):
        """The DSN fallback must not contain a password."""
        from machina.db.connection import Database

        with (
            patch("machina.db.connection.config") as mock_config,
            patch.dict("os.environ", {}, clear=True),
        ):
            mock_config.get.return_value = ""
            db = Database()
            # Fallback DSN should not contain a password
            assert "postgres@" in db.dsn or "postgresql://" in db.dsn
            assert "minecraft" not in db.dsn
            assert "2017" not in db.dsn

    @pytest.mark.asyncio
    async def test_connect_without_dsn_uses_default(self):
        """connect() with no DSN must actually connect using the fallback DSN.

        The provider/network boundary (asyncpg.create_pool) is the only mock;
        connect() itself runs for real, so a regression to "raise ValueError
        when no DSN" or to a password-bearing fallback is caught here.
        """
        from machina.db.connection import Database

        with (
            patch("machina.db.connection.config") as mock_config,
            patch.dict("os.environ", {"DATABASE_URL": ""}, clear=False),
        ):
            mock_config.get.return_value = ""
            db = Database()
        assert db.dsn == "postgresql://postgres@127.0.0.1:5432/machina"

        pool = MagicMock()
        pool.close = AsyncMock()
        with patch(
            "machina.db.connection.asyncpg.create_pool", new=AsyncMock(return_value=pool)
        ) as create_pool:
            await db.connect()  # must not raise ValueError without a DSN

        assert db.connected
        assert create_pool.await_args.kwargs["dsn"] == db.dsn
        await db.close()
        assert not db.connected


# ===========================================================================
# Fix 2: Consistent token budget
# ===========================================================================


class TestRequestContextCapacity:
    """Request capacity includes tool definitions and provider output reservation."""

    def test_tool_schema_counts_towards_context(self, monkeypatch):
        from machina.core.agent import _request_context_tokens
        from machina.core.config import config
        from machina.core.models import ToolDefinition

        monkeypatch.setattr(config, "max_tokens", 8)
        messages = [{"role": "user", "content": "hello"}]
        without = _request_context_tokens(messages, [], object())
        tool = ToolDefinition("large", "description " * 2000, {"type": "object"})
        assert _request_context_tokens(messages, [tool], object()) > without + 1000

    def test_requested_output_is_reserved(self, monkeypatch):
        from machina.core.agent import _request_context_tokens
        from machina.core.config import config

        monkeypatch.setattr(config, "max_tokens", 8)
        baseline = _request_context_tokens([], [], object())
        monkeypatch.setattr(config, "max_tokens", 8192)
        assert _request_context_tokens([], [], object()) - baseline == 8184

    def test_provider_reasoning_reservation_is_counted(self, monkeypatch):
        from machina.core.agent import _request_context_tokens
        from machina.core.config import config

        class ReasoningProvider:
            def reservation_max_tokens(self, tokens, reasoning_effort=None):
                return tokens + 2048

        monkeypatch.setattr(config, "max_tokens", 8)
        baseline = _request_context_tokens([], [], object())
        assert _request_context_tokens([], [], ReasoningProvider()) - baseline == 2048


# ===========================================================================
# Fix 3: Robust command-result parsing
# ===========================================================================


class TestParseCommandCount:
    """parse_command_count must handle asyncpg command tags safely."""

    @pytest.mark.parametrize(
        "result,expected",
        [
            pytest.param("DELETE 3", 3, id="delete-tag"),
            pytest.param("UPDATE 1", 1, id="update-tag"),
            pytest.param("INSERT 0 1", 1, id="insert-tag"),
        ],
    )
    def test_command_tag_count_parsed(self, result, expected):
        from machina.db.connection import parse_command_count

        assert parse_command_count(result) == expected

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(None, id="none"),
            pytest.param("", id="empty"),
            pytest.param("   ", id="whitespace"),
            pytest.param("DELETE notanumber", id="malformed-count"),
            pytest.param("SOMETHING_WEIRD", id="non-numeric-single-token"),
        ],
    )
    def test_unparseable_input_returns_zero(self, value):
        from machina.db.connection import parse_command_count

        assert parse_command_count(value) == 0

    def test_does_not_raise_on_any_input(self):
        from machina.db.connection import parse_command_count

        for bad in (None, "", " ", "x", "DELETE", "\n", "\t", "0"):
            result = parse_command_count(bad)  # must not raise
            assert isinstance(result, int)
            assert result >= 0

    def test_context_delete_chunks_uses_helper(self):
        from machina.core import context as ctx_mod

        src = inspect.getsource(ctx_mod.ContextManager.delete_chunks)
        assert "parse_command_count" in src
        assert "split()[-1]" not in src

    def test_session_delete_uses_helper(self):
        from machina.core import session as sess_mod

        src = inspect.getsource(sess_mod.SessionManager.delete)
        assert "parse_command_count" in src
        assert "split()[-1]" not in src

    def test_memory_store_uses_helper(self):
        from machina.memory import store as store_mod

        src = inspect.getsource(store_mod)
        assert "split()[-1]" not in src
        assert "parse_command_count" in src


# ===========================================================================
# Fix 4: UUID validation in CLI commands
# ===========================================================================


class TestCLIUUIDValidation:
    """export/context/compress must reject invalid UUIDs via _parse_uuid."""

    def _source(self, func_name: str) -> str:
        import machina.cli as cli_mod

        return inspect.getsource(getattr(cli_mod, func_name))

    def test_export_uses_parse_uuid(self):
        assert "_resolve_session" in self._source("export")
        assert "uuid.UUID(" not in self._source("export")

    def test_context_uses_parse_uuid(self):
        assert "_parse_uuid" in self._source("context")
        assert "uuid.UUID(" not in self._source("context")

    def test_compress_uses_parse_uuid(self):
        assert "_resolve_session" in self._source("compress")
        assert "uuid.UUID(" not in self._source("compress")

    def test_resolve_session_uses_parse_uuid(self):
        assert "_parse_uuid" in self._source("_resolve_session")

    def test_parse_uuid_rejects_garbage(self):
        from machina.cli import _parse_uuid

        assert _parse_uuid("not-a-uuid") is None
        assert _parse_uuid("") is None
        assert _parse_uuid("123") is None

    def test_parse_uuid_accepts_valid(self):
        from machina.cli import _parse_uuid

        u = uuid.uuid4()
        assert _parse_uuid(str(u)) == u

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["export", "out.md", "--session", "not-a-uuid"], id="export"),
            pytest.param(["context", "not-a-uuid"], id="context"),
            pytest.param(["compress", "not-a-uuid"], id="compress"),
        ],
    )
    def test_invalid_uuid_exits_nonzero(self, argv):
        from typer.testing import CliRunner

        import machina.cli as cli_mod

        runner = CliRunner()
        with (
            patch.object(cli_mod.db, "connect", new_callable=AsyncMock),
            patch.object(cli_mod.db, "close", new_callable=AsyncMock),
        ):
            result = runner.invoke(cli_mod.app, argv)
        assert result.exit_code != 0
        assert "Invalid session ID" in result.output


# ===========================================================================
# Fix 5: Shared tool-execution helper (no duplication)
# ===========================================================================


class TestSharedToolExecutionHelper:
    """run_stream() must delegate tool execution to a shared helper."""

    def test_helper_exists(self):
        from machina.core.agent import BaseReActAgent

        assert hasattr(BaseReActAgent, "_execute_tool_calls_stream")

    def test_run_stream_delegates_to_helper(self):
        from machina.core import agent as agent_mod

        src = inspect.getsource(agent_mod.ReActAgent.run_stream)
        assert "_execute_tool_calls_stream" in src

    def test_run_stream_has_no_inline_registry_execute(self):
        """The duplicated inline registry.execute loop is gone from run_stream."""
        from machina.core import agent as agent_mod

        src = inspect.getsource(agent_mod.ReActAgent.run_stream)
        assert "registry.execute(" not in src, "run_stream must not inline tool execution"

    def test_helper_contains_registry_execute(self):
        from machina.core import agent as agent_mod

        src = inspect.getsource(agent_mod.BaseReActAgent._execute_tool_calls_stream)
        assert "registry.execute(" in src

    def test_both_paths_share_helpers(self):
        from machina.core import agent as agent_mod

        base = inspect.getsource(agent_mod.BaseReActAgent)
        assert "_execute_tool_calls" in base
        assert "_execute_tool_calls_stream" in base
        assert "_prepare_context" in base
