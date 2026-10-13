"""FM7 regression tests: CLI secret guarding, uninstall checkout guard,
Rich-markup safety on user text, friendly growth errors, and honest serve
readiness wording.

Env-file note (documented, not fixed here): ``read_env_values`` strips the
surrounding quotes but does not undo ``_quote``'s backslash/quote escapes, so a
value containing ``\\`` or ``"`` round-trips corrupted. The fix belongs in
``machina/security/env_file.py`` (outside this change's owned files); the
strict-xfail test below pins the correct behavior so the gap cannot be
forgotten.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

runner = CliRunner()

MODULE = "machina.cli"


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / ".env"
    monkeypatch.setenv("MACHINA_ENV_FILE", str(target))
    return target


# ─── W2: `machina config-set` never handles secrets ─────────────────────────


def test_config_set_rejects_secret_keys_without_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    import machina.cli as cli

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(cli.config, "set", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = runner.invoke(
        cli.app, ["config-set", "openrouter_api_key", "sk-SENTINEL-never-echoed"]
    )
    assert result.exit_code == 1, result.output
    assert "sk-SENTINEL-never-echoed" not in result.output, "secret value must never be echoed"
    assert calls == [], "the secret must not reach config.set"
    assert "/keys" in result.output and "setup" in result.output, "points at the safe paths"


def test_config_set_escapes_user_values_for_rich(monkeypatch: pytest.MonkeyPatch) -> None:
    import machina.cli as cli

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        cli.config,
        "set",
        lambda key, value, persist=False: calls.append((key, value, persist)),
    )

    result = runner.invoke(cli.app, ["config-set", "theme", "codex [not-a-tag"])
    assert result.exit_code == 0, result.output
    assert "codex [not-a-tag" in result.output, "user value is rendered literally"
    assert calls and calls[0][0] == "theme"


# ─── W3: uninstall never deletes a non-checkout ─────────────────────────────


def _isolate_uninstall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import machina.cli as cli
    import machina.security.env_file as env_file_mod

    app_root = tmp_path / "site-packages-shadow"
    (app_root / ".venv").mkdir(parents=True)
    fake_env = tmp_path / ".env"
    fake_env.write_text("DATABASE_URL=postgres://x\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cli, "_uninstall_app_root", lambda: app_root)
    monkeypatch.setattr(env_file_mod, "find_env_file", lambda: fake_env)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cli, "_docker_container_exists", lambda: False)
    monkeypatch.setattr(cli, "_brew_postgres_present", lambda: False)
    monkeypatch.setattr(
        cli, "_remove_windows_user_path_entries", lambda targets: (False, "skipped")
    )
    return app_root


def test_uninstall_refuses_checkout_deletion_without_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import machina.cli as cli

    app_root = _isolate_uninstall(tmp_path, monkeypatch)
    # PATH=yes, venv=yes, env=no, config=no, then a stray "y" (the checkout
    # question must not be asked at all for a non-checkout target).
    result = runner.invoke(cli.app, ["uninstall"], input="y\ny\nn\nn\ny\n")
    assert result.exit_code == 0, result.output
    # Rich soft-wraps long paths; compare on whitespace-normalized output.
    flat = " ".join(result.output.split())
    assert "not a source checkout" in flat, "refusal is explained"
    assert "Delete the whole checkout" not in flat, "no misleading checkout prompt"
    assert app_root.is_dir(), "a non-checkout target is never recursively deleted"
    assert not (app_root / ".venv").exists(), "other confirmed items still run"


def test_uninstall_keeps_checkout_prompt_for_real_checkouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import machina.cli as cli

    app_root = _isolate_uninstall(tmp_path, monkeypatch)
    (app_root / ".git").mkdir()
    result = runner.invoke(cli.app, ["uninstall"], input="n\nn\nn\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert "Delete the whole checkout" in result.output, "real checkouts keep the honest label"
    assert "Nothing selected" in result.output


# ─── W6: Rich-markup safety on untrusted text ───────────────────────────────


def _stub_db(monkeypatch: pytest.MonkeyPatch) -> None:
    import machina.cli as cli

    monkeypatch.setattr(cli.db, "connect", AsyncMock())
    monkeypatch.setattr(cli.db, "close", AsyncMock())


def test_sessions_search_query_markup_renders_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    import machina.cli as cli

    _stub_db(monkeypatch)
    monkeypatch.setattr(cli.session_manager, "search", AsyncMock(return_value=[]))
    result = runner.invoke(cli.app, ["sessions-search", "weird [red]query"])
    assert result.exit_code == 0, result.output
    assert "weird [red]query" in result.output


def test_memory_search_query_markup_renders_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    import machina.cli as cli

    _stub_db(monkeypatch)
    monkeypatch.setattr(
        "machina.memory.retriever.MemoryRetriever.retrieve", AsyncMock(return_value=[])
    )
    result = runner.invoke(cli.app, ["memory-search", "weird [red]query"])
    assert result.exit_code == 0, result.output
    assert "weird [red]query" in result.output


# ─── Shutdown drain: consolidation must finish before the pool closes ────────


def test_chat_drains_background_tasks_before_db_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI chat finally-block drains consolidation/review tasks before
    db.close(); the shutdown race died with asyncpg InterfaceError."""
    import machina.cli as cli

    calls: list[str] = []

    class _FakeAgent:
        provider = SimpleNamespace(model="stub-model")

        async def run_stream(self, session_id, message, verbose=False):
            return
            yield  # pragma: no cover — makes this an async generator

    fake_session = SimpleNamespace(id=uuid.uuid4(), provider="deepseek")

    monkeypatch.setattr(cli.db, "connect", AsyncMock())
    monkeypatch.setattr(cli.db, "initialize_runtime_schema", AsyncMock())

    async def fake_close():
        calls.append("db.close")

    monkeypatch.setattr(cli.db, "close", fake_close)
    monkeypatch.setattr(cli.session_manager, "create", AsyncMock(return_value=fake_session))
    monkeypatch.setattr(
        "machina.core.agent_factory.build_agent_for_session",
        AsyncMock(return_value=_FakeAgent()),
    )

    async def fake_provider_close(agent, timeout: float = 5.0) -> None:
        calls.append("provider.close")

    monkeypatch.setattr("machina.core.agent_factory.close_agent_provider", fake_provider_close)

    async def fake_drain(timeout: float = 5.0) -> int:
        calls.append("drain")
        return 0

    monkeypatch.setattr(cli, "drain_background_tasks", fake_drain)

    result = runner.invoke(cli.app, ["chat", "hello"])

    assert result.exit_code == 0, result.output
    # Drain first: consolidation needs the live provider AND the live pool.
    assert calls == ["drain", "provider.close", "db.close"], (
        "drain must complete before the provider and the pool close"
    )


