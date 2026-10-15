"""R4-082 gateway/providers cluster — E2E contract proofs.

* effort vocabulary: one canonical definition drives config validation and
  provider normalization; ``models.setEffort`` never persists a value that
  ``config.set`` rejects (``minimal`` used to escape as INTERNAL_ERROR).
* ``providers.remove`` fallback is durable like the CLI's: a restart cannot
  resurrect the removed provider from config.yaml or a shadowing env line.
* the removal cleanup payload name is ``apiKeyEnv`` on both sides of the wire
  (server payload == ui/src/protocol.ts == settings.ts consumer).
"""

from __future__ import annotations

import json
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
        # No client-opened session: the gateway defaults stand in.
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
    # The developer environment must not shadow the assertion surface.
    for name in ("MACHINA_PROVIDER", "MACHINA_MODEL", "MACHINA_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    from machina.core.provider import catalog

    catalog.clear_catalog_cache()
    yield tmp_path
    catalog.clear_catalog_cache()


def _seed_live_catalog(monkeypatch, *, minimal: bool = False) -> None:
    from machina.core.provider import catalog

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if minimal:
        levels = payload["models"][0]["supported_reasoning_levels"]
        levels.insert(0, {"effort": "minimal", "description": "wire tier probe"})
    models = catalog.parse_catalog(payload, source="live")
    monkeypatch.setattr(catalog, "fetch_live_catalog", lambda *args, **kwargs: models)
    catalog.refresh_live_catalog("token-1", "acct-1", force=True)


# ─── effort vocabulary: one canonical definition, agreeing surfaces ─────────


def test_canonical_effort_vocabulary_is_single_sourced():
    from machina.core.config import (
        REASONING_CATALOG_EFFORTS,
        REASONING_EFFORTS,
        REASONING_UNSET_EFFORTS,
    )
    from machina.core.provider import reasoning

    # provider.reasoning derives its exported tuple from core.config.
    assert reasoning.REASONING_EFFORTS is REASONING_EFFORTS
    assert set(REASONING_EFFORTS) | set(REASONING_CATALOG_EFFORTS) == {
        "low",
        "medium",
        "high",
        "minimal",
        "xhigh",
        "max",
    }
    assert set(REASONING_UNSET_EFFORTS) == {"", "none", "off", "default", "auto"}


@pytest.mark.parametrize(
    "value,expected",
    [
        ("low", "low"),
        (" Medium ", "medium"),
        ("high", "high"),
        ("minimal", "minimal"),
        ("xhigh", "xhigh"),
        ("MAX", "max"),
        ("", ""),
        ("off", ""),
        ("none", ""),
        ("default", ""),
        ("auto", ""),
    ],
)
def test_config_effort_surface_accepts_tiers_and_normalizes_aliases(value, expected):
    from machina.core.config import validate_value

    assert validate_value("reasoning_effort", value) == expected


def test_config_effort_surface_still_rejects_garbage():
    from machina.core.config import validate_value

    with pytest.raises(ValueError):
        validate_value("reasoning_effort", "ultra")


async def test_set_effort_minimal_persists_through_both_surfaces(sandbox, monkeypatch):
    """minimal is a wire tier the catalog can expose; config must store it."""
    _seed_live_catalog(monkeypatch, minimal=True)
    from machina.core.config import config

    out = await feature.models_set_effort(_gw(), {"effort": "minimal"})
    assert out["effort"] == "minimal"
    assert "minimal" in out["supported"]
    assert config.get("reasoning_effort") == "minimal"


async def test_effort_set_over_the_wire_has_no_internal_error(sandbox, monkeypatch):
    """The full JSON-RPC frame path: minimal returns a result, not INTERNAL_ERROR."""
    _seed_live_catalog(monkeypatch, minimal=True)
    from machina.core.config import config
    from machina.gateway.server import Gateway

    frames: list[dict] = []
    gw = Gateway(frames.append, owns_db=False, inherit_env_token=False)
    gw._db_ready = True
    gw.provider, gw.model = "chatgpt", "gpt-6.1-sol"
    try:
        await gw.handle_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "models.setEffort",
                    "params": {"effort": "minimal"},
                }
            )
        )
        frame = frames[-1]
        assert frame.get("error") is None, frame
        assert frame["result"]["effort"] == "minimal"
        assert config.get("reasoning_effort") == "minimal"
        # off clears on both surfaces.
        await gw.handle_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "models.setEffort",
                    "params": {"effort": "off"},
                }
            )
        )
        assert frames[-1].get("error") is None, frames[-1]
        assert frames[-1]["result"]["effort"] is None
        assert config.get("reasoning_effort") == ""
    finally:
        await gw.close()


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
async def test_every_persisted_catalog_effort_survives_config_validation(
    sandbox, monkeypatch, effort
):
    _seed_live_catalog(monkeypatch)
    from machina.core.config import config

    out = await feature.models_set_effort(_gw(model="gpt-6.1-sol"), {"effort": effort})
    assert out["effort"] == effort
    assert config.get("reasoning_effort") == effort


async def test_non_chatgpt_effort_policy_stays_typed(sandbox):
    """Non-catalog routes keep the wire-wide vocabulary; rejections are typed."""
    from machina.core.config import config

    gw = _gw(provider="openai", model="gpt-4o")
    out = await feature.models_set_effort(gw, {"effort": "high"})
    assert out["supported"] == ["low", "medium", "high"]
    assert config.get("reasoning_effort") == "high"
    for value in ("xhigh", "max", "minimal", "ultra"):
        with pytest.raises(RpcError) as excinfo:
            await feature.models_set_effort(gw, {"effort": value})
        assert "supported: low, medium, high" in str(excinfo.value), value


