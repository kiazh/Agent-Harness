"""Bounded single-pass repo inventory: files, LOC, entry points, tests, imports."""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field

from machina.repomap.cache import ContentHashCache

DEFAULT_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        "node_modules",
        ".coverage",
        "htmlcov",
    }
)
DEFAULT_EXCLUDE_SUFFIXES = (".pyc", ".pyo")
MAX_BYTES_PER_FILE = 2_000_000  # bound: skip giant blobs, still count them


@dataclass
class RepoMap:
    root: str
    files: list[str] = field(default_factory=list)
    loc_by_file: dict[str, int] = field(default_factory=dict)
    total_loc: int = 0
    entry_points: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    import_edges: dict[str, list[str]] = field(default_factory=dict)
    skipped_large: list[str] = field(default_factory=list)
    files_scanned: int = 0  # files parsed this pass (cache hits re-hash, never parse)


def _is_test(path_rel: str) -> bool:
    base = os.path.basename(path_rel)
    return base.startswith("test_") or base.endswith("_test.py") or path_rel.startswith("tests/")


def _parse(source: str) -> tuple[int, list[str], bool]:
    """Return (loc, raw_imports, is_entry_point). Never raises on syntax errors."""
    loc = source.count("\n") + (1 if source and not source.endswith("\n") else 0)
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return loc, [], False
    imports: list[str] = []
    entry = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
        elif isinstance(node, ast.Compare):
            # detect `if __name__ == "__main__":` via Name/Constant compare
            left = node.left
            if (
                isinstance(left, ast.Name)
                and left.id == "__name__"
                and any(
                    isinstance(c, ast.Constant) and c.value == "__main__" for c in node.comparators
                )
            ):
                entry = True
    return loc, imports, entry


def _local_module(root: str, rel: str) -> str:
    mod = rel[: -len(".py")] if rel.endswith(".py") else rel
    return mod.replace(os.sep, ".")


def build_repomap(root: str, cache: ContentHashCache | None = None) -> RepoMap:
    """Single bounded pass over ``root``. Repeat runs with the same cache object
    (or same cache file) perform zero parses of unchanged files:
    ``files_scanned == 0``; ``lookup`` still re-hashes each cached file so a
    same-stat rewrite cannot serve stale inventory data."""
    if cache is None:
        cache = ContentHashCache()
    assert isinstance(cache, ContentHashCache)
    m = RepoMap(root=os.path.abspath(root))
    for dirpath, dirnames, filenames in os.walk(m.root):
        dirnames[:] = sorted(d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS)
        for name in sorted(filenames):
            if name.endswith(DEFAULT_EXCLUDE_SUFFIXES):
                continue
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, m.root)
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                continue
            m.files.append(rel)
            if size > MAX_BYTES_PER_FILE:
                m.skipped_large.append(rel)
                continue
            if not name.endswith(".py"):
                continue
            cached = cache.lookup(abs_path)
            if cached is not None:
                loc, imports, entry = cached
            else:
                try:
                    with open(abs_path, encoding="utf-8", errors="replace") as fh:
                        source = fh.read()
                except OSError:
                    continue
                m.files_scanned += 1
                loc, imports, entry = _parse(source)
                cache.store(abs_path, (loc, imports, entry))
            m.loc_by_file[rel] = loc
            m.total_loc += loc
            if entry or name == "__main__.py":
                m.entry_points.append(rel)
            if _is_test(rel):
                m.test_files.append(rel)
            if imports:
                m.import_edges[_local_module(m.root, rel)] = sorted(set(imports))
    m.files.sort()
    m.entry_points.sort()
    m.test_files.sort()
    return m
