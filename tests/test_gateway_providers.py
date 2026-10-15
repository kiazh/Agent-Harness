"""Gateway ``models.*`` / ``providers.*`` handlers.

No network: catalog fetches and OpenAI-compatible pulls are monkeypatched at
the module seam the handlers themselves call at request time. Provider-store
paths are sandboxed through MACHINA_PROVIDERS_FILE / MACHINA_ENV_FILE.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from machina.gateway.errors import RpcError
from machina.gateway.features import providers_feature as feature

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "model_catalog_chatgpt.json"


def _gw(provider: str = "chatgpt", model: str = "gpt-6.1-sol") -> SimpleNamespace:
    return SimpleNamespace(
        require_db=lambda: None,
        provider=provider,
        model=model,
        # No client-opened session by default: the gateway defaults stand in.
        opened_session_id=lambda: None,
    )


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolated provider store + env file + empty Codex cache."""
    monkeypatch.setenv("MACHINA_PROVIDERS_FILE", str(tmp_path / "providers.json"))
    monkeypatch.setenv("MACHINA_ENV_FILE", str(tmp_path / ".env"))
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    from machina.core.provider import catalog

    catalog.clear_catalog_cache()
    yield tmp_path
    catalog.clear_catalog_cache()


def _seed_live_catalog(monkeypatch) -> None:
    from machina.core.provider import catalog

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    models = catalog.parse_catalog(payload, source="live")
    monkeypatch.setattr(catalog, "fetch_live_catalog", lambda *args, **kwargs: models)
    catalog.refresh_live_catalog("token-1", "acct-1", force=True)


# ─── models.list ────────────────────────────────────────────────────────────


