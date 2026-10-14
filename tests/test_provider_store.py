"""User-added named providers: store hygiene, key idiom, factory resolution.

The store is a 0600 atomic JSON file holding metadata only; API keys go
through the same ``.env`` mechanism ``/keys`` uses (explicit env-file paths
keep these tests off the developer's real files).
"""

from __future__ import annotations

import json
import os
import stat

import httpx
import pytest

from machina.core.exceptions import ValidationError
from machina.core.provider import PROVIDERS, DirectProvider, get_provider
from machina.security import providers as store


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Isolated store + env file paths for every test."""
    providers_file = tmp_path / "parts" / "providers.json"
    env_file = tmp_path / "home" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MACHINA_PROVIDERS_FILE", str(providers_file))
    monkeypatch.setenv("MACHINA_ENV_FILE", str(env_file))
    yield {"providers": providers_file, "env": env_file, "tmp": tmp_path}
    # Drop any env key the test registered on the live process.
    for name in store.provider_names():
        entry = store.get_provider_entry(name) or {}
        env_name = str(entry.get("api_key_env") or "")
        if env_name:
            os.environ.pop(env_name, None)


# ─── validation ─────────────────────────────────────────────────────────────


def test_names_and_urls_are_validated(store_env):
    with pytest.raises(store.ProviderStoreError):
        store.add_provider("Bad Name", "http://127.0.0.1:1/v1")
    with pytest.raises(store.ProviderStoreError):
        store.add_provider("1bad", "http://127.0.0.1:1/v1")
    with pytest.raises(store.ProviderStoreError):
        store.add_provider("-bad", "http://127.0.0.1:1/v1")
    with pytest.raises(store.ProviderStoreError):
        store.add_provider("openai", "http://127.0.0.1:1/v1")  # reserved built-in
    with pytest.raises(store.ProviderStoreError):
        store.add_provider("ok-name", "ftp://host/v1")
    with pytest.raises(store.ProviderStoreError):
        store.add_provider("ok-name", "http://user:pass@host/v1")
    with pytest.raises(store.ProviderStoreError):
        store.add_provider("ok-name", "http://host/v1?token=leak")


def test_reserved_names_cover_every_builtin():
    from machina.core.provider.specs import PROVIDER_SPECS

    assert store.RESERVED_NAMES == (
        set(PROVIDER_SPECS) | {"openrouter", "anthropic", "chatgpt", "custom", "ollama"}
    )


# ─── store hygiene: atomic 0600, metadata only ──────────────────────────────


def test_store_is_0600_and_never_contains_the_key(store_env):
    entry = store.add_provider(
        "local-vllm",
        "http://127.0.0.1:8000/v1",
        api_key="sk-secret-value",
        env_file=store_env["env"],
    )
    mode = stat.S_IMODE(os.stat(store_env["providers"]).st_mode)
    assert mode == 0o600
    raw = store_env["providers"].read_text(encoding="utf-8")
    assert "sk-secret-value" not in raw
    payload = json.loads(raw)
    stored = payload["providers"]["local-vllm"]
    assert stored["base_url"] == "http://127.0.0.1:8000/v1"
    assert stored["api_key_env"] == "MACHINA_PROVIDER_LOCAL_VLLM_API_KEY"
    assert entry["models"] == []
    # The key landed in the env file through the shared idiom (0600 too).
    env_text = store_env["env"].read_text(encoding="utf-8")
    assert "MACHINA_PROVIDER_LOCAL_VLLM_API_KEY=sk-secret-value" in env_text
    assert store.resolve_provider_key(entry) == "sk-secret-value"
    assert os.environ["MACHINA_PROVIDER_LOCAL_VLLM_API_KEY"] == "sk-secret-value"
    assert stat.S_IMODE(os.stat(store_env["env"]).st_mode) == 0o600


def test_update_preserves_models_and_created_at(store_env):
    first = store.add_provider("acme", "http://127.0.0.1:9000/v1")
    store.save_models("acme", [{"id": "m1"}])
    second = store.add_provider("acme", "http://127.0.0.1:9001/v1")
    assert second["created_at"] == first["created_at"]
    assert [row["id"] for row in second["models"]] == ["m1"]
    assert store.get_provider_entry("acme")["base_url"].endswith(":9001/v1")


def test_remove_clears_the_key_entry(store_env):
    store.add_provider(
        "acme", "http://127.0.0.1:9000/v1", api_key="sk-x", env_file=store_env["env"]
    )
    assert store.remove_provider("acme") is True
    assert store.remove_provider("acme") is False
    assert store.provider_names() == ()
    assert "MACHINA_PROVIDER_ACME_API_KEY" not in os.environ
    env_text = store_env["env"].read_text(encoding="utf-8")
    assert "MACHINA_PROVIDER_ACME_API_KEY=" in env_text  # blanked, not deleted
    assert "sk-x" not in env_text


def test_corrupt_store_reads_as_empty(store_env):
    store_env["providers"].parent.mkdir(parents=True, exist_ok=True)
    store_env["providers"].write_text("{not json", encoding="utf-8")
    assert store.load_store()["providers"] == {}
    assert store.provider_names() == ()


# ─── model cache + fetch ────────────────────────────────────────────────────


def test_save_models_normalizes_and_pins_default(store_env):
    store.add_provider("acme", "http://127.0.0.1:9000/v1")
    rows = store.save_models(
        "acme",
        [
            {"id": "big", "context_length": 131072},
            {"id": "small", "max_model_len": 8192},
            {"id": "big"},  # duplicate id dropped
            {"id": ""},  # empty id dropped
            {"id": "weird", "context_length": -5},  # invalid window dropped
        ],
    )
    assert [row["id"] for row in rows] == ["big", "small", "weird"]
    assert rows[0]["contextWindow"] == 131_072
    assert rows[1]["contextWindow"] == 8192
    assert "contextWindow" not in rows[2]
    entry = store.get_provider_entry("acme")
    assert entry["default_model"] == "big"
    assert store.pulled_at_age_seconds(entry) is not None


def test_save_models_rejects_unknown_provider(store_env):
    with pytest.raises(store.ProviderStoreError):
        store.save_models("nope", [{"id": "m"}])


def test_fetch_models_parses_openai_shape_and_sends_bearer():
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={"data": [{"id": "m1", "context_length": 32768}, {"id": "m2"}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = store.fetch_models("http://127.0.0.1:9000/v1", api_key="sk-k", client=client)
    assert seen == {"auth": "Bearer sk-k", "path": "/v1/models"}
    assert [row["id"] for row in rows] == ["m1", "m2"]
    assert rows[0]["contextWindow"] == 32_768


def test_fetch_models_keyless_sends_no_auth_and_is_honest_on_errors():
    def ok(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"models": ["alpha", "beta"]})

    with httpx.Client(transport=httpx.MockTransport(ok)) as client:
        rows = store.fetch_models("http://127.0.0.1:9000/v1", client=client)
    assert [row["id"] for row in rows] == ["alpha", "beta"]

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    with httpx.Client(transport=httpx.MockTransport(failing)) as client:
        with pytest.raises(store.ProviderStoreError) as excinfo:
            store.fetch_models("http://127.0.0.1:9000/v1", api_key="sk-hidden", client=client)
    assert "HTTP 404" in str(excinfo.value)
    assert "sk-hidden" not in str(excinfo.value)


# ─── dynamic registry + factory resolution ──────────────────────────────────


def test_providers_registry_membership_is_dynamic(store_env):
    assert "acme" not in PROVIDERS
    store.add_provider("acme", "http://127.0.0.1:9000/v1")
    assert "acme" in PROVIDERS
    assert "acme" in store.provider_names()
    store.remove_provider("acme")
    assert "acme" not in PROVIDERS


async def test_factory_resolves_named_provider_with_cached_default_model(store_env):
    store.add_provider("acme", "http://127.0.0.1:9000/v1")
    store.save_models("acme", [{"id": "first-model"}, {"id": "second-model"}])
    provider = get_provider("acme")
    try:
        assert isinstance(provider, DirectProvider)
        assert provider.spec.name == "acme"
        assert provider.spec.base_url == "http://127.0.0.1:9000/v1"
        assert provider.model == "first-model"
        assert provider.api_key == ""  # keyless: remote answers or refuses, honestly
    finally:
        await provider.close()

    explicit = get_provider("acme", model="second-model")
    try:
        assert explicit.model == "second-model"
    finally:
        await explicit.close()


async def test_factory_rejects_named_provider_without_models(store_env):
    store.add_provider("empty-one", "http://127.0.0.1:9000/v1")
    with pytest.raises(ValidationError) as excinfo:
        get_provider("empty-one")
    assert "no known model" in str(excinfo.value)


def test_unknown_provider_error_lists_named_entries(store_env):
    store.add_provider("acme", "http://127.0.0.1:9000/v1")
    with pytest.raises(ValidationError) as excinfo:
        get_provider("ghost")
    assert "acme" in str(excinfo.value)


# ─── runtime readiness (ready / no key / unreachable) ───────────────────────


def _select(monkeypatch, name: str) -> None:
    from machina.core.config import config

    monkeypatch.setattr(config, "_session_overrides", {"provider": name})


def test_named_provider_readiness_ready_with_key(store_env, monkeypatch):
    from machina.core import runtime

    store.add_provider("acme", "https://api.example.com/v1", api_key="sk-1")
    _select(monkeypatch, "acme")
    monkeypatch.setattr(runtime, "_endpoint_reachable", lambda url, timeout=1.5: True)
    report = runtime._detect_providers()
    assert report["chat_provider"] == "ready"
    assert report["chat_provider_selected"] == "acme"


def test_named_provider_readiness_no_key_for_remote(store_env, monkeypatch):
    from machina.core import runtime

    store.add_provider("acme", "https://api.example.com/v1")
    _select(monkeypatch, "acme")
    report = runtime._detect_providers()
    assert report["chat_provider"] == "unavailable"
    assert "no key set" in report["chat_provider_reason"]
    assert "MACHINA_PROVIDER_ACME_API_KEY" in report["chat_provider_reason"]


def test_named_provider_readiness_unreachable(store_env, monkeypatch):
    from machina.core import runtime

    store.add_provider("acme", "http://127.0.0.1:9000/v1")
    _select(monkeypatch, "acme")
    monkeypatch.setattr(runtime, "_endpoint_reachable", lambda url, timeout=1.5: False)
    report = runtime._detect_providers()
    assert report["chat_provider"] == "unavailable"
    assert "unreachable" in report["chat_provider_reason"]


def test_named_provider_readiness_keyless_local_is_ready(store_env, monkeypatch):
    from machina.core import runtime

    store.add_provider("acme", "http://127.0.0.1:9000/v1")
    _select(monkeypatch, "acme")
    monkeypatch.setattr(runtime, "_endpoint_reachable", lambda url, timeout=1.5: True)
    report = runtime._detect_providers()
    assert report["chat_provider"] == "ready"
    assert "no key (local endpoint)" in report["chat_provider_reason"]
