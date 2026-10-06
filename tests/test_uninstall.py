"""Tests for `ah uninstall` — PATH removal and file deletion."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

from typer.testing import CliRunner


def test_filter_path_entries_drops_matches_case_insensitively() -> None:
    from ah.cli import _filter_path_entries

    before = r"C:\Python;C:\app\.venv\Scripts;C:\other"
    after = _filter_path_entries(before, [r"c:\APP\.venv\scripts\\"])
    assert after == r"C:\Python;C:\other"


def test_filter_path_entries_keeps_unrelated() -> None:
    from ah.cli import _filter_path_entries

    before = r"C:\Python;C:\other"
    assert _filter_path_entries(before, [r"C:\app\.venv\Scripts"]) == before


def test_shim_points_at_matches_prefix_case_insensitively() -> None:
    from ah.cli import _shim_points_at

    assert _shim_points_at('exec "/APP/.venv/bin/ah" "$@"', "/app")
    assert not _shim_points_at('exec "/other/.venv/bin/ah" "$@"', "/app")


def test_uninstall_ask_path_only_keeps_files(tmp_path: Path, monkeypatch) -> None:
    """Yes to PATH, no to files: PATH helper runs, files stay."""
    import ah.cli as cli

    fake_root, fake_env, removed = _isolate_uninstall(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["uninstall"], input="y\nn\nn\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert "Uninstall plan" in result.output
    assert removed and str(fake_root) in removed[0][0]
    assert (fake_root / ".venv").is_dir()
    assert fake_env.is_file()


def test_uninstall_ask_yes_to_all_deletes_files(tmp_path: Path, monkeypatch) -> None:
    import ah.cli as cli
    import ah.security.env_file as env_file_mod

    fake_root = tmp_path / "checkout"
    (fake_root / ".venv").mkdir(parents=True)
    fake_env = tmp_path / ".env"
    fake_env.write_text("DATABASE_URL=postgres://x\n", encoding="utf-8")
    home = tmp_path / "home"
    shim = home / ".local" / "bin" / "ah"
    shim.parent.mkdir(parents=True)
    shim.write_text(f'exec "{fake_root}/.venv/bin/ah" "$@"\n', encoding="utf-8")
    local_config = home / ".agent-harness"
    local_config.mkdir()

    monkeypatch.setattr(cli, "_uninstall_app_root", lambda: fake_root)
    monkeypatch.setattr(env_file_mod, "find_env_file", lambda: fake_env)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cli, "_remove_windows_user_path_entries", lambda targets: (True, "ok"))
    monkeypatch.setattr(cli, "_docker_container_exists", lambda: False)
    monkeypatch.setattr(cli, "_brew_postgres_present", lambda: False)

    runner = CliRunner()
    # PATH, venv, env file, config, checkout — yes to all five.
    result = runner.invoke(cli.app, ["uninstall"], input="y\ny\ny\ny\ny\n")
    assert result.exit_code == 0, result.output
    assert not (fake_root / ".venv").exists()
    assert not fake_env.exists()
    assert not shim.exists()
    assert not local_config.exists()
    assert not fake_root.exists()


# ─── docker / brew / database helpers ───────────────────────────────────────


def test_docker_container_exists_parses_names(monkeypatch) -> None:
    import ah.cli as cli

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")

    def fake_run(*args, **kwargs):
        out = MagicMockStdout("other\nagentharness-db\n")
        return Completed(out, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli._docker_container_exists() is True

    def fake_run_absent(*args, **kwargs):
        return Completed(MagicMockStdout("other\n"), 0)

    monkeypatch.setattr(subprocess, "run", fake_run_absent)
    assert cli._docker_container_exists() is False


def test_docker_container_exists_without_docker(monkeypatch) -> None:
    import ah.cli as cli

    monkeypatch.setattr("shutil.which", lambda name: None)
    assert cli._docker_container_exists() is False


def test_remove_docker_container_stops_then_removes(monkeypatch) -> None:
    import ah.cli as cli

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Completed(MagicMockStdout(""), 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, detail = cli._remove_docker_container()
    assert ok is True
    assert calls == [["docker", "stop", "agentharness-db"], ["docker", "rm", "agentharness-db"]]
    assert "gone" in detail


def test_remove_docker_container_reports_failure(monkeypatch) -> None:
    import ah.cli as cli

    def fake_run(cmd, **kwargs):
        return Completed(MagicMockStdout("", err="boom"), 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, detail = cli._remove_docker_container()
    assert ok is False
    assert "boom" in detail


def test_split_database_dsn() -> None:
    from ah.cli import _split_database_dsn

    maint, name = _split_database_dsn("postgresql://u:p@localhost:5432/agentharness")
    assert name == "agentharness"
    assert maint.endswith("/postgres")
    assert _split_database_dsn("postgresql://u:p@localhost:5432/postgres") is None
    assert _split_database_dsn("postgresql://u:p@localhost:5432/template1") is None
    assert _split_database_dsn("postgresql://u:p@localhost:5432/") is None
    assert _split_database_dsn("postgresql://u:p@localhost:5432/a-b") is None
    assert _split_database_dsn("not a url at all") is None


def test_brew_postgres_absent_without_brew(monkeypatch) -> None:
    import ah.cli as cli

    monkeypatch.setattr("shutil.which", lambda name: None)
    assert cli._brew_postgres_present() is False


class MagicMockStdout:
    def __init__(self, stdout: str, err: str = "") -> None:
        self.stdout = stdout
        self.stderr = err


class Completed:
    def __init__(self, out: MagicMockStdout, returncode: int) -> None:
        self.stdout = out.stdout
        self.stderr = out.stderr
        self.returncode = returncode


# ─── interactive ask mode ───────────────────────────────────────────────────


def _isolate_uninstall(tmp_path: Path, monkeypatch, with_db_url: bool = False):
    import ah.cli as cli
    import ah.security.env_file as env_file_mod

    fake_root = tmp_path / "checkout"
    (fake_root / ".venv").mkdir(parents=True)
    fake_env = tmp_path / ".env"
    fake_env.write_text(
        "DATABASE_URL=postgresql://u:p@localhost:5432/agentharness\n"
        if with_db_url
        else "DATABASE_URL=postgres://x\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_uninstall_app_root", lambda: fake_root)
    monkeypatch.setattr(env_file_mod, "find_env_file", lambda: fake_env)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(cli, "_docker_container_exists", lambda: False)
    monkeypatch.setattr(cli, "_brew_postgres_present", lambda: False)
    removed: list[list[str]] = []
    monkeypatch.setattr(
        cli,
        "_remove_windows_user_path_entries",
        lambda targets: (removed.append(targets), (True, "ok"))[1],
    )
    return fake_root, fake_env, removed


def test_uninstall_ask_mode_yes_to_nothing(tmp_path: Path, monkeypatch) -> None:
    """Answering no to everything aborts without touching anything."""
    import ah.cli as cli

    fake_root, fake_env, removed = _isolate_uninstall(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["uninstall"], input="n\nn\nn\nn\nn\n")
    assert result.exit_code == 0, result.output
    assert "Nothing selected" in result.output
    assert removed == []
    assert (fake_root / ".venv").is_dir()
    assert fake_env.is_file()


def test_uninstall_full_yes_to_all_removes_everything(tmp_path: Path, monkeypatch) -> None:
    """Yes to all eight questions with docker/db/brew present removes all of it."""
    import ah.cli as cli
    import ah.security.env_file as env_file_mod

    fake_root = tmp_path / "checkout"
    (fake_root / ".venv").mkdir(parents=True)
    fake_env = tmp_path / ".env"
    fake_env.write_text(
        "DATABASE_URL=postgresql://u:p@localhost:5432/agentharness\n", encoding="utf-8"
    )
    home = tmp_path / "home"
    shim = home / ".local" / "bin" / "ah"
    shim.parent.mkdir(parents=True)
    shim.write_text(f'exec "{fake_root}/.venv/bin/ah" "$@"\n', encoding="utf-8")
    local_config = home / ".agent-harness"
    local_config.mkdir()

    monkeypatch.setattr(cli, "_uninstall_app_root", lambda: fake_root)
    monkeypatch.setattr(env_file_mod, "find_env_file", lambda: fake_env)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cli, "_remove_windows_user_path_entries", lambda targets: (True, "ok"))
    monkeypatch.setattr(cli, "_docker_container_exists", lambda: True)
    docker_removed: list[bool] = []
    monkeypatch.setattr(
        cli, "_remove_docker_container", lambda: (docker_removed.append(True), (True, "gone"))[1]
    )
    monkeypatch.setattr(cli, "_brew_postgres_present", lambda: True)
    brew_removed: list[bool] = []
    monkeypatch.setattr(
        cli, "_remove_brew_postgres", lambda: (brew_removed.append(True), (True, "gone"))[1]
    )
    drop_db = AsyncMock()
    monkeypatch.setattr(cli, "_drop_database", drop_db)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["uninstall"], input="y\n" * 8)
    assert result.exit_code == 0, result.output
    assert docker_removed == [True]
    assert brew_removed == [True]
    drop_db.assert_awaited_once()
    assert drop_db.await_args.args[1] == "agentharness"
    assert not shim.exists()
    assert not local_config.exists()
    assert not fake_env.exists()
    assert not (fake_root / ".venv").exists()
    assert not fake_root.exists()
