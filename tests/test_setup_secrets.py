"""Tests for first-launch setup (.env helpers, secrets gateway, ah setup)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / ".env"
    monkeypatch.setenv("AH_ENV_FILE", str(target))
    for key in ("OPENROUTER_API_KEY", "TEST_MENU_KEY"):
        monkeypatch.delenv(key, raising=False)
    return target


class FakeGW:
    def require_db(self) -> None:
        return None


# ─── env_file helpers ────────────────────────────────────────────────────


def test_env_roundtrip_preserves_comments(env_file: Path) -> None:
    from ah.security.env_file import read_env_values, set_env_values

    env_file.write_text("# comment\nDATABASE_URL=postgres://a\n\n# keys\n", encoding="utf-8")
    set_env_values({"OPENROUTER_API_KEY": "sk-or-123", "DATABASE_URL": "postgres://b"}, env_file)
    text = env_file.read_text(encoding="utf-8")
    assert "# comment" in text and "# keys" in text
    values = read_env_values(env_file)
    assert values == {"DATABASE_URL": "postgres://b", "OPENROUTER_API_KEY": "sk-or-123"}


def test_env_quotes_values_needing_it(env_file: Path) -> None:
    from ah.security.env_file import read_env_values, set_env_values

    set_env_values({"TEST_MENU_KEY": "hello world #x"}, env_file)
    values = read_env_values(env_file)
    assert values["TEST_MENU_KEY"] == "hello world #x"


def test_env_rejects_bad_keys(env_file: Path) -> None:
    from ah.security.env_file import set_env_values

    with pytest.raises(ValueError):
        set_env_values({"bad key!": "x"}, env_file)
    with pytest.raises(ValueError):
        set_env_values({"": "x"}, env_file)


def test_find_env_file_prefers_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ah.security.env_file import find_env_file

    target = tmp_path / "custom.env"
    monkeypatch.setenv("AH_ENV_FILE", str(target))
    assert find_env_file() == target


# ─── secrets gateway ─────────────────────────────────────────────────────


async def test_secrets_list_never_returns_values(env_file: Path) -> None:
    from ah.gateway.features.secrets import secrets_list

    os.environ["OPENROUTER_API_KEY"] = "sk-or-live"
    result = await secrets_list(FakeGW(), {})  # type: ignore[arg-type]
    keys = {item["key"]: item for item in result["secrets"]}
    assert keys["OPENROUTER_API_KEY"]["set"] is True
    assert keys["ANTHROPIC_API_KEY"]["set"] is False
    assert all("value" not in item for item in result["secrets"])


async def test_secrets_set_persists_and_applies(env_file: Path) -> None:
    from ah.gateway.features.secrets import secrets_set

    result = await secrets_set(  # type: ignore[arg-type]
        FakeGW(), {"key": "openrouter_api_key", "value": "sk-or-abc", "persist": True}
    )
    assert result == {
        "key": "OPENROUTER_API_KEY",
        "set": True,
        "persisted": True,
        "envFile": str(env_file),
    }
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-abc"
    assert "sk-or-abc" in env_file.read_text(encoding="utf-8")


async def test_secrets_set_session_only_skips_file(env_file: Path) -> None:
    from ah.gateway.features.secrets import secrets_set

    result = await secrets_set(  # type: ignore[arg-type]
        FakeGW(), {"key": "GROQ_API_KEY", "value": "gsk-x", "persist": False}
    )
    assert result["persisted"] is False and result["envFile"] is None
    assert os.environ["GROQ_API_KEY"] == "gsk-x"
    assert not env_file.is_file()


async def test_secrets_set_rejects_bad_input() -> None:
    from ah.gateway.errors import RpcError
    from ah.gateway.features.secrets import secrets_set

    with pytest.raises(RpcError):
        await secrets_set(FakeGW(), {"key": "not a key!", "value": "x"})  # type: ignore[arg-type]
    with pytest.raises(RpcError):
        await secrets_set(FakeGW(), {"key": "OPENAI_API_KEY", "value": "   "})  # type: ignore[arg-type]
    with pytest.raises(RpcError):
        await secrets_set(FakeGW(), {"key": "OPENAI_API_KEY"})  # type: ignore[arg-type]


async def test_secrets_clear_blanks_env_and_file(env_file: Path) -> None:
    from ah.gateway.features.secrets import secrets_clear, secrets_set

    await secrets_set(  # type: ignore[arg-type]
        FakeGW(), {"key": "COHERE_API_KEY", "value": "co-123", "persist": True}
    )
    result = await secrets_clear(FakeGW(), {"key": "COHERE_API_KEY"})  # type: ignore[arg-type]
    assert result == {"key": "COHERE_API_KEY", "set": False}
    assert "COHERE_API_KEY" not in os.environ
    from ah.security.env_file import read_env_values

    assert read_env_values(env_file).get("COHERE_API_KEY", "") == ""


def test_secrets_methods_registered_on_gateway() -> None:
    from ah.gateway.features import METHODS

    assert METHODS["secrets.list"].__name__ == "secrets_list"
    assert METHODS["secrets.set"].__name__ == "secrets_set"
    assert METHODS["secrets.clear"].__name__ == "secrets_clear"


# ─── ah setup ────────────────────────────────────────────────────────────


def test_setup_non_interactive_creates_blank_env(env_file: Path) -> None:
    from ah.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--non-interactive"])
    assert result.exit_code == 0, result.output
    assert env_file.is_file()
    text = env_file.read_text(encoding="utf-8")
    assert "DATABASE_URL=" in text
    assert "sk-or-" not in text  # placeholders only, no real values echoed
