"""S4 structural contracts: API route-table parity.

Route-table parity (S4.1): ``create_app()`` was split from one 1300-line
module into per-resource ``machina.api.routes`` APIRouter modules. This pins
the served ``(path, methods)`` table captured BEFORE the split — every
path+method served before must be served after, legacy ``/*`` aliases
included — plus the auth/middleware/lifespan assembly invariants.
"""

from __future__ import annotations

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
