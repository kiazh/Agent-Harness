"""Bearer-only httpx2 auth adapter: exactly one yield, never a replay."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

try:
    from httpx2 import Auth as _AuthBase
except Exception:  # httpx2 is an optional connector dependency
    _AuthBase = object  # type: ignore[assignment,misc]


class OAuthBearerAuth(_AuthBase):  # type: ignore[misc]
    """httpx2-compatible auth flow yielding the resource request exactly once."""

    requires_request_body = False
    requires_response_body = False
    sync_auth_flow = None

    def __init__(
        self, coordinator, binding, expected_grant: dict[str, Any], deadline_factory
    ) -> None:
        self._coordinator = coordinator
        self._binding = binding
        self._expected_grant = dict(expected_grant)
        self._deadline_factory = deadline_factory

    async def async_auth_flow(self, request):
        from ah.connectors.models import ConnectionFailed

        parts = urlparse(str(request.url))
        resource_parts = urlparse(self._binding.resource)
        if (parts.scheme, parts.hostname, parts.port) != (
            resource_parts.scheme,
            resource_parts.hostname,
            resource_parts.port,
        ):
            raise ConnectionFailed("MCP transport destination changed")
        deadline = self._deadline_factory()
        try:
            token = await self._coordinator.ensure_token(
                self._binding, expected_grant=self._expected_grant, deadline=deadline
            )
        except Exception as exc:
            from ah.connectors.models import AuthRequired

            raise AuthRequired(str(exc) or "authorization required") from exc
        request.headers["Authorization"] = f"Bearer {token}"
        yield request
        # Intentionally no post-response handling: no retry after 401/403.
