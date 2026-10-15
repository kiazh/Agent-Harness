"""Shared test fixtures and global state isolation."""

import asyncio
import os

import pytest
import pytest_asyncio

# Config keys that must never leak from the developer's real
# ~/.machina/config.yaml into tests (keeps tests hermetic and prevents
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
    "opencode_api_key",
    "custom_api_key",
    "cohere_api_key",
    "database_url",
)


def pytest_collection_modifyitems(items):
    """Expose actual DB fixtures through the documented release selection."""
    for item in items:
        if {"db_pool", "live_db"}.intersection(item.fixturenames):
            item.add_marker(pytest.mark.db)


@pytest.fixture(scope="session", autouse=True)
def gateway_scheduler_disabled():
    """Gateways never auto-start a background scheduler in the suite.

    The flag used to be set as a side effect of constructing ``Harness`` in
    ``tests/test_gateway.py`` and leaked into every later test (and any
    subprocess inheriting the environment). The suite owns it explicitly and
    restores the process environment afterwards.
    """
    name = "MACHINA_GATEWAY_NO_SCHEDULER"
    previous = os.environ.get(name)
    os.environ[name] = "1"
    yield
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


@pytest.fixture(autouse=True)
def isolate_config_secrets(monkeypatch):
    """Isolate developer credentials across all supported secret sources.

    Individual tests can explicitly configure fake environment, file, or
    backend values using monkeypatch after this fixture runs.
    """
    from machina.core.config import LEGACY_ENV_VARS, config
    from machina.security import secrets

    known_values = set(secrets._redaction_values)
    secrets._redaction_values.clear()

    saved = {k: getattr(config, k) for k in _SECRET_CONFIG_KEYS}
    monkeypatch.setenv("MACHINA_SECRET_BACKEND", "")
    for k in _SECRET_CONFIG_KEYS:
        setattr(config, k, "")
        # Config.get() falls back to dotenv-loaded environment values. Blank
        # those too so a fake-provider test cannot spend a developer's keys.
        monkeypatch.setenv(f"MACHINA_{k.upper()}", "")
        if k in LEGACY_ENV_VARS:
            monkeypatch.setenv(LEGACY_ENV_VARS[k], "")
            monkeypatch.setenv(f"{LEGACY_ENV_VARS[k]}_FILE", "")
    yield
    secrets._redaction_values.clear()
    secrets._redaction_values.update(known_values)
    for k, v in saved.items():
        setattr(config, k, v)


@pytest.fixture(scope="session", autouse=True)
def init_test_database():
    """Create the schema in the isolated test database once per session."""
    dsn = os.environ.get("MACHINA_TEST_DATABASE_URL")
    if not dsn:
        return
    from machina.db.connection import Database

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
def reset_singletons(monkeypatch):
    """Reset mutable global singleton state before each test.

    Modules import the singletons by name (``from machina.core.context import
    context_manager``), so rebinding the module global would not affect them.
    Instead, clear their state in place.
    """
    from machina.db.connection import db

    db._pool = None
    # Never let tests touch the developer database: use the isolated test DB,
    # or no DSN at all (DB-backed tests then skip).
    db.dsn = os.environ.get("MACHINA_TEST_DATABASE_URL", "")

    from machina.core.context import context_manager

    context_manager._pending.clear()

    from machina.core.session import session_manager

    session_manager._cache.clear()

    from machina.tools.base import registry

    registry._result_cache.clear()

    from machina.core import provider as provider_module

    provider_module._cache_clear()

    # The ChatGPT catalog module keeps in-process live/CLI-cache state; one
    # test's seeded catalog must not leak into the next (nor the developer's
    # real ~/.codex/models_cache.json, which individual tests re-isolate via
    # a fake HOME/CODEX_HOME when they need a hermetic catalog).
    from machina.core.provider import catalog as provider_catalog

    provider_catalog.clear_catalog_cache()

    # In-memory config is process-global: one test's config.set would leak
    # into the next. Snapshot non-secret values and session overrides.
    from machina.core.config import DEFAULTS, SECRET_KEYS, config

    saved_config = {key: getattr(config, key, None) for key in DEFAULTS if key not in SECRET_KEYS}
    saved_overrides = dict(getattr(config, "_session_overrides", {}) or {})
    # Test defaults must not depend on a developer's saved full-host mode,
    # workspace, persona, or live environment overrides. Individual tests
    # explicitly configure the behavior they exercise after this fixture.
    from machina.core.config import LEGACY_ENV_VARS

    for key, value in DEFAULTS.items():
        if key not in SECRET_KEYS:
            setattr(config, key, value)
            monkeypatch.delenv(f"MACHINA_{key.upper()}", raising=False)
            if key in LEGACY_ENV_VARS:
                monkeypatch.delenv(LEGACY_ENV_VARS[key], raising=False)
    config._session_overrides.clear()
    from machina.core.session_mode import reset_for_tests as reset_session_modes

    reset_session_modes()

    yield

    for key, value in saved_config.items():
        setattr(config, key, value)
    config._session_overrides.clear()
    config._session_overrides.update(saved_overrides)
    reset_session_modes()


@pytest_asyncio.fixture
async def db_pool():
    """A real test database, configured via MACHINA_TEST_DATABASE_URL.

    Skips the test when no test database is configured. Never hardcode
    credentials here.
    """
    dsn = os.environ.get("MACHINA_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("MACHINA_TEST_DATABASE_URL not set")
    from machina.db.connection import Database

    test_db = Database(dsn=dsn)
    await test_db.connect()
    await test_db.initialize_schema()
    yield test_db
    await test_db.close()