def test_delete_session_title_markup_renders_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    import machina.cli as cli

    _stub_db(monkeypatch)
    session = SimpleNamespace(title="[bold]evil[/bold] title")
    monkeypatch.setattr(cli.session_manager, "get", AsyncMock(return_value=session))
    result = runner.invoke(cli.app, ["delete", str(uuid.uuid4())], input="n\n")
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "[bold]evil[/bold] title" in flat, "title is rendered literally"
    assert "Cancelled." in result.output


def test_hub_search_query_markup_renders_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    import machina.cli as cli
    from machina.skills.registry import SkillHub, skill_registry

    monkeypatch.setattr(skill_registry, "load_all", lambda: None)
    monkeypatch.setattr(SkillHub, "search", lambda self, query: [])
    result = runner.invoke(cli.app, ["hub", "search", "-q", "weird [red]query"])
    assert result.exit_code == 0, result.output
    assert "weird [red]query" in result.output


def test_user_profile_output_markup_renders_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    import machina.cli as cli
    import machina.memory.user_profile as user_profile_mod

    _stub_db(monkeypatch)
    profile = SimpleNamespace(
        id=uuid.uuid4(),
        user_id="tester",
        display_name="[red]name[/red]",
        interaction_count=1,
        created_at=datetime.now(UTC),
        preferences={"[cyan]pref": "[green]value"},
        topics={"[red]topic": 2},
        get_top_topics=lambda: [("[red]topic", 2)],
        last_topics=["[red]recent"],
    )
    monkeypatch.setattr(
        user_profile_mod.user_profile_store, "get_or_create", AsyncMock(return_value=profile)
    )
    result = runner.invoke(cli.app, ["user-profile", "tester"])
    assert result.exit_code == 0, result.output
    assert "[red]name[/red]" in result.output
    assert "[cyan]pref" in result.output


# ─── W6: friendly growth feedback error ─────────────────────────────────────


def test_growth_feedback_rejects_bad_uuid_without_traceback() -> None:
    from machina.cli.growth import app as growth_app

    result = runner.invoke(growth_app, ["feedback", "not-a-uuid", "0.5"])
    assert result.exit_code == 1, result.output
    assert "Invalid episode id" in result.output, "friendly error instead of a traceback"


# ─── W6: serve readiness wording ────────────────────────────────────────────


def test_serve_does_not_claim_ready_before_uvicorn_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    import machina.cli as cli

    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    monkeypatch.setattr("machina.api.app.create_app", lambda: object())
    result = runner.invoke(cli.app, ["serve"])
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:8000" in result.output
    assert "Server ready" not in result.output, "no readiness claim before the server binds"
    assert "Starting" in result.output


# ─── W6: env-file escape roundtrip (documented gap, strict xfail) ───────────


@pytest.mark.xfail(
    strict=True,
    reason=(
        "read_env_values strips quotes but never unescapes _quote's backslash escapes; "
        "fix belongs in machina/security/env_file.py (not owned by this change)"
    ),
)
def test_env_roundtrip_escaped_values_documents_gap(env_file: Path) -> None:
    from machina.security.env_file import read_env_values, set_env_values

    value = 'he said "hi" and C:\\tmp'
    set_env_values({"TEST_MENU_KEY": value}, env_file)
    assert read_env_values(env_file)["TEST_MENU_KEY"] == value


def test_env_roundtrip_plain_values_still_works(env_file: Path) -> None:
    from machina.security.env_file import read_env_values, set_env_values

    value = "hello world #not-a-comment"
    set_env_values({"TEST_MENU_KEY": value}, env_file)
    assert read_env_values(env_file)["TEST_MENU_KEY"] == value
