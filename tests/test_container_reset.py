"""Container.reset() regression (wave-2 audit major #2).

`Container.reset()` called `SkillRegistry.reset()` while `SkillRegistry` was
imported under `TYPE_CHECKING` only (import-time edge kept off
`machina.skills`), so every reset raised `NameError`. The reset path must
resolve the registry lazily and complete cleanly.
"""

from __future__ import annotations

import asyncio


def test_container_reset_resolves_skill_registry_without_nameerror():
    from machina.core.container import Container

    container = Container.testing()
    # Must not raise NameError (pre-fix) / any exception (post-fix).
    asyncio.run(container.reset())

    # Fresh global singletons were rebound and referenced.
    from machina.skills.registry import skill_registry

    assert container.skill_registry is skill_registry


def test_container_reset_reimportable_with_skills_blocked_from_module_level():
    """The lazy-resolution contract: importing container must not pull skills."""
    import ast
    from pathlib import Path

    source = Path("machina/core/container.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_level = []
    for node in tree.body:  # module level only
        if isinstance(node, ast.ImportFrom) and node.module == "machina.skills.registry":
            module_level.append(node.lineno)
    assert not module_level, f"module-level skills.registry import at {module_level}"