async def test_models_list_chatgpt_returns_live_catalog(sandbox, monkeypatch):
    from machina.core.provider import catalog

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    models = catalog.parse_catalog(payload, source="live")
    monkeypatch.setattr(catalog, "fetch_catalog_for_account", lambda **kwargs: models)

    out = await feature.models_list(_gw(), {})
    assert out["provider"] == "chatgpt"
    assert out["source"] == "live"
    assert out["fetchError"] is None
    assert [row["id"] for row in out["models"]] == ["gpt-6.1-sol", "gpt-6-luna"]
    sol = out["models"][0]
    assert sol["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert sol["contextWindow"] == 500_000
    assert sol["maxContextWindow"] == 872_000
    luna = out["models"][1]
    assert "ultra" not in luna["advertisedEfforts"]  # luna never advertises ultra


async def test_models_list_chatgpt_falls_back_honestly(sandbox, monkeypatch):
    from machina.core.provider import catalog

    def failing(**kwargs):
        raise catalog.CatalogFetchError("catalog fetch returned HTTP 503")

    monkeypatch.setattr(catalog, "fetch_catalog_for_account", failing)
    out = await feature.models_list(_gw(), {})
    assert out["source"] == "fallback"
    assert "HTTP 503" in out["fetchError"]
    assert out["models"]  # bundled verified rows keep the surface useful
    assert out["models"][0]["id"] == "gpt-6.1-sol"


async def test_models_list_named_provider_pulls_and_caches(sandbox, monkeypatch):
    from machina.security import providers as store

    store.add_provider("acme", "http://127.0.0.1:9000/v1")
    calls = {"count": 0}

    def fake_fetch(base_url, api_key=""):
        calls["count"] += 1
        return [{"id": "m1", "contextWindow": 32768}, {"id": "m2"}]

    monkeypatch.setattr(store, "fetch_models", fake_fetch)
    out = await feature.models_list(_gw(provider="acme", model="m1"), {})
    assert [row["id"] for row in out["models"]] == ["m1", "m2"]
    assert out["source"] == "live"
    # Second read inside the TTL serves the cache: no second pull.
    out2 = await feature.models_list(_gw(provider="acme", model="m1"), {})
    assert out2["models"] == out["models"]
    assert calls["count"] == 1
    # refresh=true forces a new pull.
    await feature.models_list(_gw(provider="acme", model="m1"), {"refresh": True})
    assert calls["count"] == 2


async def test_models_list_rejects_unknown_provider(sandbox):
    with pytest.raises(RpcError):
        await feature.models_list(_gw(provider="ghost"), {})


async def test_models_list_resolves_the_opened_session_route(sandbox, monkeypatch):
    """Round-3 F10: without explicit identity, the last created/resumed
    session's pinned provider answers — not the gateway default."""
    import uuid
    from unittest.mock import AsyncMock

    from machina.core.models import Session
    from machina.core.provider import catalog

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    models = catalog.parse_catalog(payload, source="live")
    monkeypatch.setattr(catalog, "fetch_catalog_for_account", lambda **kwargs: models)
    session = Session(id=uuid.uuid4(), title="resumed", model="gpt-6.1-sol", provider="chatgpt")
    monkeypatch.setattr(feature.session_manager, "get", AsyncMock(return_value=session))

    gw = _gw(provider="openrouter", model="openrouter/free")
    gw.opened_session_id = lambda: session.id
    out = await feature.models_list(gw, {})
    assert out["provider"] == "chatgpt"
    assert [row["id"] for row in out["models"]] == ["gpt-6.1-sol", "gpt-6-luna"]


async def test_models_list_explicit_session_and_provider_win(sandbox):
    import uuid
    from unittest.mock import AsyncMock

    from machina.core.models import Session

    session = Session(id=uuid.uuid4(), title="resumed", model="gpt-6.1-sol", provider="chatgpt")
    gw = _gw(provider="openrouter", model="openrouter/free")
    gw.get_session = AsyncMock(return_value=session)
    # Explicit session identity resolves the pinned provider.
    out = await feature.models_list(gw, {"sessionId": str(session.id)})
    assert out["provider"] == "chatgpt"
    assert gw.get_session.await_count == 1
    # An explicit provider parameter still wins (cross-provider listings).
    out = await feature.models_list(gw, {"sessionId": str(session.id), "provider": "openai"})
    assert out["provider"] == "openai"
    assert out["source"] == "none"


async def test_models_list_falls_back_when_the_opened_session_is_gone(sandbox, monkeypatch):
    import uuid
    from unittest.mock import AsyncMock

    monkeypatch.setattr(feature.session_manager, "get", AsyncMock(return_value=None))
    gw = _gw(provider="openrouter", model="openrouter/free")
    gw.opened_session_id = lambda: uuid.uuid4()
    out = await feature.models_list(gw, {})
    assert out["provider"] == "openrouter"


# ─── models.setEffort ───────────────────────────────────────────────────────


async def test_set_effort_chatgpt_validates_per_model(sandbox, monkeypatch):
    _seed_live_catalog(monkeypatch)
    from machina.core.config import config

    out = await feature.models_set_effort(_gw(model="gpt-6-luna"), {"effort": "max"})
    assert out["effort"] == "max"
    assert out["supported"] == ["low", "medium", "high", "xhigh", "max"]
    assert config.get("reasoning_effort") == "max"

    with pytest.raises(RpcError) as excinfo:
        await feature.models_set_effort(_gw(model="gpt-6-luna"), {"effort": "ultra"})
    assert "supported: low, medium, high, xhigh, max" in str(excinfo.value)


async def test_set_effort_non_chatgpt_keeps_wire_vocabulary(sandbox):
    from machina.core.config import config

    out = await feature.models_set_effort(
        _gw(provider="openai", model="gpt-4o"), {"effort": "high"}
    )
    assert out["effort"] == "high"
    assert out["supported"] == ["low", "medium", "high"]
    assert config.get("reasoning_effort") == "high"

    with pytest.raises(RpcError) as excinfo:
        await feature.models_set_effort(_gw(provider="openai", model="gpt-4o"), {"effort": "xhigh"})
    assert "supported: low, medium, high" in str(excinfo.value)


async def test_set_effort_off_clears(sandbox, monkeypatch):
    _seed_live_catalog(monkeypatch)
    from machina.core.config import config

    out = await feature.models_set_effort(_gw(), {"effort": "off"})
    assert out["effort"] is None
    assert config.get("reasoning_effort") == ""


async def test_set_effort_unset_aliases_clear_outside_chatgpt(sandbox):
    """Round-3 F11: the UI advertises ``off``; every unset alias clears the
    setting for non-ChatGPT providers instead of failing validation."""
    from machina.core.config import config

    gw = _gw(provider="openrouter", model="openrouter/free")
    for alias in ("off", "OFF", "none", "default", "auto", ""):
        config.set("reasoning_effort", "high")
        out = await feature.models_set_effort(gw, {"effort": alias})
        assert out["effort"] is None, alias
        assert config.get("reasoning_effort") == "", alias


async def test_set_effort_validates_against_the_opened_session_route(sandbox, monkeypatch):
    """Round-3 F10: effort levels validate against the resumed session's
    pinned model (``max`` is valid there, invalid for the gw defaults)."""
    import uuid
    from unittest.mock import AsyncMock

    from machina.core.models import Session

    _seed_live_catalog(monkeypatch)
    session = Session(id=uuid.uuid4(), title="resumed", model="gpt-6-luna", provider="chatgpt")
    monkeypatch.setattr(feature.session_manager, "get", AsyncMock(return_value=session))
    gw = _gw(provider="openrouter", model="openrouter/free")
    gw.opened_session_id = lambda: session.id

    out = await feature.models_set_effort(gw, {"effort": "max"})
    assert out["provider"] == "chatgpt"
    assert out["model"] == "gpt-6-luna"
    assert out["effort"] == "max"
    assert "max" in out["supported"]

    cleared = await feature.models_set_effort(gw, {"effort": "off"})
    assert cleared["effort"] is None

    # The same effort is still rejected when the session route is not ChatGPT.
    rejected = _gw(provider="openrouter", model="openrouter/free")
    with pytest.raises(RpcError):
        await feature.models_set_effort(rejected, {"effort": "max"})


# ─── providers.list / add / remove ──────────────────────────────────────────


async def test_providers_list_reports_builtin_and_named(sandbox):
    from machina.security import providers as store

    store.add_provider("acme", "http://127.0.0.1:9000/v1", api_key="sk-1")
    store.save_models("acme", [{"id": "m1"}, {"id": "m2"}])
    out = await feature.providers_list(_gw(provider="acme"), {})
    assert out["selected"] == "acme"
    names = [row["name"] for row in out["providers"]]
    assert "chatgpt" in names and "acme" in names
    acme = next(row for row in out["providers"] if row["name"] == "acme")
    assert acme["kind"] == "named"
    assert acme["keySet"] is True
    assert acme["keylessLocal"] is True  # 127.0.0.1
    assert acme["selected"] is True
    # The /keys merge in the UI reads exactly these fields.
    assert acme["apiKeyEnv"] == "MACHINA_PROVIDER_ACME_API_KEY"
    assert acme["modelsCount"] == 2


async def test_providers_add_saves_key_and_pulls_models(sandbox, monkeypatch):
    from machina.security import providers as store

    monkeypatch.setattr(
        store, "fetch_models", lambda base_url, api_key="": [{"id": "m1"}, {"id": "m2"}]
    )
    out = await feature.providers_add(
        _gw(provider="openrouter"),
        {"name": "acme", "baseUrl": "http://127.0.0.1:9000/v1", "apiKey": "sk-secret"},
    )
    assert out["provider"] == "acme"
    assert out["keySaved"] is True
    assert out["keySet"] is True
    assert [row["id"] for row in out["models"]] == ["m1", "m2"]
    assert out["fetchError"] is None
    entry = store.get_provider_entry("acme")
    assert store.resolve_provider_key(entry) == "sk-secret"
    env_text = Path(os.environ["MACHINA_ENV_FILE"]).read_text(encoding="utf-8")
    assert "sk-secret" in env_text


async def test_providers_add_reports_fetch_failure_but_keeps_entry(sandbox, monkeypatch):
    from machina.security import providers as store

    def failing(base_url, api_key=""):
        raise store.ProviderStoreError(f"could not reach {base_url} (ConnectError)")

    monkeypatch.setattr(store, "fetch_models", failing)
    out = await feature.providers_add(
        _gw(provider="openrouter"),
        {"name": "flaky", "baseUrl": "http://127.0.0.1:9000/v1"},
    )
    assert out["models"] == []
    assert "could not reach" in out["fetchError"]
    assert store.get_provider_entry("flaky") is not None


async def test_providers_add_rejects_reserved_names(sandbox):
    with pytest.raises(RpcError):
        await feature.providers_add(
            _gw(provider="openrouter"), {"name": "openai", "baseUrl": "http://127.0.0.1:1/v1"}
        )


async def test_providers_remove_deselects_the_active_one(sandbox):
    from machina.core.config import DEFAULTS
    from machina.security import providers as store

    store.add_provider("acme", "http://127.0.0.1:9000/v1")
    gw = _gw(provider="acme", model="acme-model")
    out = await feature.providers_remove(gw, {"name": "acme"})
    assert out["removed"] is True
    assert out["switchedTo"] == "openrouter"
    assert out["selected"] == "openrouter"
    assert out["modelResetTo"] == DEFAULTS["model"]
    assert gw.provider == "openrouter"
    assert gw.model == DEFAULTS["model"]
    with pytest.raises(RpcError):
        await feature.providers_remove(gw, {"name": "acme"})
    # Built-ins can never be removed through this path.
    with pytest.raises(RpcError):
        await feature.providers_remove(gw, {"name": "chatgpt"})


async def test_providers_remove_cleans_every_trace_and_reports(sandbox):
    from machina.core.provider import PROVIDERS
    from machina.security import providers as store

    store.add_provider("acme", "https://api.example.com/v1", api_key="sk-1")
    store.save_models("acme", [{"id": "m1"}, {"id": "m2"}])
    assert "acme" in PROVIDERS
    gw = _gw(provider="openrouter", model="openrouter/free")

    out = await feature.providers_remove(gw, {"name": "acme"})

    assert out["switchedTo"] is None
    assert out["modelResetTo"] is None
    assert out["cleanup"] == {
        "storeEntry": True,
        "apiKeyEnv": "MACHINA_PROVIDER_ACME_API_KEY",
        "envLineRemoved": True,
        "envVarRemoved": True,
        "cachedModels": 2,
        "registryEntry": True,
    }
    # Store entry + cached models, .env line, live env var, registry row: all gone.
    assert store.get_provider_entry("acme") is None
    assert "acme" not in PROVIDERS
    assert "MACHINA_PROVIDER_ACME_API_KEY" not in os.environ
    env_text = Path(os.environ["MACHINA_ENV_FILE"]).read_text(encoding="utf-8")
    assert "MACHINA_PROVIDER_ACME_API_KEY" not in env_text
    assert "sk-1" not in env_text
    # Idempotent and honest: nothing left to remove.
    with pytest.raises(RpcError):
        await feature.providers_remove(gw, {"name": "acme"})


async def test_providers_remove_reports_missing_traces_honestly(sandbox):
    from machina.security import providers as store

    store.add_provider("acme", "http://127.0.0.1:9000/v1")  # keyless, no .env line
    out = await feature.providers_remove(_gw(provider="openrouter"), {"name": "acme"})
    assert out["cleanup"]["envLineRemoved"] is False
    assert out["cleanup"]["envVarRemoved"] is False
    assert out["cleanup"]["cachedModels"] == 0


# ─── dynamic /keys entries: derived provider env names through secrets.* ────


class _FakeGW:
    def require_db(self) -> None:
        return None


async def test_secrets_accept_provider_env_keys(sandbox, monkeypatch):
    """keys.ts merges MACHINA_PROVIDER_<NAME>_API_KEY rows; the gateway must
    accept those derived names on set/clear while control-plane names stay
    refused."""
    from machina.gateway.features.secrets import secrets_clear, secrets_set
    from machina.security.env_file import read_env_values

    monkeypatch.delenv("MACHINA_PROVIDER_ACME_API_KEY", raising=False)
    out = await secrets_set(
        _FakeGW(),  # type: ignore[arg-type]
        {"key": "machina_provider_acme_api_key", "value": "sk-acme", "persist": True},
    )
    assert out["key"] == "MACHINA_PROVIDER_ACME_API_KEY"
    assert os.environ["MACHINA_PROVIDER_ACME_API_KEY"] == "sk-acme"
    assert read_env_values().get("MACHINA_PROVIDER_ACME_API_KEY") == "sk-acme"

    cleared = await secrets_clear(
        _FakeGW(),
        {"key": "MACHINA_PROVIDER_ACME_API_KEY"},  # type: ignore[arg-type]
    )
    assert cleared == {"key": "MACHINA_PROVIDER_ACME_API_KEY", "set": False}
    assert "MACHINA_PROVIDER_ACME_API_KEY" not in os.environ
    assert read_env_values().get("MACHINA_PROVIDER_ACME_API_KEY", "") == ""

    for key in ("MACHINA_GATEWAY_TOKEN", "MACHINA_PROVIDER_ACME", "MACHINA_PROVIDER_API_KEY"):
        with pytest.raises(RpcError):
            await secrets_set(_FakeGW(), {"key": key, "value": "x"})  # type: ignore[arg-type]


# ─── dynamic registry through the UNTOUCHED gateway validation sites ────────


async def test_gateway_set_provider_accepts_named_entries(sandbox):
    """``Gateway._set_provider`` validates with ``value not in PROVIDERS`` and
    must accept a user-added provider without server.py changes."""
    from machina.core.provider import PROVIDERS
    from machina.gateway.server import Gateway
    from machina.security import providers as store

    store.add_provider("acme", "http://127.0.0.1:9000/v1")
    gw = Gateway(lambda frame: None)
    assert "acme" in PROVIDERS
    gw._set_provider("acme")
    assert gw.provider == "acme"
    with pytest.raises(RpcError):
        gw._set_provider("ghost")


async def test_session_set_model_accepts_named_provider(sandbox, monkeypatch):
    """``session.setModel`` validates ``provider not in PROVIDERS`` the same way."""
    import uuid
    from unittest.mock import AsyncMock

    from machina.gateway.features import sessions as feature
    from machina.security import providers as store

    store.add_provider("acme", "http://127.0.0.1:9000/v1")
    session = SimpleNamespace(id=uuid.uuid4(), agent_id="harness", model=None, provider="acme")
    monkeypatch.setattr(feature.session_manager, "set_model", AsyncMock())
    monkeypatch.setattr(feature.session_manager, "get", AsyncMock(return_value=session))
    gw = SimpleNamespace(require_db=lambda: None, get_session=AsyncMock(return_value=session))
    result = await feature.session_set_model(gw, {"sessionId": str(session.id), "provider": "acme"})
    assert result["provider"] == "acme"


# ─── round-3 review findings 10/11: resumed-session route acceptance ────────


async def test_resumed_session_route_drives_models_and_effort(sandbox, monkeypatch):
    """The in-process twin of docs/verification/proofs/codebase_review_round3_
    2026-10-14/session-config.py: after session.resume the model commands
    follow the resumed session's pinned ChatGPT route (round-3 F10), and the
    advertised ``off`` alias clears instead of failing (round-3 F11)."""
    import uuid
    from unittest.mock import AsyncMock

    from machina.core.config import config
    from machina.core.models import Session
    from machina.core.provider import catalog
    from machina.gateway.server import Gateway

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    models = catalog.parse_catalog(payload, source="live")
    monkeypatch.setattr(catalog, "fetch_catalog_for_account", lambda **kwargs: models)
    session = Session(
        id=uuid.uuid4(), title="resumed account chat", model="gpt-6.1-sol", provider="chatgpt"
    )
    monkeypatch.setattr(
        "machina.gateway.server.session_manager.get", AsyncMock(return_value=session)
    )
    monkeypatch.setattr(
        "machina.gateway.server.context_manager.get_transcript_chunks",
        AsyncMock(return_value=[]),
    )

    gw = Gateway(lambda frame: None, owns_db=False, inherit_env_token=False)
    gw._db_ready = True
    gw.model = "openrouter/free"
    gw.provider = "openrouter"

    resumed = await gw._session_resume({"sessionId": str(session.id)})
    assert resumed["session"]["provider"] == "chatgpt"

    # Listing follows the resumed route, not the gateway default.
    listing = await gw._methods["models.list"]({})
    assert listing["provider"] == "chatgpt"
    assert "gpt-6.1-sol" in [row["id"] for row in listing["models"]]

    # Effort validates against the resumed model (max is ChatGPT-only here).
    accepted = await gw._methods["models.setEffort"]({"effort": "max"})
    assert accepted["provider"] == "chatgpt"
    assert accepted["model"] == "gpt-6.1-sol"
    assert "max" in accepted["supported"]

    cleared = await gw._methods["models.setEffort"]({"effort": "off"})
    assert cleared["effort"] is None
    assert config.get("reasoning_effort") == ""
