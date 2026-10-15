"""Scratch driver: real h2h fixtures through the FIXED grader.

1. Replay the real matrix run copies (genuine agent fixes, untouched tests):
   every historically PASSing cell must still score success=true.
2. Pristine task1 + stub agent that applies the genuine fix AND leaves a stray
   root ``pytest.ini``: tests pass under the hermetic grade, but the integrity
   hash must fail the run (success=false, pytest.ini in the diff).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from argparse import Namespace
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO / "machina" / "research"))
import head_to_head  # noqa: E402

H2H = Path("/private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode/h2h")
SCRATCH = Path("/private/var/folders/sk/y8w9dwmd4lnc2yvygqqxd4xw0000gn/T/opencode/r4-020/replay3")
TASK_PATHS = {
    "task1": "tests/test_calc.py",
    "task2": "tests/test_pipeline.py",
    "task3": "tests/test_expiry.py",
}

if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)


def _make_stub(bin_dir: Path, code: str) -> Path:
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "opencode"
    stub.write_text(f"#!{sys.executable}\n{code}", encoding="utf-8")
    stub.chmod(0o755)
    return bin_dir


_NOOP_BIN = _make_stub(
    SCRATCH / "bin-noop",
    'import json\nprint(json.dumps({"sessionID": "ses-noop"}))\n',
)
_OLD = 'f"{total % 24:02d}:{total % 60:02d}"'
_NEW = 'f"{(total // 60) % 24:02d}:{total % 60:02d}"'
_STRAY_BIN = _make_stub(
    SCRATCH / "bin-stray",
    (
        "import json\n"
        "from pathlib import Path\n"
        "root = Path.cwd()\n"
        "calc = (root / 'calc.py').read_text()\n"
        f"assert {_OLD!r} in calc, 'fixture shape changed'\n"
        f"(root / 'calc.py').write_text(calc.replace({_OLD!r}, {_NEW!r}, 1))\n"
        "(root / 'pytest.ini').write_text('[pytest]\\naddopts = -q\\n')\n"
        "print(json.dumps({'sessionID': 'ses-stray'}))\n"
    ),
)


def _run(task_dir: Path, task: str, bin_dir: Path) -> dict:
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    args = Namespace(
        runs_dir=str(SCRATCH / "runs"),
        test_path=TASK_PATHS[task],
        prompt="noop",
        model="fake/model",
        opencode_agent="build",
        timeout=60.0,
        opencode_db=str(SCRATCH / "absent.db"),
    )
    result = head_to_head.run_one("opencode", args, task_dir, Path(sys.executable))
    return {
        "success": result["success"],
        "passed": result["tests_passed"],
        "failed": result["tests_failed"],
        "unchanged": result["test_files_unchanged"],
        "editable": result["editable_paths"],
        "diff": result.get("test_files_diff"),
        "summary": result["pytest_summary"],
    }


summary: dict[str, dict] = {}

# 1. Real matrix run copies (post-run workspaces).
for task in ("task1", "task2", "task3"):
    for harness in ("machina", "opencode"):
        src = H2H / "runs" / task / harness
        task_dir = SCRATCH / f"replay-{task}-{harness}"
        shutil.copytree(src, task_dir, symlinks=True)
        summary[f"replay/{task}/{harness}"] = _run(task_dir, task, _NOOP_BIN)

# 2. Pristine task1 + genuine fix + stray agent-written pytest.ini.
task_dir = SCRATCH / "stray-config-task1"
shutil.copytree(H2H / "task1", task_dir, symlinks=True)
summary["stray/task1/fix + agent pytest.ini"] = _run(task_dir, "task1", _STRAY_BIN)

print(json.dumps(summary, indent=2))
