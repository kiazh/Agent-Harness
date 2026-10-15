"""R5-P symlink-containment proofs for the head-to-head runner (H-GRADE style).

Finding 20 (conditional, REPRODUCED): run copies were made with
``shutil.copytree(..., symlinks=True)``, so a fixture symlink escaping to
the pristine original (or anywhere outside the fixture) survived the copy.
The editable-source allowlist follows the link and excludes that path from
the integrity hash, so a harness edit through the run-copy link
contaminated the original while the run's before/after hashes stayed
equal. RED proof (unfixed module, manual)::

    task/src/link.py -> /original/victim.txt   (absolute escaping link)
    copytree(symlinks=True) -> run copy keeps the link
    editable_paths() marks src/link.py editable -> excluded from hashes
    harness writes run/src/link.py -> original victim.txt is overwritten
    hash_tree before == hash_tree after (attack missed)

The fix (``assert_no_escaping_symlinks`` at both the fixture gate and the
run-copy gate in ``run_one``) rejects such fixtures fail-closed before any
harness runs. These tests drive the REAL :func:`head_to_head.run_one` with
a stub ``opencode`` binary as the agent, exactly like
``tests/test_head_to_head_grader_adversarial.py`.

Attack cases (must raise ``RuntimeError``, original untouched):

* absolute file link escaping to the pristine original;
* directory link escaping to an outside directory;
* absolute link pointing *inside* the pristine fixture (passes the fixture
  gate, caught at the run-copy gate since the copy still aliases the
  original; the run copy is discarded).

Control: a fixture using an internal *relative* link still runs, and a
genuine fix applied through that link scores ``success=true`` while the
pristine original stays untouched.
"""

from __future__ import annotations

import os
import sys
from argparse import Namespace
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Plain ``import head_to_head`` mirroring the R4 adversarial module.
sys.path.append(str(_REPO_ROOT / "machina" / "research"))
import head_to_head  # noqa: E402

_FIXTURE_CALC = "def add(a: int, b: int) -> int:\n    return a - b\n"

_FIXED_CALC = "def add(a: int, b: int) -> int:\n    return a + b\n"

_TEST_MODULE = """\
from calc import add


def test_add() -> None:
    assert add(2, 3) == 5
"""

_ROOT_CONFTEST = "# Root conftest: puts the project root on sys.path.\n"

_FAKE_AGENT_HEADER = """\
import json
from pathlib import Path

root = Path.cwd()
"""

_FAKE_AGENT_FOOTER = """\
print(json.dumps({"sessionID": "ses-symlink-proof"}))
"""


