"""Adversarial filesystem tests for the ``.env`` writer, reader, and resolver.

Round-4 tasks covered here:

- R4-044: writer hardening — a symlink planted at the old predictable
  ``<name>.tmp.<pid>`` path must never be followed, a crash mid-write must
  never leave the target truncated/empty, and the write path must fsync the
  file before the replace and the directory after it.
- R4-046: case-insensitive key matching — a pre-existing lowercase line is
  updated in place or removed (never duplicated); the provider-removal
  surface drives the same behavior end to end.
- R4-048: an installed wheel layout must never resolve, allow, or create a
  ``.env`` inside ``site-packages``.

Every RED-documented test fails against the unfixed module for exactly the
named reason and passes after the fixes; non-RED guards are labeled inline.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from machina.security import env_file as env_file_mod


def _old_tmp_path(target: Path) -> Path:
    """The temp path the pre-R4-044 writer used (predictable from the pid)."""
    return target.parent / f"{target.name}.tmp.{os.getpid()}"


# ─── R4-044: planted symlink at the old predictable temp path ───────────────


def test_planted_symlink_at_old_tmp_path_is_never_followed(tmp_path: Path) -> None:
    """RED pre-fix: the writer followed the planted link and clobbered the victim."""
    target = tmp_path / ".env"
    target.write_text("ORIGINAL=1\n", encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("VICTIM-CONTENT\n", encoding="utf-8")
    planted = _old_tmp_path(target)
    planted.symlink_to(victim)

    env_file_mod.set_env_values({"NEW_KEY": "v"}, target)

    assert victim.read_text(encoding="utf-8") == "VICTIM-CONTENT\n", "symlink target clobbered"
    assert planted.is_symlink(), "planted symlink was consumed by the write"
    text = target.read_text(encoding="utf-8")
    assert "ORIGINAL=1" in text
    assert "NEW_KEY=v" in text


# ─── R4-044: crash injection mid-write must never empty the .env ────────────


def test_crash_mid_write_never_leaves_an_empty_env(tmp_path: Path, monkeypatch) -> None:
    """RED pre-fix: O_TRUNC on the planted link emptied the .env before the crash."""
    target = tmp_path / ".env"
    original = "SECRET_VALUE=keep-me\n"
    target.write_text(original, encoding="utf-8")
    planted = _old_tmp_path(target)
    planted.symlink_to(target)  # the unfixed writer truncates through this

    real_fdopen = os.fdopen

    class _ExplodingFile:
        def __enter__(self) -> _ExplodingFile:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def write(self, _data: str) -> None:
            raise OSError("injected crash mid-write")

    def exploding_fdopen(fd: int, *args: object, **kwargs: object) -> _ExplodingFile:
        real_fdopen(fd, *args, **kwargs).close()
        return _ExplodingFile()

    monkeypatch.setattr(env_file_mod.os, "fdopen", exploding_fdopen)
    with pytest.raises(OSError, match="injected crash"):
        env_file_mod.set_env_values({"NEW_KEY": "v"}, target)

    assert target.read_text(encoding="utf-8") == original, "crash left the .env truncated"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name not in {".env", planted.name}]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_crash_before_replace_keeps_old_env_and_cleans_temp(tmp_path: Path, monkeypatch) -> None:
    """Supplement (passes pre-fix): replace failure must keep the old file and clean up."""
    target = tmp_path / ".env"
    target.write_text("OLD=1\n", encoding="utf-8")

    def exploding_replace(_src: object, _dst: object) -> None:
        raise OSError("injected crash before replace")

    monkeypatch.setattr(env_file_mod.os, "replace", exploding_replace)
    with pytest.raises(OSError, match="injected crash"):
        env_file_mod.set_env_values({"NEW_KEY": "v"}, target)

    assert target.read_text(encoding="utf-8") == "OLD=1\n"
    assert [p.name for p in tmp_path.iterdir() if p.name != ".env"] == []


# ─── R4-044: durability — file fsync before replace, directory fsync after ──


def test_write_fsyncs_file_and_directory(tmp_path: Path, monkeypatch) -> None:
    """RED pre-fix: the writer never called fsync at all."""
    target = tmp_path / ".env"
    target.write_text("A_KEY=1\n", encoding="utf-8")

    calls: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(env_file_mod.os, "fsync", spy)
    env_file_mod.set_env_values({"B_KEY": "2"}, target)

    assert len(calls) >= 2, "expected a file fsync and a directory fsync"
    assert env_file_mod.read_env_values(target)["B_KEY"] == "2"
    leftovers = sorted(p.name for p in tmp_path.iterdir() if p.name != ".env")
    assert leftovers == [], f"temp files left behind: {leftovers}"


# ─── R4-046: lowercase pre-existing lines are visible to update/remove ──────


def test_lowercase_existing_line_is_updated_in_place(tmp_path: Path) -> None:
    """RED pre-fix: the lowercase line was neither matched nor normalized."""
    target = tmp_path / ".env"
    target.write_text("# keep\nopenrouter_api_key=stale\nOTHER=1\n", encoding="utf-8")

    env_file_mod.set_env_values({"OPENROUTER_API_KEY": "fresh"}, target)

    lines = target.read_text(encoding="utf-8").splitlines()
    assert "# keep" in lines
    assert lines.count("OPENROUTER_API_KEY=fresh") == 1
    assert all("openrouter_api_key" not in line for line in lines)
    assert env_file_mod.read_env_values(target)["OPENROUTER_API_KEY"] == "fresh"


def test_lowercase_existing_line_is_removed_cleanly(tmp_path: Path) -> None:
    """RED pre-fix: removal reported [] and kept the lowercase line."""
    target = tmp_path / ".env"
    target.write_text("openrouter_api_key=stale\nKEEP=1\n", encoding="utf-8")

    removed = env_file_mod.remove_env_values(["OPENROUTER_API_KEY"], path=target)

    assert removed == ["OPENROUTER_API_KEY"]
    text = target.read_text(encoding="utf-8")
    assert "openrouter_api_key" not in text
    assert "KEEP=1" in text


def test_read_normalizes_mixed_case_keys(tmp_path: Path) -> None:
    """RED pre-fix: lowercase keys were skipped entirely by the reader."""
    target = tmp_path / ".env"
    target.write_text("openrouter_api_key=stored\nOther_Key=2\n", encoding="utf-8")

    assert env_file_mod.read_env_values(target) == {
        "OPENROUTER_API_KEY": "stored",
        "OTHER_KEY": "2",
    }


def test_provider_removal_deletes_lowercase_env_line(tmp_path: Path, monkeypatch) -> None:
    """E2E (R4-046): the provider-removal surface finds and deletes the line."""
    from machina.security import providers as store

    monkeypatch.setenv("MACHINA_PROVIDERS_FILE", str(tmp_path / "providers.json"))
    env = tmp_path / ".env"
    monkeypatch.setenv("MACHINA_ENV_FILE", str(env))
    monkeypatch.delenv("MACHINA_PROVIDER_ACME_API_KEY", raising=False)
    store.add_provider("acme", "http://127.0.0.1:9000/v1", api_key="sk-x", env_file=env)
    env.write_text("machina_provider_acme_api_key=sk-legacy\nKEEP=1\n", encoding="utf-8")
    monkeypatch.delenv("MACHINA_PROVIDER_ACME_API_KEY", raising=False)

    report = store.remove_provider("acme")

    assert report is not None, "provider must be registered before removal"
    assert report["envLineRemoved"] is True
    text = env.read_text(encoding="utf-8")
    assert "machina_provider_acme_api_key" not in text
    assert "KEEP=1" in text


# ─── R4-048: installed wheel layout never targets site-packages ─────────────

_WHEEL_PROBE = """
import json
import os
import sys

