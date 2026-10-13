"""``learn_directory`` — bounded deterministic RepoMap digest of a workspace directory.

Step one of "open a directory in Machina, learn it, then debug it": scans with
``machina.repomap`` (pure stdlib, no provider calls), persists the content-hash
cache under the machina home so separate sessions reuse it (repeat scan = zero
file parses), and returns a bounded digest of counts, hubs and paths — never
file contents. Read-only; workspace escapes are rejected with an Error string.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

from machina.repomap import ContentHashCache, RepoMap, build_repomap
from machina.tools.base import _core_symbol, registry
from machina.tools.file import resolve_path

_MAX_DIGEST_CHARS = 4000
_TOP_MODULES = 10
_TOP_HUBS = 5
_TOP_OUT_DEGREE = 5
_TOP_PACKAGES = 15
_SAMPLE_LIMIT = 8
_TRUNCATION_MARKER = "\n[digest truncated]"


def _machina_home() -> Path:
    """Configured machina home, ``~/.machina`` fallback. Never raises."""
    configured = ""
    try:
        configured = _core_symbol("machina.core.config", "config").get("machina_home") or ""
    except Exception:
        configured = ""
    return Path(configured).expanduser() if configured else Path.home() / ".machina"


def _open_cache(root: str) -> tuple[ContentHashCache, str]:
    """Open the persistent per-root cache, or a fresh in-memory fallback.

    Returns ``(cache, label)``. Persistence is best-effort: an unwritable or
    unusable machina home degrades to an in-memory cache with an explicit
    label instead of failing the tool.
    """
    try:
        home = _machina_home()
        home.mkdir(parents=True, exist_ok=True)
        if not os.access(home, os.W_OK):
            raise OSError(f"machina home is not writable: {home}")
        digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
        path = home / f"repomap-{digest}.json"
        return ContentHashCache(str(path)), str(path)
    except (OSError, ValueError):
        return ContentHashCache(), "in-memory (machina home unavailable)"


def _local_names(repomap: RepoMap) -> set[str]:
    """Dotted local module names (``pkg/a.py`` -> ``pkg.a``), matching scan edges."""
    return {
        rel[: -len(".py")].replace(os.sep, ".")
        for rel in repomap.loc_by_file
        if rel.endswith(".py")
    }


def _resolve_local(name: str, known: set[str]) -> str | None:
    """Map an import name onto the longest matching local module.

    ``from pkg import b`` records the bare ``pkg``; the module that actually
    executes is ``pkg/__init__.py``, so package imports credit the package.
    """
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in known:
            return prefix
        if f"{prefix}.__init__" in known:
            return prefix + ".__init__"
    return None


def _fan_in(edges: dict[str, list[str]], known: set[str]) -> dict[str, int]:
    """Importers per local module (distinct importers, exact local targets)."""
    counts: dict[str, int] = {}
    for importer, imports in edges.items():
        targets = {
            target
            for target in (_resolve_local(name, known) for name in imports)
            if target is not None and target != importer
        }
        for target in targets:
            counts[target] = counts.get(target, 0) + 1
    return counts


def _sample(items: list[str]) -> str:
    head = ", ".join(items[:_SAMPLE_LIMIT])
    if len(items) > _SAMPLE_LIMIT:
        head += f", +{len(items) - _SAMPLE_LIMIT} more"
    return head or "none"


def _package_counts(files: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rel in files:
        norm = rel.replace(os.sep, "/")
        key = norm.split("/")[0] if "/" in norm else "(root)"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _format_digest(repomap: RepoMap, cache_label: str) -> str:
    """Deterministic bounded digest: same tree + same cache label = same text."""
    m = repomap
    parsed = m.files_scanned
    cached = parsed == 0 and bool(m.loc_by_file)
    status = "cached (0 files parsed)" if cached else f"fresh (parsed {parsed} files)"
    lines = [
        f"learn_directory: {m.root} (read-only digest; no file contents)",
        f"scan: {status} | cache: {cache_label}",
        f"totals: files={len(m.files)} modules={len(m.loc_by_file)} loc={m.total_loc}",
    ]
    if m.skipped_large:
        lines.append(f"large file content skipped: {len(m.skipped_large)}")
    packages = _package_counts(m.files)
    top_packages = sorted(packages.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = ", ".join(f"{name}={count}" for name, count in top_packages[:_TOP_PACKAGES])
    if len(top_packages) > _TOP_PACKAGES:
        shown += f", +{len(top_packages) - _TOP_PACKAGES} more"
    lines.append(f"packages: {shown or 'none'}")
    largest = sorted(m.loc_by_file.items(), key=lambda kv: (-kv[1], kv[0]))[:_TOP_MODULES]
    lines.append(f"largest modules ({len(m.loc_by_file)} total):")
    lines.extend(f"  {loc:>7}  {rel.replace(os.sep, '/')}" for rel, loc in largest)
    hubs = sorted(_fan_in(m.import_edges, _local_names(m)).items(), key=lambda kv: (-kv[1], kv[0]))[
        :_TOP_HUBS
    ]
    lines.append("import hubs (fan-in): " + (", ".join(f"{mod}={n}" for mod, n in hubs) or "none"))
    out_degree = sorted(m.import_edges.items(), key=lambda kv: (-len(kv[1]), kv[0]))[
        :_TOP_OUT_DEGREE
    ]
    lines.append(
        "most imports (out-degree): "
        + (", ".join(f"{mod}={len(imports)}" for mod, imports in out_degree) or "none")
    )
    lines.append(f"entry points ({len(m.entry_points)}): {_sample(m.entry_points)}")
    lines.append(f"tests ({len(m.test_files)}): {_sample(m.test_files)}")
    text = "\n".join(lines)
    if len(text) > _MAX_DIGEST_CHARS:
        # Bound the response, never the scan: keep the highest-signal lines.
        cut = text[: _MAX_DIGEST_CHARS - len(_TRUNCATION_MARKER)]
        text = cut.rsplit("\n", 1)[0] + _TRUNCATION_MARKER
    return text


def _scan(root: str) -> str:
    """Build/refresh the RepoMap for *root* and return its bounded digest."""
    cache, label = _open_cache(root)
    repomap = build_repomap(root, cache=cache)
    try:
        cache.save()
    except OSError:
        # Cache persistence is best-effort; a read-only home must not fail learning.
        pass
    return _format_digest(repomap, label)


@registry.register(
    name="learn_directory",
    description=(
        "Learn a workspace directory: a bounded, deterministic inventory digest "
        "(files, modules, LOC, packages, largest modules, import hubs, entry "
        "points, tests). Read-only; returns no file contents. Repeat calls reuse "
        "a persistent content-hash cache and report cached (0 files parsed)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Directory to learn (workspace-relative or absolute inside "
                    "the workspace; default: workspace root)"
                ),
            }
        },
        "required": [],
        "additionalProperties": False,
    },
    effects=("fs.read",),
)
async def learn_directory(path: str = ".") -> str:
    """Return a bounded digest of the directory at *path* (workspace-bounded)."""
    try:
        target = resolve_path(path)
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"
    if not target.exists():
        return f"Error: directory not found: {path}"
    if not target.is_dir():
        return f"Error: not a directory: {path}"
    return await asyncio.to_thread(_scan, str(target))
