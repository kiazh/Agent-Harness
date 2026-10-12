"""Validate a built wheel and import it outside the source checkout.

Run with the project's installed runtime dependencies. No database/provider calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


def check_source(archive_path: Path) -> None:
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        names = {"/".join(Path(member.name).parts[1:]) for member in members}
        required = {
            "pyproject.toml",
            "README.md",
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
            "docker-compose.yml",
            "ui/package-lock.json",
            "ui/src/main.ts",
            "ah/connectors/execution.py",
        }
        if missing := required - names:
            raise ValueError(f"Missing source resources: {sorted(missing)}")
        if any(
            any(
                part == "node_modules"
                or part.startswith(".venv")
                or part in {".env", "__pycache__"}
                for part in Path(name).parts
            )
            for name in names
        ):
            raise ValueError("Source archive contains local dependencies or state")
    print(f"Verified clean source distribution: {archive_path.name}")


def check(wheel: Path) -> None:
    if wheel.name.endswith(".tar.gz"):
        check_source(wheel)
        return
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        required = {
            "ah/db/schema.sql",
            "ah/bundled_agents/ecc/LICENSE",
            "ah/bundled_agents/ecc/manifest.json",
            "skills/ecc/LICENSE",
        }
        missing = required - names
        if missing:
            raise ValueError(f"Missing packaged resources: {sorted(missing)}")
        manifest = json.loads(archive.read("ah/bundled_agents/ecc/manifest.json"))
        if len(manifest["agents"]) != 71:
            raise ValueError("Expected all 71 ECC subagents")
        skills = json.loads(archive.read("skills/ecc/manifest.json"))
        for name, digest in skills["files"].items():
            if hashlib.sha256(archive.read(f"skills/ecc/{name}")).hexdigest() != digest:
                raise ValueError(f"Bundled skill resource hash mismatch: {name}")
        for name, entry in manifest["agents"].items():
            data = archive.read(f"ah/bundled_agents/ecc/{name}.md")
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError(f"Bundled agent hash mismatch: {name}")
        forbidden = [
            name
            for name in names
            if any(
                part in {".env", "node_modules", "__pycache__", ".venv.broken.bak"}
                for part in Path(name).parts
            )
        ]
        if forbidden:
            raise ValueError(f"Unexpected local artifacts: {forbidden}")
        with tempfile.TemporaryDirectory(prefix="ah-release-wheel-") as directory:
            root = Path(directory).resolve()
            for member in archive.infolist():
                target = (root / member.filename).resolve()
                if not target.is_relative_to(root):
                    raise ValueError("Wheel contains an escaping member path")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(member))
            probe = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import ah
from ah.core.agent_def import BUILTIN_AGENTS
assert Path(ah.__file__).is_relative_to(Path(sys.argv[1]))
assert len([a for a in BUILTIN_AGENTS.values() if a.source == 'ecc']) == 71
assert len(BUILTIN_AGENTS) == 74
print('Isolated wheel imports, 71 ECC agents, resource hashes: OK')
"""
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.endswith("API_KEY")
                and key
                not in {
                    "DATABASE_URL",
                    "AGENT_HARNESS_DATABASE_URL",
                    "AGENT_HARNESS_TEST_DATABASE_URL",
                    "AGENT_HARNESS_SECRET_BACKEND",
                }
            }
            environment.update(
                {
                    "HOME": directory,
                    "AH_ENV_FILE": str(root / "empty.env"),
                    "AGENT_HARNESS_SECRET_BACKEND": "",
                }
            )
            subprocess.run(
                [sys.executable, "-I", "-c", probe, directory],
                cwd=directory,
                env=environment,
                check=True,
                timeout=30,
            )
    print(f"Verified {wheel.name}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python tools/check_release_artifacts.py path/to/dist/*")
    for argument in sys.argv[1:]:
        check(Path(argument).resolve())
