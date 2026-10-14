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


def _facade_imports(path: Path) -> list[str]:
    """Imports (module level or deferred) that pull the provider facade itself.

    Used by the provider package guard: submodules must reach siblings only.
    """
    hits: list[str] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ImportFrom) and child.level == 0:
                if child.module == "machina.core.provider":
                    hits.append("from machina.core.provider import ...")
                elif child.module == "machina.core" and any(
                    alias.name == "provider" for alias in child.names
                ):
                    hits.append("from machina.core import provider")
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    if alias.name == "machina.core.provider":
                        hits.append("import machina.core.provider")
            walk(child)

    walk(_tree(path))
    return hits


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
    # Ratchet history (guard-method census over machina/ only):
    #   658 = measured baseline at guard creation (the old 672 was a ceiling,
    #         not the count; wave-1 landings).
    #   677 = after wave-2 hook/isolation work: +19 total, itemized below.
    #         An earlier revision of this table attributed -3 to session_mode,
    #         but its deferred-import count did not change: the hoist that
    #         entry referred to is agent.py's turn_scope import, kept at
    #         module level (no cycle possible). The itemized deltas already
    #         sum to +19 (6+4+4+2+2+1+1-1+0):
    #         agent.py +6 (S3 wiring hooks, guarded), controller/wiring.py +4
    #         (new, guarded by purity tests), gateway/server.py +4 / usage.py
    #         +2 / orchestrator.py +2 / tools/agents.py +1 (S1+S2 observe-only
    #         hooks), container.py +1 (locator decoupling), repomap/scan.py -1
    #         (cargo-cult deferred import removed), api/app.py -45 balanced by
    #         api/routes/* +45 (split moved, not added).
    #   Wave-3 hotfix added +1 (permissions/broker.py guarded redaction
    #   import); wave-4 removed -1 net (agent.py -3: datetime/uuid/images
    #   hoisted to module level, +1 tool-trace hook; controller/wiring.py +2
    #   trace/evidence helpers). Net since wave-2: -1, so cap stays 677.
    #   Final wave (A5, 6a1131a): -1 (agent.py 31 -> 29 deferred imports after
    #   the turn_flow extraction; new core/turn_flow.py +1 guarded helper
    #   import). Census = 675. N1 (final pass): entry added to make the
    #   procedure table complete; the cap was already honest at 675 <= 677.
    #   Benchmark tool (research/benchmark.py, unified offline benchmark): +5
    #   (the four test-harness modules — replay/retrieval/prefix/architecture —
    #   are imported lazily per section because tests/ is not importable until
    #   sys.path setup and is absent from wheels; plus pytest for
    #   MonkeyPatch. The machina-side imports — version, locomo, db — are at
    #   module level, matching research/workflow_benchmark.py.) Census
    #   675 -> 680, so cap 677 -> 680.
    # Procedure: this number may only move with a delta table like the above
    # in this comment; silent growth fails the suite by design.
    total = sum(_deferred_import_count(p) for p in ROOT.rglob("*.py"))
    assert total <= 680, f"deferred (function-level) imports grew: {total} > 680"


# God-file ceilings. Ratchets down, never up, except with a reviewed delta
# table like the deferred-import ratchet above:
#   core/provider.py 2212 -> 2381 (FM2 wave, audited fixes): response-cache
#     identity isolation + TTL/size bound + defensive copies, stream 429
#     retry parity for direct/anthropic/ollama, explicit mid-stream error
#     frames, Ollama missing-message guard, fallback-chain cache skip,
#     Retry-After HTTP-date, reasoning-effort config warning, runtime
#     rate-limiter reload; offset by removing the dead embed implementations
#     (-26) and a duplicated free-tier clamp (-1). No unreviewed growth may
#     land on top of this.
#   core/provider.py 2381 -> 2554 (E1 pilot wave): config-gated prompt-cache
#     breakpoints — payload-only markers on the system tail + last tool for
#     Anthropic, OpenRouter passthrough only for Anthropic-routed models,
#     direct/ollama no-ops, and OFF-path wire-byte pins; transport logic
#     unchanged.
#   core/provider.py 2554 -> 2576 (audit-remediation wave): SSE terminal-state
#     validation — saw_terminal tracking ([DONE] or non-null finish_reason)
#     across the DirectProvider loop and both OpenRouter parse sites; a stream
#     ending without terminal state now raises ProviderError before any done
#     event. Unreviewed growth beyond this is rejected.
#   core/provider.py 2576 -> core/provider/ package (W5 split): facade + 13
#     focused modules; ``__all__`` byte-identical; +228 lines are per-module
#     import headers and docstrings only (all 41 class/function bodies verified
#     byte-identical to the old module; the five function-level imports moved
#     verbatim, so the deferred-import census is unchanged). Per-file ceilings
#     are the measured split landing values; the package total is pinned
#     separately below.
#   core/provider/anthropic.py 462 -> 562, core/provider/ollama.py 369 -> 378
#     (R06/R08 wave): Anthropic native content-block preservation through
#     response parsing, streaming accumulation, and message assembly (signed
#     thinking/redacted_thinking blocks replay verbatim on tool continuations),
#     plus protocol terminal markers for both stream transports (``message_stop``
#     / ``done: true``) so clean EOF without them raises ProviderError. The
#     shared carrier key lives in core/prompt.py (not pinned here).
GOD_FILE_CEILINGS: tuple[tuple[str, int], ...] = (
    ("core/agent.py", 2057),
    ("core/compression.py", 999),
    ("core/provider/__init__.py", 51),
    ("core/provider/_log.py", 10),
    ("core/provider/reasoning.py", 61),
    ("core/provider/specs.py", 103),
    ("core/provider/audit.py", 147),
    ("core/provider/rate_limit.py", 79),
    ("core/provider/caches.py", 97),
    ("core/provider/transport.py", 382),
    ("core/provider/base.py", 60),
    ("core/provider/openrouter.py", 607),
    ("core/provider/ollama.py", 378),
    ("core/provider/direct.py", 337),
    ("core/provider/anthropic.py", 562),
    ("core/provider/factory.py", 39),
)


@pytest.mark.parametrize("relative_path,ceiling", GOD_FILE_CEILINGS)
def test_god_file_ceilings_only_fall(relative_path, ceiling):
    assert _line_count(ROOT / relative_path) <= ceiling, (
        f"{relative_path} grew past its ceiling of {ceiling} lines"
    )


# Provider package total (W5 split). Ratchets down, never up: keeps the split
# from silently regrowing into one god module or sprawling into new files.
# 2913 = 2804 at split landing + 109 for the reviewed R06/R08 delta above.
_PROVIDER_PACKAGE_TOTAL_CEILING = 2913


def test_provider_package_total_only_falls():
    package = ROOT / "core" / "provider"
    total = sum(_line_count(p) for p in sorted(package.rglob("*.py")))
    assert total <= _PROVIDER_PACKAGE_TOTAL_CEILING, (
        f"core/provider package grew past its ceiling of "
        f"{_PROVIDER_PACKAGE_TOTAL_CEILING} lines: {total}"
    )


def test_provider_submodules_do_not_import_the_facade():
    """Provider submodules import siblings only — never the package facade.

    A submodule importing ``machina.core.provider`` would re-enter the
    partially initialized package and make import order load-bearing (the
    classic package-split cycle).
    """
    bad: dict[str, list[str]] = {}
    for path in sorted((ROOT / "core" / "provider").glob("*.py")):
        if path.name == "__init__.py":
            continue
        hits = _facade_imports(path)
        if hits:
            bad[path.name] = hits
    assert not bad, f"provider submodules must not import the facade: {bad}"
