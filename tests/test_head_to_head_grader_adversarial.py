"""R4-020 adversarial proofs for the head-to-head grader (H-GRADE matrix).

These tests drive the REAL :func:`head_to_head.run_one` end to end against
tiny temp fixtures: real copytree, real before/after integrity hashes, the
real pytest subprocess grade in the repo venv, and the real success rule. The
only faked boundary is the external harness binary: a stub ``opencode`` on
PATH plays the agent (applies a fix or plants an attack), exactly like a real
agent editing the run copy.

Attack cases (must score ``success=false`` even though pytest exits 0 without
them):

* ``pytest.ini`` ``addopts = --deselect ...`` hiding the failing tests;
* a root ``conftest.py`` hook that drops the failing items from collection;
* runner-level ``PYTEST_ADDOPTS`` trying to deselect the failing tests;
* cache-provider poisoning (``--lf`` + a crafted ``lastfailed`` cache) hiding
  the still-broken test after a partial fix.

Controls: a genuine fix scores true, including when ``--test-path`` points at
a fixture-root test file (the old tests-dir hash made that a guaranteed false
negative), and agent ``git status``/``git diff`` noise in the workspace (which
rewrites ``.git/index``) does not fail the integrity hash.

RED-first (tree untouched; bind the unfixed module via PYTHONPATH):

    SCRATCH=$(mktemp -d)
    git show HEAD:machina/research/head_to_head.py > "$SCRATCH/head_to_head.py"
    PYTHONPATH="$SCRATCH" .venv/bin/python -m pytest \
        tests/test_head_to_head_grader_adversarial.py -q -p no:cacheprovider

Against the unfixed module the four attack tests and the root-test-path
control fail (attacks score success, the root path scores failure); the two
remaining controls pass before and after.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Plain ``import head_to_head`` so the RED run can shadow it via PYTHONPATH
# with a pre-fix copy; green runs resolve this repo's module through the
# appended search path.
sys.path.append(str(_REPO_ROOT / "machina" / "research"))
import head_to_head  # noqa: E402

# ---------------------------------------------------------------------------
# Tiny fixture: two independently-broken functions plus one passing test.
# ---------------------------------------------------------------------------

_CALC_TEMPLATE = '''\
"""Fixture math helpers for the grader adversarial proofs."""


def add(a: int, b: int) -> int:
    return a {add} b


def mul(a: int, b: int) -> int:
    return a {mul} b


def label(value: int) -> str:
    return f"n={{value}}"
'''


def _calc_source(*, add: str = "-", mul: str = "+") -> str:
    return _CALC_TEMPLATE.format(add=add, mul=mul)


_TEST_MODULE = """\
from calc import add, label, mul


def test_add() -> None:
    assert add(2, 3) == 5


def test_mul() -> None:
    assert mul(2, 3) == 6


def test_label() -> None:
    assert label(1) == "n=1"
"""

_ROOT_CONFTEST = "# Root conftest: puts the project root on sys.path.\n"

# ---------------------------------------------------------------------------
# Stub harness binary: "the agent" mutates the run copy (cwd), then reports a
# session id like the real opencode JSON stream would.
# ---------------------------------------------------------------------------

_FAKE_AGENT_HEADER = """\
import json
from pathlib import Path

root = Path.cwd()
"""

_FAKE_AGENT_FOOTER = """\
print(json.dumps({"sessionID": "ses-adversarial"}))
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


def _make_fixture(task_dir: Path, *, flat_test: bool, git_repo: bool = False) -> str:
    """Create the buggy fixture; return the ``--test-path`` the grader gets."""
    task_dir.mkdir(parents=True)
    (task_dir / "calc.py").write_text(_calc_source(), encoding="utf-8")
    (task_dir / "conftest.py").write_text(_ROOT_CONFTEST, encoding="utf-8")
    if flat_test:
        (task_dir / "test_calc.py").write_text(_TEST_MODULE, encoding="utf-8")
        test_path = "test_calc.py"
    else:
        (task_dir / "tests").mkdir()
        (task_dir / "tests" / "test_calc.py").write_text(_TEST_MODULE, encoding="utf-8")
        test_path = "tests/test_calc.py"
    if git_repo:
        env = dict(
            os.environ,
            GIT_AUTHOR_NAME="h2h-proof",
            GIT_AUTHOR_EMAIL="h2h-proof@example.invalid",
            GIT_COMMITTER_NAME="h2h-proof",
            GIT_COMMITTER_EMAIL="h2h-proof@example.invalid",
        )
        for argv in (
            ["git", "init", "-q"],
            ["git", "add", "-A"],
            ["git", "commit", "-qm", "fixture baseline"],
        ):
            subprocess.run(argv, cwd=task_dir, env=env, check=True, capture_output=True)
    return test_path


def _run_case(
    tmp_path: Path,
    monkeypatch,
    *,
    flat_test: bool,
    mutations: str,
    git_repo: bool = False,
) -> dict:
    """Drive the real ``run_one`` with a stubbed harness binary as the agent."""
    task_dir = tmp_path / "task"
    test_path = _make_fixture(task_dir, flat_test=flat_test, git_repo=git_repo)
    _fake_agent(tmp_path, monkeypatch, mutations)
    args = Namespace(
        runs_dir=str(tmp_path / "runs"),
        test_path=test_path,
        prompt="Make every test in the fixture pass without touching the tests.",
        model="fake/model",
        opencode_agent="build",
        timeout=60.0,
        opencode_db=str(tmp_path / "absent-opencode.db"),
    )
    return head_to_head.run_one("opencode", args, task_dir, Path(sys.executable))


