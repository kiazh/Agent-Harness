"""Vendor only canonical ECC agent Markdown and its MIT notice from a local snapshot.

Never executes upstream code or imports skills, hooks, commands or MCP config.
Usage: python -m tools.vendor_ecc_agents /path/to/pinned/ECC
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

PIN = "b92f181b3b748d1abbc2afd94af6aa7d8c8fca29"
DESTINATION = Path(__file__).resolve().parents[1] / "ah" / "bundled_agents" / "ecc"


def vendor(source: Path, destination: Path = DESTINATION) -> dict:
    """Copy a reviewed snapshot byte-for-byte and record reproducible hashes."""
    snapshot = source / "source.json"
    revision = (
        json.loads(snapshot.read_text())["commit"]
        if snapshot.is_file()
        else subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    )
    if revision != PIN:
        raise ValueError(f"expected pinned ECC revision {PIN}")
    paths = sorted((source / "agents").glob("*.md"))
    if not paths or not (source / "LICENSE").is_file():
        raise ValueError("expected agents/*.md and LICENSE in the pinned ECC snapshot")
    agents = {}
    for path in paths:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        parts = text.split("---", 2)
        if not text.startswith("---\n") or len(parts) != 3:
            raise ValueError(f"missing agent frontmatter: {path.name}")
        metadata = yaml.safe_load(parts[1])
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if name != path.stem or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise ValueError(f"invalid agent name: {path.name}")
        if not isinstance(metadata.get("description"), str) or not isinstance(
            metadata.get("tools"), str
        ):
            raise ValueError(f"invalid agent metadata: {path.name}")
        agents[name] = {
            "path": f"agents/{path.name}",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "upstream_model": metadata.get("model"),
            "upstream_tools": [tool.strip() for tool in metadata["tools"].split(",")],
        }
    manifest = {
        "repository": "https://github.com/affaan-m/ECC",
        "revision": PIN,
        "agents": agents,
    }
    destination.mkdir(parents=True, exist_ok=True)
    for path in paths:
        shutil.copyfile(path, destination / path.name)
    shutil.copyfile(source / "LICENSE", destination / "LICENSE")
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m tools.vendor_ecc_agents /path/to/pinned/ECC")
    result = vendor(Path(sys.argv[1]))
    print(f"Vendored {len(result['agents'])} ECC agents at {PIN}")
