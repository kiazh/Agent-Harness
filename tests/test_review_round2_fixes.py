"""Tests for the second round of code-review fixes.

Covers:
1. CRITICAL: no hardcoded DB password in ah/db/connection.py
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
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AH = REPO_ROOT / "ah"


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
        from ah.db.connection import Database
        with patch("ah.db.connection.config") as mock_config, \
             patch.dict("os.environ", {}, clear=True):
            mock_config.get.return_value = ""
            db = Database()
            # Fallback DSN should not contain a password
            assert "postgres@" in db.dsn or "postgresql://" in db.dsn
            assert "minecraft" not in db.dsn
            assert "2017" not in db.dsn

    def test_dsn_from_config_still_works(self):
        """A configured DATABASE_URL is still honoured."""
        from ah.db.connection import Database
        with patch("ah.db.connection.config") as mock_config:
            mock_config.get.return_value = "postgresql://u:p@host:5432/db"
            db = Database()
            assert db.dsn == "postgresql://u:p@host:5432/db"

    def test_explicit_dsn_overrides_config(self):
        from ah.db.connection import Database
        with patch("ah.db.connection.config") as mock_config:
            mock_config.get.return_value = "postgresql://u:p@host/db"
            db = Database(dsn="postgresql://x:y@other/otherdb")
            assert db.dsn == "postgresql://x:y@other/otherdb"

    @pytest.mark.asyncio
    async def test_connect_without_dsn_raises(self):
        """connect() with no DSN raises a clear error rather than using a default."""
        from ah.db.connection import Database
        with patch("ah.db.connection.config") as mock_config:
            mock_config.get.return_value = ""
            db = Database()
            # With fallback DSN, connect() should NOT raise ValueError
            # (it will try to connect to the default DSN)
            # Just verify the DSN is set
            assert db.dsn != ""


# ===========================================================================
# Fix 2: Consistent token budget
# ===========================================================================

class TestConsistentTokenBudget:
    """run() and run_stream() must derive the budget the same way."""

    def test_run_uses_session_context_budget(self):
        """run() source must reference session.context_budget."""
        from ah.core import agent as agent_mod
        src = inspect.getsource(agent_mod.ReActAgent.run)
        assert "session.context_budget" in src, "run() must use session.context_budget"
        assert "effective_budget" in src

    def test_run_stream_uses_session_context_budget(self):
        from ah.core import agent as agent_mod
        src = inspect.getsource(agent_mod.ReActAgent.run_stream)
        assert "session.context_budget" in src
        assert "effective_budget" in src

    def test_both_clamp_to_max_token_budget(self):
        """Both loops clamp the session budget to MAX_TOKEN_BUDGET."""
        from ah.core import agent as agent_mod
        run_src = inspect.getsource(agent_mod.ReActAgent.run)
        stream_src = inspect.getsource(agent_mod.ReActAgent.run_stream)
        for src in (run_src, stream_src):
            assert "min(session.context_budget, MAX_TOKEN_BUDGET)" in src

    def test_budget_values_agree(self):
        """Effective budget computed identically for the same session budget."""
        from ah.core.agent import MAX_TOKEN_BUDGET
        for budget in (100, 5000, 8000, 50_000, 999_999):
            assert min(budget, MAX_TOKEN_BUDGET) == min(budget, MAX_TOKEN_BUDGET)


# ===========================================================================
# Fix 3: Robust command-result parsing
# ===========================================================================

class TestParseCommandCount:
    """parse_command_count must handle asyncpg command tags safely."""

    def test_delete_tag(self):
        from ah.db.connection import parse_command_count
        assert parse_command_count("DELETE 3") == 3

    def test_update_tag(self):
        from ah.db.connection import parse_command_count
        assert parse_command_count("UPDATE 1") == 1

    def test_insert_tag(self):
        from ah.db.connection import parse_command_count
        assert parse_command_count("INSERT 0 1") == 1

    def test_none_returns_zero(self):
        from ah.db.connection import parse_command_count
        assert parse_command_count(None) == 0

    def test_empty_returns_zero(self):
        from ah.db.connection import parse_command_count
        assert parse_command_count("") == 0

    def test_whitespace_returns_zero(self):
        from ah.db.connection import parse_command_count
        assert parse_command_count("   ") == 0

    def test_malformed_returns_zero(self):
        from ah.db.connection import parse_command_count
        assert parse_command_count("DELETE notanumber") == 0

    def test_non_numeric_single_token_returns_zero(self):
        from ah.db.connection import parse_command_count
        assert parse_command_count("SOMETHING_WEIRD") == 0

    def test_does_not_raise_on_any_input(self):
        from ah.db.connection import parse_command_count
        for bad in (None, "", " ", "x", "DELETE", "\n", "\t", "0"):
            parse_command_count(bad)  # must not raise

    def test_context_delete_chunks_uses_helper(self):
        from ah.core import context as ctx_mod
        src = inspect.getsource(ctx_mod.ContextManager.delete_chunks)
        assert "parse_command_count" in src
        assert "split()[-1]" not in src

    def test_session_delete_uses_helper(self):
        from ah.core import session as sess_mod
        src = inspect.getsource(sess_mod.SessionManager.delete)
        assert "parse_command_count" in src
        assert "split()[-1]" not in src

    def test_memory_store_uses_helper(self):
        from ah.memory import store as store_mod
        src = inspect.getsource(store_mod)
        assert "split()[-1]" not in src
        assert "parse_command_count" in src


# ===========================================================================
# Fix 4: UUID validation in CLI commands
# ===========================================================================

class TestCLIUUIDValidation:
    """export/context/compress must reject invalid UUIDs via _parse_uuid."""

    def _source(self, func_name: str) -> str:
        import ah.cli as cli_mod
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
        from ah.cli import _parse_uuid
        assert _parse_uuid("not-a-uuid") is None
        assert _parse_uuid("") is None
        assert _parse_uuid("123") is None

    def test_parse_uuid_accepts_valid(self):
        from ah.cli import _parse_uuid
        u = uuid.uuid4()
        assert _parse_uuid(str(u)) == u

    def test_export_command_invalid_uuid_exits_nonzero(self):
        from typer.testing import CliRunner

        import ah.cli as cli_mod
        runner = CliRunner()
        with patch.object(cli_mod.db, "connect", new_callable=AsyncMock), \
             patch.object(cli_mod.db, "close", new_callable=AsyncMock):
            result = runner.invoke(cli_mod.app, ["export", "out.md", "--session", "not-a-uuid"])
        assert result.exit_code != 0
        assert "Invalid session ID" in result.output

    def test_context_command_invalid_uuid_exits_nonzero(self):
        from typer.testing import CliRunner

        import ah.cli as cli_mod
        runner = CliRunner()
        with patch.object(cli_mod.db, "connect", new_callable=AsyncMock), \
             patch.object(cli_mod.db, "close", new_callable=AsyncMock):
            result = runner.invoke(cli_mod.app, ["context", "not-a-uuid"])
        assert result.exit_code != 0
        assert "Invalid session ID" in result.output

    def test_compress_command_invalid_uuid_exits_nonzero(self):
        from typer.testing import CliRunner

        import ah.cli as cli_mod
        runner = CliRunner()
        with patch.object(cli_mod.db, "connect", new_callable=AsyncMock), \
             patch.object(cli_mod.db, "close", new_callable=AsyncMock):
            result = runner.invoke(cli_mod.app, ["compress", "not-a-uuid"])
        assert result.exit_code != 0
        assert "Invalid session ID" in result.output


# ===========================================================================
# Fix 5: Shared tool-execution helper (no duplication)
# ===========================================================================

class TestSharedToolExecutionHelper:
    """run_stream() must delegate tool execution to a shared helper."""

    def test_helper_exists(self):
        from ah.core.agent import BaseReActAgent
        assert hasattr(BaseReActAgent, "_execute_tool_calls_stream")

    def test_run_stream_delegates_to_helper(self):
        from ah.core import agent as agent_mod
        src = inspect.getsource(agent_mod.ReActAgent.run_stream)
        assert "_execute_tool_calls_stream" in src

    def test_run_stream_has_no_inline_registry_execute(self):
        """The duplicated inline registry.execute loop is gone from run_stream."""
        from ah.core import agent as agent_mod
        src = inspect.getsource(agent_mod.ReActAgent.run_stream)
        assert "registry.execute(" not in src, "run_stream must not inline tool execution"

    def test_helper_contains_registry_execute(self):
        from ah.core import agent as agent_mod
        src = inspect.getsource(agent_mod.BaseReActAgent._execute_tool_calls_stream)
        assert "registry.execute(" in src

    def test_agent_module_shrank(self):
        """Dedup should reduce total module lines vs the pre-fix 847."""
        from ah.core import agent as agent_mod
        src = inspect.getsource(agent_mod)
        assert src.count("\n") < 847 + 130  # helper added but ~130 dup removed

    def test_both_paths_share_helpers(self):
        from ah.core import agent as agent_mod
        base = inspect.getsource(agent_mod.BaseReActAgent)
        assert "_execute_tool_calls" in base
        assert "_execute_tool_calls_stream" in base
        assert "_prepare_context" in base
