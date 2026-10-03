"""Basic tests for AgentHarness."""

import pytest


def test_import():
    """Test that all modules import cleanly."""
    import ah

    assert ah.__version__ == "0.2.0"


def test_tool_registry():
    """Test tool registry has built-in tools."""
    from ah.tools import (
        builtins,  # noqa: F401 — registers web_search, web_extract, search_files
        file,  # noqa: F401 — registers read_file, write_file, list_files
        terminal,  # noqa: F401 — registers terminal
    )
    from ah.tools.base import registry

    tools = registry.list_tools()
    assert "read_file" in tools
    assert "write_file" in tools
    assert "list_files" in tools
    assert "terminal" in tools
    assert "web_search" in tools
    assert "web_extract" in tools
    assert "search_files" in tools


def test_session_dataclass():
    """Test Session dataclass creation."""
    import uuid

    from ah.core.models import Session

    s = Session(id=uuid.uuid4(), title="test", agent_id="harness")
    assert s.status == "active"
    assert s.context_budget == 8000


def test_context_chunk_dataclass():
    """Test ContextChunk dataclass creation."""
    import uuid

    from ah.core.models import ContextChunk

    c = ContextChunk(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        agent_id="harness",
        chunk_type="user_message",
        payload={"content": "hello"},
    )
    assert c.token_count == 0
    assert c.chunk_type == "user_message"


def test_prompt_assembler():
    """Test PromptAssembler basic functionality."""
    from ah.core.assembler import PromptAssembler

    assembler = PromptAssembler(session_budget=8000)
    prompt = assembler.assemble(
        system_prompt="You are a test agent.",
        goal="Test goal",
        recent_chunks=[{"type": "user_message", "payload": {"content": "hello"}}],
        retrieved_chunks=[],
        query="test query",
    )
    assert "You are a test agent." in prompt
    assert "Test goal" in prompt
    assert "test query" in prompt


@pytest.mark.asyncio
async def test_database_connection():
    """Test database connection (requires PostgreSQL)."""
    from ah.db.connection import db

    try:
        await db.connect()
        version = await db.fetchval("SELECT version()")
        assert "PostgreSQL" in version
    except Exception:
        pytest.skip("PostgreSQL not available")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_session_crud():
    """Test session CRUD operations (requires PostgreSQL)."""
    from ah.core.session import session_manager

    try:
        from ah.db.connection import db

        await db.connect()
    except Exception:
        pytest.skip("PostgreSQL not available")

    try:
        # Create
        session = await session_manager.create(title="test session")
        assert session.id is not None
        assert session.status == "active"

        # Get
        loaded = await session_manager.get(session.id)
        assert loaded is not None
        assert loaded.title == "test session"

        # Update
        await session_manager.update_state(session.id, {"key": "value"})
        loaded = await session_manager.get(session.id)
        assert loaded.state == {"key": "value"}

        # List
        sessions = await session_manager.list_sessions(limit=10)
        assert len(sessions) > 0

        # Archive
        await session_manager.archive(session.id)
        loaded = await session_manager.get(session.id)
        assert loaded.status == "archived"

    finally:
        await db.close()
