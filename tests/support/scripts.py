"""Explicit administrator script policies for tests that execute trusted scripts."""

import json
from pathlib import Path


def allow_scripts(root: Path, scripts: dict[str, list[str]]) -> None:
    policy = root / ".script-policy.json"
    policy.write_text(json.dumps({"version": 1, "scripts": scripts}), encoding="utf-8")
    policy.chmod(0o600)
