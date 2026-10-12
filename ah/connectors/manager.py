"""Runtime-owned connector lifecycle (one instance per worker).

Connections are worker-local: a healthy connection here proves nothing
about another worker. Retries cover transient *connection* failures only —
never failed authentication, invalid config, or (later) side-effecting
calls. Disabled connectors are never launched.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Mapping
from typing import Any

from ah.connectors.mcp_client import MCPConnection
from ah.connectors.models import (
    AuthRequired,
    ConfigError,
    ConnectionFailed,
    ConnectionState,
    ConnectorConfig,
)
from ah.connectors.registry import ConnectorRegistry

logger = logging.getLogger(__name__)

_CIRCUIT_FAILURES = 3
_CIRCUIT_OPEN_SECONDS = 60.0


class ConnectorManager:
    """Owns worker-local connections with bounded reconnect policy."""

    def __init__(
        self,
        registry: ConnectorRegistry | None = None,
        client_factory: Callable[[Any], MCPConnection] = MCPConnection,
    ) -> None:
        self.registry = registry or ConnectorRegistry()
        self._factory = client_factory
        self._connections: dict[str, MCPConnection] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._failures: dict[str, list[float]] = {}
        self._open_until: dict[str, float] = {}

    def _lock_for(self, connector_id: str) -> asyncio.Lock:
        lock = self._locks.get(connector_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[connector_id] = lock
        return lock

    def _circuit_open(self, connector_id: str) -> bool:
        return time.monotonic() < self._open_until.get(connector_id, 0.0)

    def _record_failure(self, connector_id: str) -> None:
        now = time.monotonic()
        recent = [t for t in self._failures.get(connector_id, []) if now - t < 300.0]
        recent.append(now)
        self._failures[connector_id] = recent
        if len(recent) >= _CIRCUIT_FAILURES:
            self._open_until[connector_id] = now + _CIRCUIT_OPEN_SECONDS
            logger.warning("connector %s circuit open for %ds", connector_id, _CIRCUIT_OPEN_SECONDS)

    def _record_success(self, connector_id: str) -> None:
        self._failures.pop(connector_id, None)
        self._open_until.pop(connector_id, None)

    async def connect(
        self,
        connector_id: str,
        secrets: Mapping[str, str] | None = None,
        *,
        launch_config: ConnectorConfig | None = None,
    ) -> dict[str, Any]:
        """Connect (idempotent); bounded retries on transient failures."""
        config = self.registry.get(connector_id)
        if config is None:
            raise ConnectionFailed(f"unknown connector: {connector_id}")
        if not config.enabled:
            raise ConnectionFailed("connector is disabled; disabled servers are never launched")
        async with self._lock_for(connector_id):
            existing = self._connections.get(connector_id)
            if (
                existing is not None
                and existing.state == ConnectionState.CONNECTED
                and existing.config.revision == config.revision
                and (launch_config is None or existing.config == launch_config)
            ):
                return self._info(connector_id, existing)
            if self._circuit_open(connector_id):
                raise ConnectionFailed("circuit open after repeated failures; try again later")
            if existing is not None:
                await existing.disconnect()
            connection = self._factory(launch_config or config)
            self._connections[connector_id] = connection
            attempts = 1 + max(0, config.max_retries)
            last_error: Exception | None = None
            for attempt in range(attempts):
                try:
                    await connection.connect(secrets or {})
                    self._connections[connector_id] = connection
                    self._record_success(connector_id)
                    return self._info(connector_id, connection)
                except (ConfigError, AuthRequired) as exc:
                    # Never retried: fix config/credentials instead.
                    await connection.disconnect()
                    connection.state = (
                        ConnectionState.AUTH_REQUIRED
                        if isinstance(exc, AuthRequired)
                        else ConnectionState.ERROR
                    )
                    connection.last_error = _safe_reason(exc, secrets)
                    raise ConnectionFailed(connection.last_error) from exc
                except ConnectionFailed as exc:
                    await connection.disconnect()
                    connection.state = ConnectionState.ERROR
                    connection.last_error = _safe_reason(exc, secrets)
                    last_error = exc
                    if not exc.retryable:
                        break
                    self._record_failure(connector_id)
                    if attempt + 1 >= attempts or self._circuit_open(connector_id):
                        break
                    delay = config.backoff_base * (2**attempt) + random.uniform(
                        0, config.backoff_base
                    )
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    await connection.disconnect()
                    raise
            raise ConnectionFailed(connection.last_error if last_error else "connect failed")

    async def disconnect(self, connector_id: str) -> bool:
        """Disconnect and forget the connection; True when one existed."""
        async with self._lock_for(connector_id):
            connection = self._connections.get(connector_id)
            if connection is None:
                return False
            await connection.disconnect()
            self._connections.pop(connector_id, None)
            return True

    async def shutdown_all(self) -> None:
        """Bounded shutdown of every connection (worker teardown)."""
        for connector_id in list(self._connections):
            try:
                await self.disconnect(connector_id)
            except Exception as exc:
                logger.warning("connector %s shutdown issue: %s", connector_id, type(exc).__name__)

    def connection(self, connector_id: str) -> MCPConnection | None:
        return self._connections.get(connector_id)

    def _info(self, connector_id: str, connection: MCPConnection) -> dict[str, Any]:
        config = self.registry.get(connector_id)
        return {
            "id": connector_id,
            "state": connection.state.value,
            "server_name": connection.server_name,
            "server_version": connection.server_version,
            "protocol_version": connection.protocol_version,
            "error": connection.last_error,
            "enabled": config.enabled if config else False,
        }

    def status(self, connector_id: str) -> dict[str, Any]:
        """Current state without launching anything."""
        config = self.registry.get(connector_id)
        if config is None:
            raise ConnectionFailed(f"unknown connector: {connector_id}")
        connection = self._connections.get(connector_id)
        if connection is None:
            state = (
                ConnectionState.DISABLED.value
                if not config.enabled
                else ConnectionState.DISCONNECTED.value
            )
            return {"id": connector_id, "state": state, "enabled": config.enabled}
        return self._info(connector_id, connection)


def _safe_reason(error: Exception, secrets: Mapping[str, str] | None) -> str:
    reason = str(error)
    for value in sorted(
        (value for value in (secrets or {}).values() if value), key=len, reverse=True
    ):
        reason = reason.replace(value, "[REDACTED]")
    return reason
