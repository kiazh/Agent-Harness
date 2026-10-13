"""Architecture layer guard (Stream A, A1).

Pins dependency direction between layers so new coupling cannot sneak in
unnoticed. Methodology (AST, stdlib only):

* "Module level" = an ``import`` / ``from`` statement with no function- or
  class-body ancestor (top-level ``if``/``try`` wrappers count: they execute
  at import time). Only absolute (``level == 0``) ``machina.*`` references
  are considered; intra-package relative imports are ignored.
* "Deferred" = an import statement with a function-body ancestor.
* Import counts are per statement (``from x import a, b`` = 1); line
  ceilings use ``wc -l`` semantics.

DEVIATION (evidence-backed): the literal "leaves import nothing from
``machina.core``" does not hold on the current tree — tools (21 stmts),
memory (14), permissions (1: shell.py), skills (4), rag (15), and soulspec
(4) all import ``machina.core.*`` at module level today, and ``machina.core``
itself imports leaves (agent.py, container.py) at module level. Enforcing
zero would fail on the current tree, so those packages are pinned as
ratchets instead: the per-package statement counts below may only shrink.
Genuinely clean boundaries keep literal assertions: connectors has zero
module-level ``machina.core`` imports, controller is limited to core+skills,
and repomap imports nothing from ``machina``.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "machina"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _module_machina_imports(path: Path) -> list[str]:
    """Absolute machina.* modules imported at module level (see methodology)."""
    found: list[str] = []

    def walk(node: ast.AST, nested: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_nested = nested or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            if isinstance(child, ast.Import):
                if not nested:
                    for alias in child.names:
                        if alias.name == "machina" or alias.name.startswith("machina."):
                            found.append(alias.name)
            elif isinstance(child, ast.ImportFrom):
                if not nested and child.level == 0 and child.module:
                    if child.module == "machina":
                        found.extend(f"machina.{a.name}" for a in child.names)
                    elif child.module == "machina" or child.module.startswith("machina."):
                        found.append(child.module)
            walk(child, child_nested)

    walk(_tree(path), False)
    return found


def _is_core(mod: str) -> bool:
    return mod == "machina.core" or mod.startswith("machina.core.")


def _in_packages(mod: str, packages: tuple[str, ...]) -> bool:
    return any(mod == pkg or mod.startswith(pkg + ".") for pkg in packages)


def _deferred_import_count(path: Path) -> int:
    count = 0

    def walk(node: ast.AST, in_func: bool) -> None:
        nonlocal count
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                if in_func:
                    count += 1
            walk(child, in_func or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))

    walk(_tree(path), False)
    return count


def _line_count(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def test_controller_module_imports_limited_to_core_and_skills():
    allowed = ("machina.core", "machina.skills", "machina.controller")
    bad: dict[str, list[str]] = {}
    for path in sorted((ROOT / "controller").rglob("*.py")):
        violations = [m for m in _module_machina_imports(path) if not _in_packages(m, allowed)]
        if violations:
            bad[str(path.relative_to(ROOT))] = violations
    assert not bad, f"controller imports outside core+skills at module level: {bad}"


def test_repomap_has_no_module_level_machina_imports():
    bad: dict[str, list[str]] = {}
    for path in sorted((ROOT / "repomap").rglob("*.py")):
        imports = [
            m for m in _module_machina_imports(path) if not _in_packages(m, ("machina.repomap",))
        ]
        if imports:
            bad[str(path.relative_to(ROOT))] = imports
    assert not bad, f"repomap imports machina at module level: {bad}"


# Current census of module-level machina.core import statements per leaf
# package (see DEVIATION above). Ratchets down, never up.
_CORE_IMPORT_CEILINGS = {
    "tools": 21,
    "memory": 14,
    "permissions": 1,
    "skills": 4,
    "rag": 15,
    "connectors": 0,
    "soulspec": 4,
}


def test_module_level_core_imports_only_shrink():
    over: dict[str, str] = {}
    for package, ceiling in _CORE_IMPORT_CEILINGS.items():
        total = 0
        for path in sorted((ROOT / package).rglob("*.py")):
            total += sum(1 for mod in _module_machina_imports(path) if _is_core(mod))
        if total > ceiling:
            over[package] = f"{total} > {ceiling}"
    assert not over, f"new module-level machina.core imports: {over}"


def test_deferred_import_count_ratchet():
    total = sum(_deferred_import_count(p) for p in ROOT.rglob("*.py"))
    assert total <= 672, f"deferred (function-level) imports grew: {total} > 672"


@pytest.mark.parametrize(
    "relative_path,ceiling",
    [
        ("core/agent.py", 2057),
        ("core/provider.py", 2212),
        ("core/compression.py", 999),
    ],
)
def test_god_file_ceilings_only_fall(relative_path, ceiling):
    assert _line_count(ROOT / relative_path) <= ceiling, (
        f"{relative_path} grew past its ceiling of {ceiling} lines"
    )
