#!/usr/bin/env python3
"""R4-095 probe: tools/vendor_ecc.py frontmatter validation (IndexError -> ValueError).

Builds throwaway pinned-revision git fixtures in a temp dir and runs both the
fixed importer and the pre-fix revision (read from git HEAD) against them.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
from tools import vendor_ecc  # noqa: E402

FAILURES = 0


def check(label: str, actual, expected) -> None:
    global FAILURES
    if actual == expected:
        print(f"PASS {label:56} = {actual!r}")
    else:
        print(f"FAIL {label:56} = {actual!r} (expected {expected!r})")
        FAILURES += 1


def build_repo(root: Path, skill_md: str) -> str:
    (root / "LICENSE").write_text("MIT fixture notice\n", encoding="utf-8")
    skill = root / "skills" / "python-patterns"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(skill_md, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Probe", "-c", "user.email=probe@example.invalid",
         "commit", "-qm", "fixture"],
        cwd=root,
        check=True,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def load_old_module(tmp: Path):
    old_source = subprocess.check_output(
        ["git", "show", "HEAD:tools/vendor_ecc.py"], cwd=REPO, text=True
    )
    old_path = tmp / "vendor_ecc_old.py"
    old_path.write_text(old_source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("vendor_ecc_old", old_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_case(module, fixture: Path, revision: str, target: Path) -> Exception | None:
    module.PIN = revision
    try:
        module.main(fixture, target)
        return None
    except Exception as error:  # the probe classifies the error type
        return error


NO_FRONTMATTER = "Just prose with no YAML frontmatter delimiters.\n"
MISSING_DESCRIPTION = "---\nname: python-patterns\n---\nBody text.\n"
VALID = "---\nname: python-patterns\ndescription: Python patterns\n---\nBody text.\n"

with tempfile.TemporaryDirectory(prefix="r4095-vendor-") as tmpdir:
    tmp = Path(tmpdir)
    old_module = load_old_module(tmp)

    # RED: the pre-fix importer trips over a SKILL.md without frontmatter.
    fixture = tmp / "red-repo"
    fixture.mkdir()
    revision = build_repo(fixture, NO_FRONTMATTER)
    red_error = run_case(old_module, fixture, revision, tmp / "red-out")
    check("RED pre-fix error type", type(red_error).__name__, "IndexError")

    # GREEN: the fixed importer reports a clear validation error.
    fixture = tmp / "green-repo"
    fixture.mkdir()
    revision = build_repo(fixture, NO_FRONTMATTER)
    error = run_case(vendor_ecc, fixture, revision, tmp / "green-out")
    check("fixed error type", type(error).__name__, "ValueError")
    check(
        "fixed error names the file and cause",
        "no YAML frontmatter" in str(error) and "skills/python-patterns/SKILL.md" in str(error),
        True,
    )

    # GREEN: frontmatter that parses but lacks name/description is rejected too.
    fixture = tmp / "mdesc-repo"
    fixture.mkdir()
    revision = build_repo(fixture, MISSING_DESCRIPTION)
    error = run_case(vendor_ecc, fixture, revision, tmp / "mdesc-out")
    check("missing-description error type", type(error).__name__, "ValueError")
    check(
        "missing-description message",
        "string name/description" in str(error),
        True,
    )

    # GREEN: valid frontmatter still vendors successfully.
    fixture = tmp / "ok-repo"
    fixture.mkdir()
    revision = build_repo(fixture, VALID)
    error = run_case(vendor_ecc, fixture, revision, tmp / "ok-out")
    check("valid fixture importer error", error, None)
    check("valid fixture manifest written", (tmp / "ok-out" / "manifest.json").is_file(), True)

print()
if FAILURES == 0:
    print("== result: ALL PROBES PASS")
    sys.exit(0)
print(f"== result: {FAILURES} FAILURE(S)")
sys.exit(1)