# ─── models.setEffort vs environment precedence (H23) ────────────────────────


async def test_set_effort_takes_effect_despite_an_env_shadow(sandbox, monkeypatch):
    """H23: ``/effort`` must not echo success while MACHINA_REASONING_EFFORT wins."""
    from machina.core.config import config

    monkeypatch.setenv("MACHINA_REASONING_EFFORT", "high")
    out = await feature.models_set_effort(_gw(provider="openai", model="gpt-4o"), {"effort": "low"})
    assert out["effort"] == "low"
    assert config.get("reasoning_effort") == "low"


async def test_set_effort_off_takes_effect_despite_an_env_shadow(sandbox, monkeypatch):
    """Clearing to provider default must also beat an explicit env value."""
    from machina.core.config import config

    monkeypatch.setenv("MACHINA_REASONING_EFFORT", "high")
    out = await feature.models_set_effort(_gw(provider="openai", model="gpt-4o"), {"effort": "off"})
    assert out["effort"] is None
    assert config.get("reasoning_effort") == ""


async def test_set_effort_persist_syncs_the_env_file(sandbox, monkeypatch):
    """A persisted effort survives a restart: .env carries the same value."""
    import importlib

    from machina.security.env_file import read_env_values

    config_module = importlib.import_module("machina.core.config")
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", sandbox / "config.yaml")
    monkeypatch.setenv("MACHINA_REASONING_EFFORT", "high")

    out = await feature.models_set_effort(
        _gw(provider="openai", model="gpt-4o"), {"effort": "medium", "persist": True}
    )
    assert out["effort"] == "medium"
    assert config_module.config.get("reasoning_effort") == "medium"
    assert read_env_values()["MACHINA_REASONING_EFFORT"] == "medium"


# ─── providers.remove: the fallback survives a restart ──────────────────────


async def test_providers_remove_persists_fallback_across_restart(sandbox, monkeypatch):
    import importlib

    from machina.security import providers as store
    from machina.security.env_file import read_env_values, set_env_values

    config_module = importlib.import_module("machina.core.config")
    config = config_module.config
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", sandbox / "config.yaml")
    monkeypatch.setattr(config, "provider", "acme")
    monkeypatch.setattr(config, "model", "acme-model")
    monkeypatch.setenv("MACHINA_PROVIDER", "acme")
    monkeypatch.setenv("MACHINA_MODEL", "acme-model")
    store.add_provider("acme", "https://api.example.com/v1", api_key="sk-1")
    set_env_values({"MACHINA_PROVIDER": "acme", "MACHINA_MODEL": "acme-model"})

    gw = _gw(provider="acme", model="acme-model")
    out = await feature.providers_remove(gw, {"name": "acme"})
    assert out["switchedTo"] == "openrouter"
    assert gw.provider == "openrouter" and gw.model == config_module.DEFAULTS["model"]

    # Durable config.yaml carries the fallback.
    saved = config_module.yaml.safe_load((sandbox / "config.yaml").read_text(encoding="utf-8"))
    assert saved["provider"] == "openrouter"
    assert saved["model"] == config_module.DEFAULTS["model"]
    # Shadowing .env lines were updated in place (Config.get checks env first).
    assert read_env_values()["MACHINA_PROVIDER"] == "openrouter"
    assert read_env_values()["MACHINA_MODEL"] == config_module.DEFAULTS["model"]

    # Restart simulation: process env gone, fresh load from disk only.
    monkeypatch.delenv("MACHINA_PROVIDER")
    monkeypatch.delenv("MACHINA_MODEL")
    fresh = config_module.Config.load(str(sandbox / "config.yaml"))
    assert fresh.get("provider") == "openrouter"
    assert fresh.get("model") == config_module.DEFAULTS["model"]


async def test_providers_remove_without_switch_persists_nothing(sandbox, monkeypatch):
    from machina.security import providers as store

    store.add_provider("acme", "http://127.0.0.1:9000/v1")
    config_module = __import__("machina.core.config", fromlist=["x"])
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", sandbox / "config.yaml")
    out = await feature.providers_remove(_gw(provider="openrouter"), {"name": "acme"})
    assert out["switchedTo"] is None
    assert not (sandbox / "config.yaml").exists()


# ─── cleanup payload name: apiKeyEnv (single name across the boundary) ──────


async def test_remove_cleanup_names_apikeyenv_on_the_wire(sandbox):
    from machina.security import providers as store

    store.add_provider("acme", "http://127.0.0.1:9000/v1", api_key="sk-1")
    out = await feature.providers_remove(_gw(provider="openrouter"), {"name": "acme"})
    # ui/src/protocol.ts ProviderCleanup mirrors exactly this key set.
    assert set(out["cleanup"]) == {
        "storeEntry",
        "apiKeyEnv",
        "envLineRemoved",
        "envVarRemoved",
        "cachedModels",
        "registryEntry",
    }
    assert out["cleanup"]["apiKeyEnv"] == "MACHINA_PROVIDER_ACME_API_KEY"
    assert "envKey" not in out["cleanup"]
