"""Reproduce the pinned ECC bundle from a local clone; never runs upstream code."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

PIN = "4eb71d92a39cab44ad40ac9d8a6a5ccb4029d6c2"

# Deliberate default scope: this Python/TypeScript agent harness and its next UI/MCP work.
# Keep instructions unchanged; removed domains can still be installed as local skills.
CURATED_SKILLS = frozenset(
    """agent-architecture-audit agent-harness-construction agent-introspection-debugging
    agent-security-hardening api-connector-builder api-design architecture-decision-records
    benchmark-optimization-loop codebase-onboarding coding-standards content-hash-cache-pattern
    contract-first cost-aware-llm-pipeline database-migrations deep-research deployment-patterns
    documentation-lookup docker-patterns error-handling eval-harness fastapi-patterns git-workflow
    growth-log hexagonal-architecture inherit-legacy-style intent-driven-development
    iterative-retrieval latency-critical-systems mcp-dependency-review mcp-server-patterns
    postgres-patterns production-audit prompt-caching-strategy prompt-optimizer python-patterns
    python-testing regex-vs-llm-structured-text repo-scan search-first security-review
    tdd-workflow verification-loop""".split()
)
RESOURCE_ROOTS = (
    "scripts/",
    "agents/",
    "rules/",
    "docs/",
    "schemas/",
    "contexts/",
    "examples/eval-harness/",
)


def select_files(source: Path, paths: set[str]) -> tuple[set[str], list[str]]:
    """Close static resource references without adding unrelated skill catalogs."""
    roots = {f"skills/{name}/" for name in CURATED_SKILLS}
    selected = {
        p
        for p in paths
        if any(p.startswith(root) for root in roots)
        or p.upper() in {"LICENSE", "COPYING", "NOTICE"}
    }
    excluded: set[str] = set()
    pending = sorted(selected)
    while pending:
        relative = pending.pop()
        try:
            text = (source / relative).read_text()
        except UnicodeDecodeError:
            continue
        candidates = re.findall(r"\]\(([^)\s]+)(?:\s+[^)]*)?\)", text)
        candidates += re.findall(r"[`\"']([^`\"'\n\s]+)[`\"']", text)
        # Paths in shell variables and unquoted command arguments.
        candidates += re.findall(
            r"(?:skills|scripts|agents|rules|docs|schemas|contexts|hooks|src)/"
            r"[\w./-]+\.[\w-]+",
            text,
        )
        for arguments in re.findall(r"path\.join\(__dirname,\s*([^)]*)\)", text):
            fragments = re.findall(r"['\"]([^'\"]+)['\"]", arguments)
            if fragments:
                candidates.append(posixpath.join(*fragments))
        if relative.endswith(".py"):
            for dots, module in re.findall(r"\bfrom\s+(\.+)([\w.]*)\s+import", text):
                base = posixpath.dirname(relative)
                for _ in range(len(dots) - 1):
                    base = posixpath.dirname(base)
                candidates.append("/" + base + "/" + module.replace(".", "/") + ".py")
        for candidate in candidates:
            candidate = candidate.split("#", 1)[0].split("?", 1)[0]
            if not candidate or "://" in candidate or candidate.startswith(("#", "mailto:")):
                continue
            options = [
                posixpath.normpath(posixpath.join(posixpath.dirname(relative), candidate)),
                candidate.removeprefix("/"),
            ]
            matches: set[str] = set()
            for option in options:
                if option in paths:
                    matches.add(option)
                if relative.endswith((".js", ".mjs", ".cjs", ".ts")):
                    matches.update(
                        p
                        for p in (
                            option + ".js",
                            option + ".mjs",
                            option + ".json",
                            option + "/index.js",
                        )
                        if p in paths
                    )
                elif candidate.endswith("/") and option.startswith("rules/"):
                    matches.update(p for p in paths if p.startswith(option.rstrip("/") + "/"))
            for match in matches:
                if match.startswith("skills/") and not any(
                    match.startswith(root) for root in roots
                ):
                    excluded.add(match)
                    continue
                if (
                    match.startswith("rules/")
                    and not match.startswith(
                        ("rules/common/", "rules/python/", "rules/typescript/")
                    )
                    and match != "rules/README.md"
                ):
                    excluded.add(match)
                    continue
                if not match.startswith("skills/") and not match.startswith(RESOURCE_ROOTS):
                    if match not in selected:
                        excluded.add(match)
                    continue
                if match not in selected:
                    selected.add(match)
                    pending.append(match)
    return selected, sorted(excluded)


def confined_path(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError(f"Unsafe vendor path: {relative}")
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Refusing symlink or escaping path: {relative}")
    return path


def main(source: Path, target: Path) -> None:
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if revision != PIN:
        raise ValueError(f"Expected ECC commit {PIN}, got {revision}")
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--"], cwd=source, check=True)
    paths = set(
        filter(
            None,
            subprocess.check_output(["git", "ls-files", "-z"], cwd=source).decode().split("\0"),
        )
    )
    # Reject unsafe tracked content even before following its resource references.
    for relative in paths:
        confined_path(source, relative)
    selected, excluded = select_files(source, paths)
    manifest_path = target / "manifest.json"
    previous = json.loads(manifest_path.read_text())["files"] if manifest_path.exists() else {}
    # Validate every previously managed file before overwriting or deleting anything.
    for relative, digest in previous.items():
        old = confined_path(target, relative)
        if old.exists() and hashlib.sha256(old.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Refusing to overwrite/delete modified vendor file: {relative}")
    files = {}
    entries = []
    for relative in sorted(selected):
        original = source / relative
        if original.is_symlink():
            raise ValueError(f"Refusing symlink: {relative}")
        if not original.is_file():
            raise ValueError(f"Missing tracked file: {relative}")
        destination = confined_path(target, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
        files[relative] = hashlib.sha256(original.read_bytes()).hexdigest()
        if relative.startswith("skills/") and original.name == "SKILL.md":
            text = original.read_text()
            parts = text.split("---", 2)
            if not text.startswith("---\n") or len(parts) != 3:
                raise ValueError(f"Skill SKILL.md has no YAML frontmatter: {relative}")
            metadata = yaml.safe_load(parts[1])
            if (
                not isinstance(metadata, dict)
                or not isinstance(metadata.get("name"), str)
                or not isinstance(metadata.get("description"), str)
            ):
                raise ValueError(
                    f"Skill frontmatter must define string name/description: {relative}"
                )
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
        "distribution": "curated-harness",
        "upstream_file_count": len(paths),
        "excluded_references": excluded,
        "skills": entries,
        "files": files,
    }
    for relative in previous.keys() - selected:
        old = confined_path(target, relative)
        if old.exists():
            old.unlink()
    # Do not remove files that were never owned by the prior manifest.
    for directory in sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
            directory.rmdir()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"Vendored {len(entries)} skills and {len(files)} files at {revision}")
    print("Manifest SHA256:", hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest())


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(__file__).resolve().parents[1] / "skills" / "ecc")
