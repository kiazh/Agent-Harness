"""Tests for `ah uninstall` — PATH removal and file deletion."""

from __future__ import annotations

from pathlib import Path

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


def test_uninstall_plan_only_touches_nothing_by_default(tmp_path: Path, monkeypatch) -> None:
    """Default `uninstall --force` removes PATH registration but deletes no files."""
    import ah.cli as cli
    import ah.security.env_file as env_file_mod

    fake_root = tmp_path / "checkout"
    (fake_root / ".venv").mkdir(parents=True)
    fake_env = tmp_path / ".env"
    fake_env.write_text("DATABASE_URL=postgres://x\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_uninstall_app_root", lambda: fake_root)
    monkeypatch.setattr(env_file_mod, "find_env_file", lambda: fake_env)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    removed: list[list[str]] = []
    monkeypatch.setattr(
        cli,
        "_remove_windows_user_path_entries",
        lambda targets: (removed.append(targets), (True, "ok"))[1],
    )

    runner = CliRunner()
    result = runner.invoke(cli.app, ["uninstall", "--force"])
    assert result.exit_code == 0, result.output
    assert "Uninstall plan" in result.output
    assert removed and str(fake_root) in removed[0][0]
    # Nothing deleted by default.
    assert (fake_root / ".venv").is_dir()
    assert fake_env.is_file()


def test_uninstall_full_deletes_files_and_shim(tmp_path: Path, monkeypatch) -> None:
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

    runner = CliRunner()
    # Point the checkout delete at a copy so the assertion below is meaningful
    # without touching the real repo: --full minus checkout via individual flags.
    result = runner.invoke(
        cli.app,
        ["uninstall", "--remove-venv", "--remove-env-file", "--remove-config", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert not (fake_root / ".venv").exists()
    assert not fake_env.exists()
    assert not shim.exists()
    assert not local_config.exists()
