"""Final-review 2026-10-15 fixes — CLI config-set env shadow + setup --force.

F09 (P2): ``config-set model|provider --persist`` synced the ``.env``/env
shadow from ``config.get(key)``, which still prefers a pre-existing
``MACHINA_MODEL``/``MACHINA_PROVIDER`` over the just-saved attribute, so the
OLD value was written back. The shadow must carry the validated new value.

F10 (P2): ``setup --force`` printed that it overwrote ``.env`` but only cleared
its in-memory map, so ``set_env_values`` merged into the original file —
prior provider keys, model/provider choices and secrets survived. Forced
setup must produce a fresh, complete file (atomic, 0600).

These tests drive the REAL Typer commands and the REAL env-file/config
machinery; only the database and the interop step are stubbed. DB: none.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def isolated_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate config file, .env, provider store, and env shadows.

    Restores the config singleton's touched values afterwards so this file
    never leaks a model/provider change into other tests.
    """
    import machina.cli as cli
    import machina.core.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "DEFAULT_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setenv("MACHINA_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("MACHINA_PROVIDERS_FILE", str(tmp_path / "providers.json"))
    for var in ("MACHINA_PROVIDER", "MACHINA_MODEL", "MACHINA_EXECUTION_MODE"):
        monkeypatch.delenv(var, raising=False)
    previous = {k: cli.config.get(k) for k in ("provider", "model", "execution_mode")}
    yield cli
    for key, value in previous.items():
        try:
            cli.config.set(key, value)
        except ValueError:
            pass


def _stub_interop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`setup` always runs the best-effort interop step; keep it hermetic."""
    from machina import interop

    monkeypatch.setattr(interop, "detect", lambda platforms=None: {})
    monkeypatch.setattr(interop, "enable", lambda *a, **k: None)
    monkeypatch.setattr(interop, "enable_detected", lambda *a, **k: None)


# ─── F09: config-set must sync the NEW validated value, not the stale env ────


def test_config_set_model_persist_overrides_stale_env(
    isolated_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED (F09): with MACHINA_MODEL=old in the environment, `config-set model
    new --persist` saved YAML ``new`` then wrote ``old`` into .env and the live
    process environment, because the shadow read ``config.get`` (env wins)."""
    from machina.security.env_file import read_env_values

    cli = isolated_cli
    monkeypatch.setenv("MACHINA_MODEL", "old-model")
    assert cli.config.get("model") == "old-model", "precondition: env shadows the file"

    result = runner.invoke(cli.app, ["config-set", "model", "new-model", "--persist"])

    assert result.exit_code == 0, result.output
    assert os.environ.get("MACHINA_MODEL") == "new-model", "live env carries the new value"
    assert read_env_values()["MACHINA_MODEL"] == "new-model", ".env carries the new value"
    assert cli.config.get("model") == "new-model", "effective value is the new value"


def test_config_set_provider_persist_overrides_stale_env(
    isolated_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED (F09): the same stale-shadow bug for MACHINA_PROVIDER."""
    from machina.security.env_file import read_env_values

    cli = isolated_cli
    monkeypatch.setenv("MACHINA_PROVIDER", "old-provider")

    result = runner.invoke(cli.app, ["config-set", "provider", "groq", "--persist"])

    assert result.exit_code == 0, result.output
    assert os.environ.get("MACHINA_PROVIDER") == "groq"
    assert read_env_values()["MACHINA_PROVIDER"] == "groq"
    assert cli.config.get("provider") == "groq"


def test_config_set_model_persist_without_env_still_syncs(
    isolated_cli,
) -> None:
    """No-regression guard: the ordinary (no stale env) path keeps working."""
    from machina.security.env_file import read_env_values

    cli = isolated_cli
    result = runner.invoke(cli.app, ["config-set", "model", "openrouter/free", "--persist"])
    assert result.exit_code == 0, result.output
    assert os.environ.get("MACHINA_MODEL") == "openrouter/free"
    assert read_env_values()["MACHINA_MODEL"] == "openrouter/free"


# ─── F10: setup --force must REPLACE, ordinary setup still MERGES ────────────


def test_setup_force_noninteractive_replaces_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED (F10): forced non-interactive setup must drop every old key."""
    import machina.cli as cli
    from machina.security.env_file import read_env_values

    env_file = tmp_path / ".env"
    monkeypatch.setenv("MACHINA_ENV_FILE", str(env_file))
    _stub_interop(monkeypatch)
    env_file.write_text(
        "OPENROUTER_API_KEY=sk-or-old\n"
        "MACHINA_MODEL=old-model\n"
        "MACHINA_PROVIDER=old-provider\n"
        "DATABASE_URL=postgresql://u:p@db.internal:5432/prod\n",
        encoding="utf-8",
    )

    result = runner.invoke(cli.app, ["setup", "--force", "--non-interactive", "--interop", "none"])

    assert result.exit_code == 0, result.output
    values = read_env_values(env_file)
    assert "OPENROUTER_API_KEY" not in values, "old secret must not survive --force"
    assert "MACHINA_MODEL" not in values, "old model must not survive --force"
    assert "MACHINA_PROVIDER" not in values, "old provider must not survive --force"
    assert values["DATABASE_URL"] == "postgresql://postgres:postgres@localhost:5432/machina"
    assert oct(env_file.stat().st_mode & 0o777) == "0o600", "fresh file is private"


def test_setup_force_interactive_blank_answers_remove_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED (F10): blank interactive answers under --force must remove old
    secrets instead of silently keeping them (existing map was non-empty)."""
    import machina.cli as cli
    from machina.security.env_file import read_env_values

    env_file = tmp_path / ".env"
    monkeypatch.setenv("MACHINA_ENV_FILE", str(env_file))
    _stub_interop(monkeypatch)
    env_file.write_text("OPENROUTER_API_KEY=sk-or-old\nMACHINA_MODEL=old-model\n", encoding="utf-8")

    # Accept every prompt default / skip every hidden prompt.
    result = runner.invoke(cli.app, ["setup", "--force", "--interop", "none"], input="\n" * 25)

    assert result.exit_code == 0, result.output
    values = read_env_values(env_file)
    assert "OPENROUTER_API_KEY" not in values, "blank answer must remove the old secret"
    assert values.get("MACHINA_MODEL") != "old-model", "old model must not survive --force"
    assert values.get("MACHINA_PROVIDER") != "old-provider", "old provider must not survive --force"


def test_setup_without_force_still_merges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No-regression guard: ordinary setup keeps existing values (merge)."""
    import machina.cli as cli
    from machina.security.env_file import read_env_values

    env_file = tmp_path / ".env"
    monkeypatch.setenv("MACHINA_ENV_FILE", str(env_file))
    _stub_interop(monkeypatch)
    env_file.write_text("OPENROUTER_API_KEY=sk-or-keep\nCUSTOM_KEEP=keep-me\n", encoding="utf-8")

    result = runner.invoke(cli.app, ["setup", "--non-interactive", "--interop", "none"])

    assert result.exit_code == 0, result.output
    values = read_env_values(env_file)
    assert values["OPENROUTER_API_KEY"] == "sk-or-keep", "ordinary setup must not wipe secrets"
    assert values["CUSTOM_KEEP"] == "keep-me"
    assert values["DATABASE_URL"] == "postgresql://postgres:postgres@localhost:5432/machina"