def _fake_agent(tmp_path: Path, monkeypatch, mutations: str) -> None:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    stub = bin_dir / "opencode"
    stub.write_text(
        f"#!{sys.executable}\n{_FAKE_AGENT_HEADER}\n{mutations}\n{_FAKE_AGENT_FOOTER}",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _make_task(task_dir: Path) -> str:
    task_dir.mkdir(parents=True)
    (task_dir / "calc.py").write_text(_FIXTURE_CALC, encoding="utf-8")
    (task_dir / "conftest.py").write_text(_ROOT_CONFTEST, encoding="utf-8")
    (task_dir / "tests").mkdir()
    (task_dir / "tests" / "test_calc.py").write_text(_TEST_MODULE, encoding="utf-8")
    return "tests/test_calc.py"


def _args(tmp_path: Path, test_path: str) -> Namespace:
    return Namespace(
        runs_dir=str(tmp_path / "runs"),
        test_path=test_path,
        prompt="Make every test in the fixture pass without touching the tests.",
        model="fake/model",
        opencode_agent="build",
        timeout=60.0,
        opencode_db=str(tmp_path / "absent-opencode.db"),
    )


# ---------------------------------------------------------------------------
# Attacks: escaping links are rejected before any harness runs
# ---------------------------------------------------------------------------


def test_escaping_file_symlink_is_rejected_and_original_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    task_dir = tmp_path / "task"
    test_path = _make_task(task_dir)
    victim = tmp_path / "original" / "victim.txt"
    victim.parent.mkdir(parents=True)
    victim.write_text("ORIGINAL\n", encoding="utf-8")
    (task_dir / "evil_link.py").symlink_to(victim)
    # The agent playbook a real harness could run: edit through the link.
    _fake_agent(
        tmp_path,
        monkeypatch,
        '(root / "evil_link.py").write_text("HARNESS-PWNED\\n")\n',
    )
    with pytest.raises(RuntimeError, match="escaping the fixture tree"):
        head_to_head.run_one("opencode", _args(tmp_path, test_path), task_dir, Path(sys.executable))
    assert victim.read_text(encoding="utf-8") == "ORIGINAL\n"
    assert not (tmp_path / "runs").exists()


def test_escaping_directory_symlink_is_rejected(tmp_path: Path, monkeypatch) -> None:
    task_dir = tmp_path / "task"
    test_path = _make_task(task_dir)
    outside = tmp_path / "original" / "shared"
    outside.mkdir(parents=True)
    (outside / "data.txt").write_text("ORIGINAL\n", encoding="utf-8")
    (task_dir / "shared").symlink_to(outside, target_is_directory=True)
    _fake_agent(
        tmp_path,
        monkeypatch,
        '(root / "shared" / "evil.txt").write_text("HARNESS-PWNED\\n")\n',
    )
    with pytest.raises(RuntimeError, match="escaping the fixture tree"):
        head_to_head.run_one("opencode", _args(tmp_path, test_path), task_dir, Path(sys.executable))
    assert (outside / "data.txt").read_text(encoding="utf-8") == "ORIGINAL\n"
    assert not (outside / "evil.txt").exists()


def test_absolute_link_into_pristine_fixture_is_rejected_after_copy(
    tmp_path: Path, monkeypatch
) -> None:
    task_dir = tmp_path / "task"
    test_path = _make_task(task_dir)
    # Absolute yet fixture-internal: passes the fixture gate (resolves
    # inside the task dir) but must fail the run-copy gate, where the same
    # bytes alias the pristine original instead of the copy.
    (task_dir / "abs_link.py").symlink_to(task_dir / "calc.py")
    assert head_to_head.escaping_symlinks(task_dir) == []
    _fake_agent(
        tmp_path,
        monkeypatch,
        f'(root / "abs_link.py").write_text({_FIXED_CALC!r})\n',
    )
    with pytest.raises(RuntimeError, match="run copy contains symlink"):
        head_to_head.run_one("opencode", _args(tmp_path, test_path), task_dir, Path(sys.executable))
    # Pristine fixture untouched; the tainted run copy is discarded.
    assert (task_dir / "calc.py").read_text(encoding="utf-8") == _FIXTURE_CALC
    assert not (tmp_path / "runs" / "task" / "opencode").exists()


# ---------------------------------------------------------------------------
# Control: internal relative links keep working, genuine fix still scores
# ---------------------------------------------------------------------------


def test_internal_relative_symlink_fixture_still_scores(tmp_path: Path, monkeypatch) -> None:
    task_dir = tmp_path / "task"
    test_path = _make_task(task_dir)
    (task_dir / "alias.py").symlink_to("calc.py")
    assert head_to_head.escaping_symlinks(task_dir) == []
    # Genuine fix applied *through* the internal link: contained in the
    # run copy, so the pristine original must stay buggy while grading the
    # fixed copy scores success.
    _fake_agent(
        tmp_path,
        monkeypatch,
        f'(root / "alias.py").write_text({_FIXED_CALC!r})\n',
    )
    result = head_to_head.run_one(
        "opencode", _args(tmp_path, test_path), task_dir, Path(sys.executable)
    )
    assert result["success"] is True, result
    assert result["tests_passed"] == 1, result
    assert result["tests_failed"] == 0, result
    assert (task_dir / "calc.py").read_text(encoding="utf-8") == _FIXTURE_CALC
