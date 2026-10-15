"""R4-093 CLI cluster — H-CLI proofs over the real Typer commands.

Every test drives the public CLI surface (``runner.invoke``) with real
config/env-file/provider-store machinery. The only stubs are at the
allowed boundaries: the database (async connect/close) and the network
(``fetch_models`` HTTP). DB: none needed — no test here touches Postgres.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def isolated_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate config file, .env, provider store, and env shadows.

    Restores the config singleton's touched values afterwards so the
    suite never leaks a provider/model/mode change into other tests.
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


def _stub_db(monkeypatch: pytest.MonkeyPatch) -> None:
    import machina.cli as cli

    monkeypatch.setattr(cli.db, "connect", AsyncMock())
    monkeypatch.setattr(cli.db, "close", AsyncMock())


# ─── config-set/model+provider sync .env+env like the gateway ────────────────


def test_config_set_provider_persist_syncs_env_and_dotenv(isolated_cli) -> None:
    cli = isolated_cli
    result = runner.invoke(cli.app, ["config-set", "provider", "openrouter", "--persist"])
    assert result.exit_code == 0, result.output
    assert os.environ.get("MACHINA_PROVIDER") == "openrouter"
    from machina.security.env_file import read_env_values

    assert read_env_values()["MACHINA_PROVIDER"] == "openrouter"
    assert (cli.config.get("provider")) == "openrouter"


def test_config_set_model_persist_syncs_env_and_dotenv(isolated_cli) -> None:
    cli = isolated_cli
    result = runner.invoke(cli.app, ["config-set", "model", "openrouter/free", "--persist"])
    assert result.exit_code == 0, result.output
    assert os.environ.get("MACHINA_MODEL") == "openrouter/free"
    from machina.security.env_file import read_env_values

    assert read_env_values()["MACHINA_MODEL"] == "openrouter/free"


def test_config_set_non_shadowed_key_writes_no_env_line(isolated_cli) -> None:
    """Only model/provider shadow config.yaml via MACHINA_* — theme must not sprout env lines."""
    cli = isolated_cli
    result = runner.invoke(cli.app, ["config-set", "theme", "codex", "--persist"])
    assert result.exit_code == 0, result.output
    assert "MACHINA_THEME" not in os.environ
    from machina.security.env_file import read_env_values

    assert "MACHINA_THEME" not in read_env_values()


def test_config_set_invalid_value_is_typed_error(isolated_cli) -> None:
    cli = isolated_cli
    result = runner.invoke(cli.app, ["config-set", "execution_mode", "bogus"])
    assert result.exit_code == 1, result.output
    assert "ask|workspace|sandbox|full" in result.output, "validation message, never a traceback"
    assert "Traceback" not in result.output


# ─── mode sync ───────────────────────────────────────────────────────────────


def test_mode_set_syncs_env_and_dotenv(isolated_cli) -> None:
    cli = isolated_cli
    result = runner.invoke(cli.app, ["mode", "workspace"])
    assert result.exit_code == 0, result.output
    assert cli.config.get("execution_mode") == "workspace"
    assert os.environ.get("MACHINA_EXECUTION_MODE") == "workspace"
    from machina.security.env_file import read_env_values

    assert read_env_values()["MACHINA_EXECUTION_MODE"] == "workspace"


def test_mode_invalid_is_typed_error(isolated_cli) -> None:
    cli = isolated_cli
    result = runner.invoke(cli.app, ["mode", "bogus"])
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output


# ─── repl --mode validation ──────────────────────────────────────────────────


def test_repl_rejects_invalid_mode_without_launching(isolated_cli) -> None:
    cli = isolated_cli
    result = runner.invoke(cli.app, ["repl", "--mode", "bogus"])
    assert result.exit_code == 1, result.output
    assert "Invalid --mode" in result.output
    assert "Traceback" not in result.output


# ─── setup env-file guard ────────────────────────────────────────────────────


def test_setup_bad_env_override_is_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import machina.cli as cli

    monkeypatch.setenv("MACHINA_ENV_FILE", "bad-override-without-dotenv-suffix")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["setup", "--non-interactive", "--interop", "none"])
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert ".env" in result.output


# ─── Rich escaping for interpolated output ───────────────────────────────────


def test_config_set_value_with_markup_renders_literally(isolated_cli) -> None:
    cli = isolated_cli
    result = runner.invoke(cli.app, ["config-set", "theme", "a [/b] b"])
    assert result.exit_code == 0, result.output
    assert "a [/b] b" in result.output, "stray closing tag renders literally, never raises"


def test_delete_confirm_renders_hostile_title_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import machina.cli as cli

    _stub_db(monkeypatch)
    session = SimpleNamespace(title="[/b] evil", id="00000000-0000-0000-0000-000000000000")
    monkeypatch.setattr(cli.session_manager, "get", AsyncMock(return_value=session))
    import uuid as uuid_mod

    result = runner.invoke(cli.app, ["delete", str(uuid_mod.uuid4())], input="n\n")
    assert result.exit_code == 0, result.output
    assert "[/b] evil" in " ".join(result.output.split())


def test_output_helpers_render_markup_literally() -> None:
    """Direct supplement: the shared helpers escape; a stray tag must survive."""
    from machina.cli import output

    output.success("ok [/b] done")
    output.error("bad [/b] boom", "hint [/i] here")
    output.muted("dim [/b] text")
    output.status_line("Label [/b]", "value [/red]")


def test_sessions_table_renders_hostile_title_literally(capsys) -> None:
    """Direct supplement: table cells interpret markup, so they must escape."""
    from machina.cli.output import sessions_table

    row = SimpleNamespace(
        id="abc123",
        title="[/b] evil",
        status="active",
        agent_id="harness",
        goal="[red]goal",
        last_activity=None,
    )
    sessions_table([row])
    out = capsys.readouterr().out
    assert "[/b] evil" in out
    assert "[red]goal" in out


# ─── traceback-free fork ─────────────────────────────────────────────────────


def test_fork_unknown_session_is_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store raises ValueError for a missing session (session.py); the
    CLI maps it to exit 1 with the message, never a traceback."""
    import uuid as uuid_mod

    import machina.cli as cli

    _stub_db(monkeypatch)

    async def _missing(session_id, title=None):
        raise ValueError(f"Session {session_id} not found")

    monkeypatch.setattr(cli.session_manager, "fork", _missing)
    result = runner.invoke(cli.app, ["fork", str(uuid_mod.uuid4())])
    assert result.exit_code == 1, result.output
    assert "not found" in result.output
    assert "Traceback" not in result.output