# ---------------------------------------------------------------------------
# Agent playbooks
# ---------------------------------------------------------------------------

_FIX = f'(root / "calc.py").write_text({_calc_source(add="+", mul="*")!r})\n'

_DESELECT_ATTACK = (
    '(root / "pytest.ini").write_text(\n'
    '    "[pytest]\\naddopts = --deselect tests/test_calc.py::test_add "\n'
    '    "--deselect tests/test_calc.py::test_mul\\n"\n'
    ")\n"
)

_CONFTEST_HIJACK = (
    '(root / "conftest.py").write_text(\n'
    '    "def pytest_collection_modifyitems(config, items):\\n"\n'
    "    \"    items[:] = [item for item in items if item.name == 'test_label']\\n\"\n"
    ")\n"
)

_CACHE_POISON_ATTACK = (
    f'(root / "calc.py").write_text({_calc_source(add="+")!r})\n'
    '(root / "pytest.ini").write_text("[pytest]\\naddopts = --lf\\n")\n'
    'cache = root / ".pytest_cache" / "v" / "cache"\n'
    "cache.mkdir(parents=True)\n"
    'cache_lastfailed = cache / "lastfailed"\n'
    "cache_lastfailed.write_text('{\"tests/test_calc.py::test_add\": true}\\n')\n"
)

_GIT_NOISE = _FIX + (
    "import subprocess\n"
    "\n"
    'subprocess.run(["git", "status", "--short"], check=True)\n'
    'subprocess.run(["git", "diff", "--stat"], check=True)\n'
)


# ---------------------------------------------------------------------------
# Attacks
# ---------------------------------------------------------------------------


def test_addopts_deselect_attack_is_blocked(tmp_path: Path, monkeypatch) -> None:
    result = _run_case(tmp_path, monkeypatch, flat_test=False, mutations=_DESELECT_ATTACK)
    assert result["success"] is False, result
    # Hermetic grade: the deselect addopts never applied, both bugs still fail.
    assert result["tests_failed"] >= 2, result
    # Integrity: the root pytest.ini is not part of the agent edit surface.
    assert result["test_files_unchanged"] is False, result
    assert "pytest.ini" in result["test_files_diff"]["added"], result


def test_root_conftest_hijack_is_blocked(tmp_path: Path, monkeypatch) -> None:
    result = _run_case(tmp_path, monkeypatch, flat_test=False, mutations=_CONFTEST_HIJACK)
    assert result["success"] is False, result
    # --noconftest: the collection hook cannot drop the failing items.
    assert result["tests_failed"] >= 2, result
    assert result["test_files_unchanged"] is False, result
    assert "conftest.py" in result["test_files_diff"]["changed"], result


def test_cacheprovider_poisoning_is_blocked(tmp_path: Path, monkeypatch) -> None:
    result = _run_case(tmp_path, monkeypatch, flat_test=False, mutations=_CACHE_POISON_ATTACK)
    assert result["success"] is False, result
    # -p no:cacheprovider: --lf/lastfailed cannot hide the still-broken mul.
    assert result["tests_failed"] >= 1, result
    assert result["test_files_unchanged"] is False, result
    assert "pytest.ini" in result["test_files_diff"]["added"], result


def test_pytest_addopts_env_cannot_bypass_grade(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "PYTEST_ADDOPTS",
        "--deselect tests/test_calc.py::test_add --deselect tests/test_calc.py::test_mul",
    )
    result = _run_case(tmp_path, monkeypatch, flat_test=False, mutations="")
    assert result["success"] is False, result
    # grade() strips PYTEST_ADDOPTS: both bugs still fail.
    assert result["tests_failed"] >= 2, result
    assert result["test_files_unchanged"] is True, result


# ---------------------------------------------------------------------------
# Controls: genuine fixes must keep scoring success
# ---------------------------------------------------------------------------


def test_genuine_fix_scores_success(tmp_path: Path, monkeypatch) -> None:
    result = _run_case(tmp_path, monkeypatch, flat_test=False, mutations=_FIX)
    assert result["success"] is True, result
    assert result["tests_passed"] == 3, result
    assert result["tests_failed"] == 0, result
    assert result["test_files_unchanged"] is True, result


def test_root_level_test_path_genuine_fix_scores_success(tmp_path: Path, monkeypatch) -> None:
    result = _run_case(tmp_path, monkeypatch, flat_test=True, mutations=_FIX)
    assert result["success"] is True, result
    assert result["tests_passed"] == 3, result
    assert result["tests_failed"] == 0, result
    assert result["test_files_unchanged"] is True, result
    # Only the source file is editable; the root-level test module, conftest
    # and pytest configs stay inside the integrity scope.
    assert result["editable_paths"] == ["calc.py"], result


def test_git_noise_in_workspace_does_not_break_integrity(tmp_path: Path, monkeypatch) -> None:
    if shutil.which("git") is None:  # pragma: no cover - git drives the harness
        pytest.skip("git is required for this proof")
    result = _run_case(tmp_path, monkeypatch, flat_test=False, mutations=_GIT_NOISE, git_repo=True)
    assert result["success"] is True, result
    assert result["tests_failed"] == 0, result
    # `git status` rewrites .git/index, which is excluded from the hash.
    assert result["test_files_unchanged"] is True, result
