"""Stream C — leaf independence: converted leaves import with machina.core blocked.

For each converted leaf module, proves decoupling in two ways:
1. Static (AST): no module-level ``machina.core`` import remains (the seam
   uses ``sys.modules``/``importlib`` with zero ``Import`` nodes, so the
   deferred-import census does not grow).
2. Dynamic: the module file executes with ``machina.core`` import-blocked
   (parent packages stubbed by path so their ``__init__`` coupling does not
   interfere; leaf-local deps are converted-or-clean by construction).

Plus a parity spot-check with core present: each seam callable resolves to
the same object as before (``is`` identity against ``machina.core``).
"""

from __future__ import annotations

import ast
import importlib
import importlib.abc
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CONVERTED = [
    "machina.tools.base",
    "machina.memory.models",
    "machina.memory.identity",
    "machina.memory.user_profile",
    "machina.rag.loaders",
    "machina.rag.embedder",
    "machina.rag.reranker",
]

# Already independent controls (no module-level core import; no edits made).
ALREADY_CLEAN = [
    "machina.memory.redaction",
    "machina.memory.forgetting",
    "machina.memory.scorer",
    "machina.memory.policy",
    "machina.rag.chunker",
    "machina.tools.skills",
    "machina.tools.repo",
]


def _module_file(dotted: str) -> Path:
    return ROOT / Path(*dotted.split(".")) .with_suffix(".py")


def _top_level_core_imports(dotted: str) -> list[str]:
    """Core imports executed at module import time (outside func/class bodies)."""
    tree = ast.parse(_module_file(dotted).read_text())

    found: list[str] = []

    def visit(node: ast.AST, depth: int) -> None:
        nonlocal found
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            depth += 1
        if isinstance(node, (ast.Import, ast.ImportFrom)) and depth == 0:
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            else:
                mods = [node.module or ""]
            for m in mods:
                if m == "machina.core" or m.startswith("machina.core."):
                    found.append(f"line {node.lineno}: {m}")
        for child in ast.iter_child_nodes(node):
            # depth increments only for func/class; If/Try at top level still
            # execute at import, so they count as top-level (depth unchanged).
            visit(child, depth)

    visit(tree, 0)
    return found


class _BlockCore(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "machina.core" or fullname.startswith("machina.core."):
            raise ImportError(f"leaf-independence: blocked {fullname}")
        return None


def _import_with_core_blocked(dotted: str):
    """Import *dotted* with machina.core unimportable; stub leaf parents by path."""
    saved_modules = dict(sys.modules)
    saved_meta = list(sys.meta_path)
    parts = dotted.split(".")
    parent_name = ".".join(parts[:-1])
    parent_rel = Path(*parts[:-1])
    try:
        # Stub the immediate parent package so its real __init__ (which fans
        # out to still-coupled siblings) never executes during this import.
        stub = types.ModuleType(parent_name)
        stub.__path__ = [str(ROOT / parent_rel)]  # type: ignore[attr-defined]
        sys.modules[parent_name] = stub
        # Drop cached leaf + core modules so the import really re-executes.
        sys.modules.pop(dotted, None)
        for key in [k for k in sys.modules if k == "machina.core" or k.startswith("machina.core.")]:
            sys.modules.pop(key, None)
        sys.meta_path.insert(0, _BlockCore())
        # Core must be absent: any direct/indirect core import now raises.
        assert "machina.core" not in sys.modules
        return importlib.import_module(dotted)
    finally:
        sys.modules.clear()
        sys.modules.update(saved_modules)
        sys.meta_path[:] = saved_meta


@pytest.mark.parametrize("dotted", CONVERTED + ALREADY_CLEAN)
def test_no_top_level_core_import(dotted):
    assert _top_level_core_imports(dotted) == [], (
        f"{dotted} still has module-level machina.core imports"
    )


@pytest.mark.parametrize("dotted", CONVERTED + ALREADY_CLEAN)
def test_imports_with_core_blocked(dotted):
    mod = _import_with_core_blocked(dotted)
    assert mod is not None
    assert getattr(mod, "_core_symbol", None) is None or callable(mod._core_symbol)


def _resolve(dotted: str, module: str, name: str):
    mod = importlib.import_module(dotted)
    return mod._core_symbol(module, name)


@pytest.mark.parametrize(
    "dotted,module,name",
    [
        ("machina.tools.base", "machina.core.exceptions", "ToolError"),
        ("machina.tools.base", "machina.core.exceptions", "ValidationError"),
        ("machina.tools.base", "machina.core.models", "ToolDefinition"),
        ("machina.tools.base", "machina.core.provider", "audit_log"),
        ("machina.memory.models", "machina.core.exceptions", "ValidationError"),
        ("machina.memory.identity", "machina.core.provider", "audit_log"),
        ("machina.memory.user_profile", "machina.core.provider", "audit_log"),
        ("machina.rag.loaders", "machina.core.config", "config"),
        ("machina.rag.loaders", "machina.core.exceptions", "ValidationError"),
        ("machina.rag.embedder", "machina.core.config", "config"),
        ("machina.rag.embedder", "machina.core.exceptions", "ProviderError"),
        ("machina.rag.embedder", "machina.core.provider", "audit_log"),
        ("machina.rag.reranker", "machina.core.config", "config"),
        ("machina.rag.reranker", "machina.core.exceptions", "ProviderError"),
        ("machina.rag.reranker", "machina.core.provider", "audit_log"),
    ],
)
def test_seam_resolves_to_same_object(dotted, module, name):
    expected = getattr(importlib.import_module(module), name)
    assert _resolve(dotted, module, name) is expected


def test_models_validation_error_identity():
    import machina.core.exceptions as exc
    import machina.memory.models as models

    assert models._core_symbol("machina.core.exceptions", "ValidationError") is (
        exc.ValidationError
    )
    with pytest.raises(exc.ValidationError):
        models.MemoryEntry(
            id=__import__("uuid").uuid4(),
            session_id=None,
            agent_id="harness",
            content="x",
            category="nope",
        )


async def _await_if(awaitable):
    return await awaitable


@pytest.mark.asyncio
async def test_base_registry_tool_error_identity():
    import machina.core.exceptions as exc
    from machina.tools.base import ToolRegistry

    reg = ToolRegistry()
    with pytest.raises(exc.ToolError):
        await reg.execute("missing_tool_xyz")


def test_loader_validation_error_identity():
    import machina.core.exceptions as exc
    from machina.rag.loaders import FileLoader

    loader = FileLoader(base_dir="/tmp")
    with pytest.raises(exc.ValidationError):
        loader.load("/definitely/not/here-xyz.md")
