"""Container.reset() regression (wave-2 audit major #2).

`Container.reset()` called `SkillRegistry.reset()` while `SkillRegistry` was
imported under `TYPE_CHECKING` only (import-time edge kept off
`machina.skills`), so every reset raised `NameError`. The reset path must
resolve the registry lazily and complete cleanly.

Hermetic contract (suite-order fix): reset() deliberately REBINDS the
module-level singletons to fresh instances. Modules that captured the old
references at import (notably ``gateway/server.py``) keep using the retired
instance, so leaving the rebind in place pollutes every later test that
patches the (new) module global. Snapshot-and-restore via ``monkeypatch``
keeps the reset fully tested while preventing cross-test leakage.
"""

from __future__ import annotations

import asyncio


def _pin_singleton_globals(monkeypatch) -> None:
    """Snapshot every singleton a reset() rebinds; monkeypatch restores them.

    ``monkeypatch.setattr(mod, name, getattr(mod, name))`` records the current
    value and restores it at teardown — even though reset() replaces it later.
    """
    import machina.core.context as context_mod
    import machina.core.session as session_mod
    import machina.core.usage as usage_mod
    import machina.db.connection as db_mod
    import machina.skills.registry as skills_mod
    import machina.tools.base as tools_mod

    for mod, name in (
        (session_mod, "session_manager"),
        (context_mod, "context_manager"),
        (usage_mod, "usage_store"),
        (db_mod, "db"),
        (tools_mod, "registry"),
        (skills_mod, "skill_registry"),
    ):
        monkeypatch.setattr(mod, name, getattr(mod, name))


def test_container_reset_resolves_skill_registry_without_nameerror(monkeypatch):
    _pin_singleton_globals(monkeypatch)
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
