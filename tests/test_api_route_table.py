"""S4 structural contracts: API route-table parity + container locator edge.

Route-table parity (S4.1): ``create_app()`` was split from one 1300-line
module into per-resource ``machina.api.routes`` APIRouter modules. This pins
the served ``(path, methods)`` table captured BEFORE the split — every
path+method served before must be served after, legacy ``/*`` aliases
included — plus the auth/middleware/lifespan assembly invariants.

Container locator edge (S4.2): ``machina.core.container`` must not import
``machina.skills.registry`` at module level (lazy access only).
"""

from __future__ import annotations

import ast
import importlib
import importlib.abc
import sys
from pathlib import Path

from fastapi.routing import APIRoute

from machina.api.app import create_app

# Pinned route table captured from create_app() BEFORE the S4.1 split
# (path, sorted-methods tuples; HEAD/OPTIONS excluded).
EXPECTED_ROUTES = frozenset(
    {
        ("/agents", ("GET",)),
        ("/api/v1/approvals", ("GET",)),
        ("/api/v1/approvals/{request_id}/resolve", ("POST",)),
        ("/api/v1/approvals/{request_id}/resume", ("POST",)),
        ("/api/v1/documents", ("POST",)),
        ("/api/v1/documents/search", ("GET",)),
        ("/api/v1/jobs/{job_id}", ("DELETE",)),
        ("/api/v1/memory", ("POST",)),
        ("/api/v1/memory/search", ("GET",)),
        ("/api/v1/persona-memory", ("POST",)),
        ("/api/v1/sessions", ("GET",)),
        ("/api/v1/sessions", ("POST",)),
        ("/api/v1/sessions/{session_id}", ("DELETE",)),
        ("/api/v1/sessions/{session_id}", ("GET",)),
        ("/api/v1/sessions/{session_id}", ("PATCH",)),
        ("/api/v1/sessions/{session_id}/chat", ("POST",)),
        ("/api/v1/sessions/{session_id}/context", ("GET",)),
        ("/api/v1/sessions/{session_id}/jobs", ("GET",)),
        ("/api/v1/sessions/{session_id}/jobs", ("POST",)),
        ("/api/v1/sessions/{session_id}/prompt", ("POST",)),
        ("/api/v1/sessions/{session_id}/usage", ("GET",)),
        ("/health", ("GET",)),
        ("/metrics", ("GET",)),
        ("/ready", ("GET",)),
        ("/rpc", ("POST",)),
        ("/sessions", ("GET",)),
        ("/sessions", ("POST",)),
        ("/sessions/{session_id}", ("GET",)),
        ("/sessions/{session_id}/context", ("GET",)),
        ("/sessions/{session_id}/jobs", ("GET",)),
        ("/sessions/{session_id}/jobs", ("POST",)),
        ("/sessions/{session_id}/memory", ("GET",)),
        ("/sessions/{session_id}/prompt", ("POST",)),
    }
)

# Legacy (unversioned) aliases: preserved, never removed without the
# deprecation cycle documented in machina.api.routes.
LEGACY_ALIASES = frozenset(
    {
        ("/agents", ("GET",)),
        ("/rpc", ("POST",)),
        ("/sessions", ("GET",)),
        ("/sessions", ("POST",)),
        ("/sessions/{session_id}", ("GET",)),
        ("/sessions/{session_id}/context", ("GET",)),
        ("/sessions/{session_id}/jobs", ("GET",)),
        ("/sessions/{session_id}/jobs", ("POST",)),
        ("/sessions/{session_id}/memory", ("GET",)),
        ("/sessions/{session_id}/prompt", ("POST",)),
    }
)


def _route_table(app) -> frozenset:
    return frozenset(
        (
            route.path,
            tuple(sorted(m for m in (route.methods or []) if m not in ("HEAD", "OPTIONS"))),
        )
        for route in app.routes
        if isinstance(route, APIRoute)
    )


class TestApiRouteTable:
    def test_route_table_matches_pinned(self):
        assert _route_table(create_app()) == EXPECTED_ROUTES

    def test_legacy_aliases_preserved(self):
        assert LEGACY_ALIASES <= _route_table(create_app())

    def test_auth_dependencies_preserved(self):
        """Every route except /health and /ready keeps its auth dependency."""
        app = create_app()
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            if route.path in ("/health", "/ready"):
                assert not route.dependencies, route.path
            else:
                assert route.dependencies, f"{route.path} lost its auth dependency"

    def test_rate_limit_middleware_still_applied(self):
        from machina.api.rate_limit import RateLimitMiddleware

        app = create_app()
        assert any(
            getattr(entry, "cls", None) is RateLimitMiddleware for entry in app.user_middleware
        )

    def test_lifespan_preserved(self):
        from machina.api.app import lifespan

        assert create_app().router.lifespan_context is lifespan


class _BlockSkillsRegistry(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "machina.skills.registry":
            raise ImportError("S4.2: machina.skills.registry is blocked")
        return None


class TestContainerLocatorEdge:
    def test_no_module_level_skills_registry_import(self):
        """AST: container.py has no import-time edge to skills.registry."""
        path = Path(__file__).resolve().parents[1] / "machina" / "core" / "container.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def _is_type_checking(node: ast.AST) -> bool:
            # `if TYPE_CHECKING:` blocks never execute at runtime; the
            # dynamic test below proves the runtime edge is gone.
            return (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Name)
                and node.test.id == "TYPE_CHECKING"
            )

        def walk(node, nested):
            for child in ast.iter_child_nodes(node):
                if _is_type_checking(child):
                    continue
                child_nested = nested or isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
                if not nested:
                    if isinstance(child, ast.Import):
                        for alias in child.names:
                            assert alias.name != "machina.skills.registry", (
                                "container.py regained a module-level skills.registry import"
                            )
                    elif isinstance(child, ast.ImportFrom):
                        assert not (
                            child.level == 0 and child.module == "machina.skills.registry"
                        ), "container.py regained a module-level skills.registry import"
                walk(child, child_nested)

        walk(tree, False)

    def test_container_imports_with_skills_registry_blocked(self):
        """Dynamic: container re-imports while skills.registry is blocked.

        Warms the transitive graph first (tools.repo legitimately binds the
        registry at ITS import time — out of S4 scope), then evicts only
        ``machina.core.container`` + ``machina.skills.registry`` and blocks
        the latter. Pre-fix this raises ImportError on container's own
        module-level import; post-fix the re-import succeeds and the
        registry stays out of ``sys.modules`` (lazy access only).
        """
        import machina.core.container  # noqa: F401 — warm
        import machina.skills.registry  # noqa: F401 — warm transitive edge
        import machina.tools.base  # noqa: F401 — warm transitive edge

        saved_modules = dict(sys.modules)
        saved_meta = list(sys.meta_path)
        sys.meta_path.insert(0, _BlockSkillsRegistry())
        try:
            for key in ("machina.core.container", "machina.skills.registry"):
                sys.modules.pop(key, None)
            assert "machina.skills.registry" not in sys.modules
            importlib.import_module("machina.core.container")
            assert "machina.skills.registry" not in sys.modules
        finally:
            sys.modules.clear()
            sys.modules.update(saved_modules)
            sys.meta_path[:] = saved_meta

    def test_singleton_wiring_order_preserved(self):
        """production() still wires the global singleton; testing() is fresh."""
        from machina.core.container import Container
        from machina.skills.registry import SkillRegistry, skill_registry

        assert Container.production().skill_registry is skill_registry
        fresh = Container.testing().skill_registry
        assert isinstance(fresh, SkillRegistry)
        assert fresh is not skill_registry
