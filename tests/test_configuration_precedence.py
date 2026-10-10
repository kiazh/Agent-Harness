"""Live precedence and atomic secret management, including failure paths."""

import os

import pytest

from ah.core.config import Config
from ah.gateway.errors import RpcError
from tests.test_setup_secrets import FakeGW


def test_explicit_environment_wins_legacy_and_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("model: from-file\nsearxng_url: https://file.example\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_HARNESS_MODEL", "explicit-model")
    monkeypatch.setenv("AGENT_HARNESS_SEARXNG_URL", "https://explicit.example")
    monkeypatch.setenv("SEARXNG_URL", "https://legacy.example")
    config = Config.load(path)
    assert config.get("searxng_url") == "https://explicit.example"
    assert config.get("model") == "explicit-model"
    config._session_overrides["model"] = "session-model"
    assert config.get("model") == "session-model"


def test_environment_rotation_is_live(monkeypatch):
    config = Config(model="file-model")
    monkeypatch.setenv("AGENT_HARNESS_MODEL", "rotated")
    assert config.get("model") == "rotated"
    monkeypatch.delenv("AGENT_HARNESS_MODEL")
    assert config.get("model") == "file-model"


def test_secrets_are_not_loaded_from_yaml(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_HARNESS_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("openrouter_api_key: forbidden-yaml-secret\n", encoding="utf-8")
    config = Config.load(path)
    assert config.get("openrouter_api_key") != "forbidden-yaml-secret"
    assert config.openrouter_api_key != "forbidden-yaml-secret"


def test_explicit_secret_wins_legacy_and_rotates(monkeypatch):
    config = Config(openrouter_api_key="stale-memory-secret")
    monkeypatch.setenv("AGENT_HARNESS_OPENROUTER_API_KEY", "explicit-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "legacy-secret")
    assert config.get("openrouter_api_key") == "explicit-secret"
    monkeypatch.setenv("AGENT_HARNESS_OPENROUTER_API_KEY", "rotated-secret")
    assert config.get("openrouter_api_key") == "rotated-secret"


def test_invalid_live_environment_fails_without_mutation(monkeypatch):
    config = Config(turn_timeout=300)
    monkeypatch.setenv("AGENT_HARNESS_TURN_TIMEOUT", "not-a-number")
    with pytest.raises(ValueError):
        config.get("turn_timeout")
    assert config.turn_timeout == 300


@pytest.mark.parametrize("persist", [False, True])
async def test_secret_newline_rejected_before_live_mutation(tmp_path, monkeypatch, persist):
    from ah.gateway.features.secrets import secrets_set

    monkeypatch.setenv("AH_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("OPENAI_API_KEY", "previous-value")
    with pytest.raises(RpcError):
        await secrets_set(
            FakeGW(), {"key": "OPENAI_API_KEY", "value": "new\nINJECTED=secret", "persist": persist}
        )
    assert os.environ["OPENAI_API_KEY"] == "previous-value"
    assert not (tmp_path / ".env").exists()


async def test_persist_failure_does_not_activate_secret(monkeypatch):
    from ah.gateway.features.secrets import secrets_set

    monkeypatch.setenv("OPENAI_API_KEY", "previous-value")

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("ah.security.env_file.set_env_values", fail)
    with pytest.raises(RpcError):
        await secrets_set(FakeGW(), {"key": "OPENAI_API_KEY", "value": "next-value"})
    assert os.environ["OPENAI_API_KEY"] == "previous-value"


async def test_clear_failure_does_not_claim_durable_removal(tmp_path, monkeypatch):
    from ah.gateway.features.secrets import secrets_clear

    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=previous-value\n", encoding="utf-8")
    monkeypatch.setenv("AH_ENV_FILE", str(path))
    monkeypatch.setenv("OPENAI_API_KEY", "previous-value")

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("ah.security.env_file.set_env_values", fail)
    with pytest.raises(RpcError):
        await secrets_clear(FakeGW(), {"key": "OPENAI_API_KEY"})
    assert os.environ["OPENAI_API_KEY"] == "previous-value"