import machina.security.env_file as env_file

data = {
    "module": os.path.realpath(env_file.__file__),
    "site_packages": os.path.realpath(sys.argv[1]),
    "roots": [os.path.realpath(str(p)) for p in env_file._allowed_roots()],
    "resolved": os.path.realpath(str(env_file.find_env_file())),
}
env_file.set_env_values({"R4_PROBE_KEY": "v"})
data["written"] = os.path.realpath(str(env_file.find_env_file()))
print(json.dumps(data))
"""


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def test_installed_wheel_layout_never_targets_site_packages(tmp_path: Path) -> None:
    """E2E (R4-048): RED pre-fix; the fallback resolved inside the fake site-packages."""
    site = tmp_path / "venv" / "lib" / "python3.14" / "site-packages"
    package = site / "machina"
    (package / "security").mkdir(parents=True)
    (package / "observability").mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "security" / "__init__.py").write_text("", encoding="utf-8")
    (package / "observability" / "__init__.py").write_text("", encoding="utf-8")
    (package / "observability" / "diagnostics.py").write_text(
        "def record_failure(*_args, **_kwargs):\n    return None\n",
        encoding="utf-8",
    )
    shutil.copyfile(Path(env_file_mod.__file__).resolve(), package / "security" / "env_file.py")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    env = dict(os.environ)
    env.pop("MACHINA_ENV_FILE", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(site), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    proc = subprocess.run(
        [sys.executable, "-c", _WHEEL_PROBE, str(site)],
        cwd=run_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    site_real = os.path.realpath(str(site))

    # The simulation is real: the loaded module is the fake site-packages copy.
    assert _inside(data["module"], site_real), data
    # No allowed root, resolved path, or written path may target site-packages.
    assert not any(_inside(root, site_real) for root in data["roots"]), data["roots"]
    assert not _inside(data["resolved"], site_real), data["resolved"]
    assert not _inside(data["written"], site_real), data["written"]
    assert not list(site.rglob(".env")), "a .env was created inside site-packages"
    assert not list(site.rglob("*.tmp")), "temp files leaked into site-packages"
    # The write fell back to the walk-up origin (the run directory).
    assert data["written"] == os.path.join(os.path.realpath(str(run_dir)), ".env")
    assert (run_dir / ".env").is_file()
