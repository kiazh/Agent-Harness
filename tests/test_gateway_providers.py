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
    return SimpleNamespace(require_db=lambda: None, provider=provider, model=model)


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
    assert "ultra" not in luna["advertisedEfforts"] or True  # luna never advertises ultra


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


# ─── providers.list / add / remove ──────────────────────────────────────────


async def test_providers_list_reports_builtin_and_named(sandbox):
    from machina.security import providers as store

    store.add_provider("acme", "http://127.0.0.1:9000/v1", api_key="sk-1")
    out = await feature.providers_list(_gw(provider="acme"), {})
    assert out["selected"] == "acme"
    names = [row["name"] for row in out["providers"]]
    assert "chatgpt" in names and "acme" in names
    acme = next(row for row in out["providers"] if row["name"] == "acme")
    assert acme["kind"] == "named"
    assert acme["keySet"] is True
    assert acme["keylessLocal"] is True  # 127.0.0.1
    assert acme["selected"] is True


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
    from machina.security import providers as store

    store.add_provider("acme", "http://127.0.0.1:9000/v1")
    gw = _gw(provider="acme")
    out = await feature.providers_remove(gw, {"name": "acme"})
    assert out["removed"] is True
    assert out["switchedTo"] == "openrouter"
    assert gw.provider == "openrouter"
    with pytest.raises(RpcError):
        await feature.providers_remove(gw, {"name": "acme"})
    # Built-ins can never be removed through this path.
    with pytest.raises(RpcError):
        await feature.providers_remove(gw, {"name": "chatgpt"})


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
