"""learn_directory tool (V1): bounded deterministic workspace digest, read-only.

RED-first: at HEAD ``machina/tools/repomap.py`` does not exist, so every test
here fails until the tool module lands. The tool is step one of "open a
directory in Machina, learn it, then debug it": deterministic, no LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from machina.core.config import config

TINY_FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": "import os\nfrom pkg import b\n\nif __name__ == '__main__':\n    print('hi')\n",
    "pkg/b.py": "X = 1\n",
    "tests/test_a.py": "def test_x():\n    assert True\n",
    "notes.txt": "hello\n",
}


def _write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _cache_label(digest: str) -> str:
    for line in digest.splitlines():
        if line.startswith("scan: "):
            return line.split("cache:", 1)[1].strip()
    raise AssertionError("digest has no scan header")


def _without_scan_line(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith("scan: "))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "project"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setattr(config, "workspace_root", str(root))
    monkeypatch.setattr(config, "machina_home", str(home))
    return root


@pytest.fixture
def tiny_tree(workspace):
    _write_files(workspace, TINY_FILES)
    # Excluded by repomap.scan defaults: must not appear in the digest.
    (workspace / "__pycache__").mkdir()
    (workspace / "__pycache__" / "junk.py").write_text("BROKEN(\n", encoding="utf-8")
    return workspace


async def test_digest_contains_expected_modules_totals_and_layout(tiny_tree):
    from machina.tools.repomap import learn_directory

    out = await learn_directory(".")
    assert "pkg/a.py" in out and "pkg/b.py" in out
    assert "files=5" in out  # notes.txt counted, __pycache__ excluded
    assert "modules=4" in out
    assert "loc=8" in out  # pkg/a.py=5, test_a=2, b=1, __init__=0
    assert "junk.py" not in out
    assert "entry points (1): pkg/a.py" in out
    assert "tests (1): tests/test_a.py" in out
    assert "largest modules (4 total):" in out
    assert "packages: pkg=3" in out and "(root)=1" in out
    assert "import hubs (fan-in): pkg.__init__=1" in out
    assert "scan: fresh" in out
    assert "cache: " in out


async def test_repeat_call_reports_cached_with_zero_parses(tiny_tree):
    from machina.tools.repomap import learn_directory

    first = await learn_directory(".")
    assert "scan: fresh" in first
    cache_path = _cache_label(first)
    second = await learn_directory(".")
    assert "scan: cached (0 files parsed)" in second
    # The content-hash counters persisted under the machina home.
    entries = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    assert entries
    assert any(key.endswith("a.py") for key in entries)


async def test_registry_call_reports_cached_after_repeat(tiny_tree):
    from machina.tools.base import registry

    first = await registry.execute("learn_directory", path=".")
    second = await registry.execute("learn_directory", path=".")
    assert "scan: fresh" in first
    assert "scan: cached (0 files parsed)" in second


async def test_path_escape_returns_error_string_not_exception(tiny_tree, tmp_path):
    from machina.tools.repomap import learn_directory

    escaped = await learn_directory("../..")
    assert escaped.startswith("Error:")
    assert "escapes" in escaped
    outside = await learn_directory(str(tmp_path))
    assert outside.startswith("Error:")


async def test_two_builds_identical_except_cache_status(tiny_tree):
    from machina.tools.repomap import learn_directory

    first = await learn_directory(".")
    second = await learn_directory(".")
    assert _without_scan_line(first) == _without_scan_line(second)
    assert "scan: fresh" in first
    assert "scan: cached (0 files parsed)" in second


async def test_registration_and_read_only_effects(tiny_tree):
    import machina.tools  # noqa: F401 — registers builtins
    from machina.tools.base import registry

    tool = registry.get_tool("learn_directory")
    assert tool is not None
    assert tool.effects == registry.get_tool("read_file").effects == ("fs.read",)
    assert "path" in tool.parameters["properties"]
    assert "learn_directory" in {d.name for d in registry.get_tool_definitions()}


async def test_digest_bounded_on_large_tree(workspace):
    from machina.tools.repomap import learn_directory

    level = "d" * 40
    prefix = "/".join([level] * 12)
    for i in range(130):
        target = workspace / prefix / f"module_{i:03d}_{'x' * 40}.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("X = 1\n", encoding="utf-8")
    out = await learn_directory(".")
    assert len(out) <= 4000
    assert "totals:" in out and "files=130" in out
    assert out.endswith("[digest truncated]")


async def test_unwritable_machina_home_falls_back_to_memory(workspace, tmp_path, monkeypatch):
    from machina.tools.repomap import learn_directory

    _write_files(workspace, TINY_FILES)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.setattr(config, "machina_home", str(blocker / "home"))
    out = await learn_directory(".")
    assert out.startswith("learn_directory:")
    assert "cache: in-memory" in out
    assert "files=5" in out
