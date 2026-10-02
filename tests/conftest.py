"""Shared test fixtures and global singleton reset."""
import asyncio
import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset all global singletons before each test."""
    # Reset db
    from ah.db.connection import db as db_singleton
    db_singleton._pool = None
    db_singleton._initialized = False

    # Reset context_manager
    from ah.core.context import context_manager, ContextManager
    context_manager = ContextManager()

    # Reset session_manager
    from ah.core.session import session_manager, SessionManager
    session_manager = SessionManager()

    # Reset registry (but don't clear — tests share the registry state)
    # The registry is populated by imports at module level, so clearing
    # would break tests that rely on built-in tools being registered.

    # Reset memory_store
    from ah.memory.store import memory_store, MemoryStore
    memory_store = MemoryStore()

    # Reset skill_registry
    from ah.skills.registry import skill_registry, SkillRegistry
    skill_registry = SkillRegistry()

    yield


@pytest.fixture
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_pool():
    """Create a test database pool."""
    from ah.db.connection import Database
    test_db = Database(
        dsn="postgresql://postgres:minecraft@2017@localhost:5432/agentharness_test",
        min_size=1,
        max_size=5,
    )
    await test_db.initialize()
    yield test_db
    await test_db.close()
