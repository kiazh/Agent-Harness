"""Shared test fixtures and global state isolation."""

import asyncio
import os

import pytest
import pytest_asyncio

# Config keys that must never leak from the developer's real
# ~/.agent-harness/config.yaml into tests (keeps tests hermetic and prevents
# accidental real API calls with a personal key).
_SECRET_CONFIG_KEYS = (
    "openrouter_api_key",
    "openai_api_key",
    "anthropic_api_key",
    "google_api_key",
    "mistral_api_key",
    "groq_api_key",
    "together_api_key",
    "deepseek_api_key",
    "xai_api_key",
    "cohere_api_key",
    "database_url",
)


@pytest.fixture(autouse=True)
def isolate_config_secrets():
    """Blank secrets loaded from the user's config file for each test.

    ``Config.get`` falls back to the legacy env vars when the attribute is
    empty, so tests that patch ``os.environ`` still work as intended.
    """
    from ah.core.config import config

    saved = {k: getattr(config, k) for k in _SECRET_CONFIG_KEYS}
    for k in _SECRET_CONFIG_KEYS:
        setattr(config, k, "")
    yield
    for k, v in saved.items():
        setattr(config, k, v)


@pytest.fixture(scope="session", autouse=True)
def init_test_database():
    """Create the schema in the isolated test database once per session."""
    dsn = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL")
    if not dsn:
        return
    from ah.db.connection import Database

    async def _init() -> None:
        test_db = Database(dsn=dsn)
        try:
            await test_db.connect()
            await test_db.initialize_schema()
        finally:
            await test_db.close()

    try:
        asyncio.run(_init())
    except Exception:
        pass  # DB-backed tests will skip on their own if the DB is unreachable


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset mutable global singleton state before each test.

    Modules import the singletons by name (``from ah.core.context import
    context_manager``), so rebinding the module global would not affect them.
    Instead, clear their state in place.
    """
    from ah.db.connection import db

    db._pool = None
    # Never let tests touch the developer database: use the isolated test DB,
    # or no DSN at all (DB-backed tests then skip).
    db.dsn = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL", "")

    from ah.core.context import context_manager

    context_manager._pending.clear()

    from ah.core.session import session_manager

    session_manager._cache.clear()

    from ah.tools.base import registry

    registry._result_cache.clear()

    from ah.core import provider as provider_module

    provider_module._cache_clear()

    yield


@pytest.fixture
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_pool():
    """A real test database, configured via AGENT_HARNESS_TEST_DATABASE_URL.

    Skips the test when no test database is configured. Never hardcode
    credentials here.
    """
    dsn = os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("AGENT_HARNESS_TEST_DATABASE_URL not set")
    from ah.db.connection import Database

    test_db = Database(dsn=dsn)
    await test_db.connect()
    await test_db.initialize_schema()
    yield test_db
    await test_db.close()
