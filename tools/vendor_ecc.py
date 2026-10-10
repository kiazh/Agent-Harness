"""Reproduce the pinned ECC bundle from a local clone; never runs upstream code."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

PIN = "4eb71d92a39cab44ad40ac9d8a6a5ccb4029d6c2"


def main(source: Path, target: Path) -> None:
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if revision != PIN:
        raise ValueError(f"Expected ECC commit {PIN}, got {revision}")
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--"], cwd=source, check=True)
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=source).decode().split("\0")
    files = {}
    entries = []
    for relative in sorted(filter(None, paths)):
        original = source / relative
        if original.is_symlink():
            raise ValueError(f"Refusing symlink: {relative}")
        if not original.is_file():
            raise ValueError(f"Missing tracked file: {relative}")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
        files[relative] = hashlib.sha256(original.read_bytes()).hexdigest()
        if relative.startswith("skills/") and original.name == "SKILL.md":
            metadata = yaml.safe_load(original.read_text().split("---", 2)[1])
            entries.append(
                {
                    "name": "ecc-" + metadata["name"],
                    "description": metadata["description"],
                    "path": relative,
                }
            )
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source": "https://github.com/affaan-m/ECC",
        "revision": PIN,
        "license": "MIT",
        "skills": entries,
        "files": files,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"Vendored {len(entries)} skills and {len(files)} files at {revision}")
    print("Manifest SHA256:", hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest())


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(__file__).resolve().parents[1] / "skills" / "ecc")