def test_fork_invalid_uuid_is_typed_error() -> None:
    import machina.cli as cli

    result = runner.invoke(cli.app, ["fork", "not-a-uuid"])
    assert result.exit_code == 1, result.output
    assert "Invalid session ID" in result.output
    assert "Traceback" not in result.output


# ─── traceback-free growth file errors ───────────────────────────────────────


def test_growth_import_policy_missing_file_is_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import machina.cli as cli
    from machina.cli.growth import app as growth_app

    _stub_db(monkeypatch)
    monkeypatch.setattr(cli.db, "initialize_runtime_schema", AsyncMock())
    import uuid as uuid_mod

    fake = SimpleNamespace(id=uuid_mod.uuid4(), agent_id="harness")
    monkeypatch.setattr(cli.session_manager, "get", AsyncMock(return_value=fake))
    result = runner.invoke(
        growth_app, ["import-policy", str(tmp_path / "nope.json"), "--session", str(fake.id)]
    )
    assert result.exit_code == 1, result.output
    assert "Error" in result.output, "typed file error, never a traceback"
    assert "Traceback" not in result.output


def test_growth_import_policy_bad_json_is_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import machina.cli as cli
    from machina.cli.growth import app as growth_app

    _stub_db(monkeypatch)
    monkeypatch.setattr(cli.db, "initialize_runtime_schema", AsyncMock())
    import uuid as uuid_mod

    fake = SimpleNamespace(id=uuid_mod.uuid4(), agent_id="harness")
    monkeypatch.setattr(cli.session_manager, "get", AsyncMock(return_value=fake))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = runner.invoke(growth_app, ["import-policy", str(bad), "--session", str(fake.id)])
    assert result.exit_code == 1, result.output
    assert "Error" in result.output, "typed JSON error, never a traceback"
    assert "Traceback" not in result.output


# ─── providers models --refresh keeps the cache on empty success ────────────


def test_models_refresh_empty_success_keeps_cache(
    isolated_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seed a cached catalog, then refresh against an endpoint whose live
    fetch succeeds but returns no models: exit stays 0, the cached list is
    shown with an honest warning, and the store keeps the cached rows."""
    from machina.security import providers as store

    cli = isolated_cli
    store.add_provider("cachekeep", "http://127.0.0.1:9/v1", api_key="k-secret")
    store.save_models("cachekeep", [{"id": "cached-model"}])
    monkeypatch.setattr(store, "fetch_models", lambda *a, **k: [])
    result = runner.invoke(cli.app, ["providers", "models", "cachekeep", "--refresh"])
    assert result.exit_code == 0, result.output
    assert "cached-model" in result.output
    assert "cached list" in result.output
    entry = store.get_provider_entry("cachekeep")
    assert entry is not None
    assert [row["id"] for row in entry.get("models") or []] == ["cached-model"]


def test_models_refresh_nonempty_success_replaces_cache(
    isolated_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-regression guard: a live fetch WITH models still replaces the cache."""
    from machina.security import providers as store

    cli = isolated_cli
    store.add_provider("cachereplace", "http://127.0.0.1:9/v1", api_key="k-secret")
    store.save_models("cachereplace", [{"id": "old-model"}])
    monkeypatch.setattr(store, "fetch_models", lambda *a, **k: [{"id": "new-model"}])
    result = runner.invoke(cli.app, ["providers", "models", "cachereplace", "--refresh"])
    assert result.exit_code == 0, result.output
    assert "new-model" in result.output
    entry = store.get_provider_entry("cachereplace")
    assert entry is not None
    assert [row["id"] for row in entry.get("models") or []] == ["new-model"]
