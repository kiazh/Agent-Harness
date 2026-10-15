"""Shared connector operations (management + bounded discovery).

Native invocation and exact broker authorization live in execution.py.
Configuration is human administration; discovery never grants tool consent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from machina.connectors.manager import ConnectorManager
from machina.connectors.models import (
    ConfigError,
    ConnectionFailed,
    ConnectionState,
    ConnectorToolError,
    DiscoveredTool,
)
from machina.connectors.policy import classify_tool
from machina.connectors.registry import ConnectorRegistry
from machina.connectors.store import ConnectorStore
from machina.observability.diagnostics import record_failure


def refuse_sandbox_stdio(config, mode: str | None) -> None:
    """One authoritative refusal for host stdio launch under sandbox mode.

    Existing connect/invoke paths already reject this combination; the agent
    ``mcp_connect`` management path launches through ``connect_and_discover``,
    so the rule lives here and is enforced for every caller that knows the
    session mode. Host stdio MCP always runs on the host, never in the
    sandbox transport.
    """
    if config.transport == "stdio" and mode == "sandbox":
        raise ConnectorToolError("host stdio MCP is unavailable in sandbox mode")


class ConnectorService:
    """One service object shared by future CLI/TUI/HTTP adapters."""

    def __init__(
        self,
        registry: ConnectorRegistry | None = None,
        manager: ConnectorManager | None = None,
        store: ConnectorStore | None = None,
        oauth_coordinator=None,
    ) -> None:
        if registry is not None and manager is not None and manager.registry is not registry:
            raise ConfigError("service and manager must share one connector inventory")
        self.registry = registry or (
            manager.registry if manager is not None else ConnectorRegistry()
        )
        self.manager = manager or ConnectorManager(self.registry)
        self.store = store
        self._store_generation = -1
        self._configuration_lock = asyncio.Lock()
        self.oauth = oauth_coordinator
        if self.oauth is not None and getattr(self.oauth, "_service", None) is None:
            self.oauth._service = self
        if getattr(self.manager, "_oauth_coordinator", None) is None:
            self.manager._oauth_coordinator = self.oauth

    def auth_identity(self, connector_id: str) -> dict[str, Any] | None:
        """Synchronous immutable cached public auth identity (or None)."""
        coordinator = self.oauth
        if coordinator is None:
            return None
        cached = coordinator.auth_identity(connector_id)
        return dict(cached) if cached is not None else None

    async def refresh(self) -> None:
        """Reload changed durable config and retire stale worker connections.

        The configuration lock is released before cached authorization is
        refreshed. ``oauth.refresh_auth_cache`` may wait on a credential
        refresh lock, and the token-refresh owner re-resolves config through
        ``refresh`` while holding that lock; keeping the configuration lock
        across the credential wait would invert the two locks into a circular
        wait (F06). One order holds throughout: configuration lock, then (only
        outside it) any credential-store wait.
        """
        if self.store is None:
            await self._refresh_auth_cache()
            return
        async with self._configuration_lock:
            await self._refresh_locked()
        await self._refresh_auth_cache()

    async def _refresh_locked(self) -> None:
        generation, configs = self.store.load()
        if generation < self._store_generation:
            raise ConfigError("connector inventory generation moved backwards")
        if generation == self._store_generation:
            return
        desired = {c.id: c for c in configs}
        for current in self.registry.list():
            if desired.get(current.id) != current:
                await self.manager.disconnect(current.id)
                self.registry.remove(current.id)
                if self.oauth is not None:
                    try:
                        await self.oauth.invalidate_connector(
                            current.id,
                            config_revision=current.revision if current.id in desired else None,
                        )
                    except Exception as _boundary_error:
                        # Optional fallback preserves the primary outcome; report no payload.
                        record_failure("connector_service.invalidate_oauth", _boundary_error)
        for config in configs:
            if self.registry.get(config.id) is None:
                stored = self.registry.add(config)
                stored.revision = config.revision
        self._store_generation = generation

    async def _refresh_auth_cache(self) -> None:
        # Pass the already loaded config: resolving it through service.refresh
        # here would recursively acquire the configuration lock. Credentials
        # may change independently of the inventory generation (another worker
        # can log out), so refresh these views even when config is unchanged.
        if self.oauth is not None:
            for config in self.registry.list():
                await self.oauth.refresh_auth_cache(config.id, config=config)

    async def configure(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Human installation administration; never starts a connector."""
        if self.store is None:
            raise ConfigError("durable connector store is unavailable")
        if not isinstance(fields, dict):
            raise ConfigError("config must be an object")
        async with self._configuration_lock:
            result = await self._configure_locked(fields)
        # Credential-cache refresh never runs under the configuration lock (F06).
        try:
            await self._refresh_auth_cache()
        except asyncio.CancelledError:
            from machina.core.background_tasks import CompletedOnCancellation

            raise CompletedOnCancellation(result) from None
        return result

    async def _configure_locked(self, fields: dict[str, Any], *, expected_revision: int | None = None) -> dict[str, Any]:
        """Configure under the caller-held configuration lock.

        Composite management flows (agent mcp_connect add+launch, rollback)
        hold the lock across several of these primitives so no concurrent
        worker can observe or interleave a half-configured server.
        """
        await self._refresh_locked()
        if (
            "transport" not in fields
            and "id" in fields
            and "oauth" in fields
            and set(fields) <= {"id", "oauth"}
        ):
            # Partial OAuth-settings edit: merge over the stored config so
            # existing servers go through the normal revision/retirement
            # path without re-entering transport fields.
            from dataclasses import asdict as _asdict

            existing = self.registry.get(fields["id"])
            if existing is None:
                raise ConfigError("unknown connector for partial update")
            merged = _asdict(existing)
            merged.pop("owner", None)
            merged.pop("revision", None)
            for key, value in fields.items():
                if key == "id":
                    continue
                if key not in ("oauth",):
                    raise ConfigError("partial updates support only oauth settings")
                merged[key] = value
            fields = merged
        config = self.store.validate_input(fields)
        if self.registry.get(config.id) is not None:
            await self.manager.disconnect(config.id)
        from machina.core.background_tasks import CompletedOnCancellation

        written = None
        try:
            written = await self.store.configure_async(fields, expected_revision=expected_revision)
            await self._refresh_locked()
        except CompletedOnCancellation as cancellation:
            raise CompletedOnCancellation(cancellation.result.public_view()) from None
        except asyncio.CancelledError:
            if written is not None:
                raise CompletedOnCancellation(written.public_view()) from None
            raise
        return {**written.public_view(), **_state_only(self.manager.status(config.id))}

    async def remove(self, connector_id: str) -> bool:
        if self.store is None:
            raise ConfigError("durable connector store is unavailable")
        async with self._configuration_lock:
            removed = await self._remove_locked(connector_id)
        # Credential-cache refresh never runs under the configuration lock (F06).
        try:
            await self._refresh_auth_cache()
        except asyncio.CancelledError:
            from machina.core.background_tasks import CompletedOnCancellation

            raise CompletedOnCancellation(removed) from None
        return removed

    async def _remove_locked(self, connector_id: str, *, expected_revision: int | None = None) -> bool:
        """Remove under the caller-held configuration lock (see _configure_locked)."""
        await self._refresh_locked()
        if self.registry.get(connector_id) is not None:
            await self.manager.disconnect(connector_id)
            if self.oauth is not None:
                try:
                    await self.oauth.invalidate_connector(connector_id)
                except Exception as _boundary_error:
                    # Optional fallback preserves the primary outcome; report no payload.
                    record_failure("connector_service.invalidate_oauth", _boundary_error)
        from machina.core.background_tasks import CompletedOnCancellation

        removed = None
        try:
            removed = await self.store.remove_async(connector_id, expected_revision=expected_revision)
            await self._refresh_locked()
        except asyncio.CancelledError:
            if removed is not None:
                raise CompletedOnCancellation(removed) from None
            raise
        return removed

    async def list(self) -> list[dict[str, Any]]:
        """Sanitized inventory with live state; launches nothing."""
        await self.refresh()
        return [
            {**config.public_view(), **_state_only(self.manager.status(config.id))}
            for config in self.registry.list()
        ]

    async def status(self, connector_id: str) -> dict[str, Any]:
        """State of one connector without launching anything."""
        await self.refresh()
        config = self.registry.get(connector_id)
        if config is None:
            raise ConnectionFailed(f"unknown connector: {connector_id}")
        return {**config.public_view(), **_state_only(self.manager.status(connector_id))}

    async def connect(
        self, connector_id: str, secrets: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        """Connect with bounded retries; disabled servers are refused."""
        await self.refresh()
        return await self.manager.connect(connector_id, secrets)

    async def connect_and_discover(
        self, connector_id: str, *, launch_config=None, mode: str | None = None
    ) -> list[DiscoveredTool]:
        """Launch a configured connector and install its discovered catalog.

        The low-level primitive shared by the human-reviewed management route
        (``connect_authorized``) and the agent ``mcp_connect`` tool: approval
        happens before this call, and composite flows hold the configuration
        lock across it. ``mode`` is the approved session mode; host stdio is
        refused under sandbox before any launch. ``secret_refs`` values resolve
        at launch; a discovery failure retires the connection so a failed
        connect never leaves a half-live server. Never retried here — the
        manager owns bounded connection retries.
        """
        from machina.security.secrets import get_secret

        config = self._config(connector_id)
        refuse_sandbox_stdio(config, mode)
        secrets = {name: get_secret(name) or "" for name in config.secret_refs}
        await self.manager.connect(connector_id, secrets, launch_config=launch_config)
        connection = self._live_connection(connector_id)
        try:
            tools, _ = await connection.list_tools()
            return self.registry.set_tools(connector_id, tools)
        except BaseException:
            await self.manager.disconnect(connector_id)
            raise

    async def disconnect(self, connector_id: str) -> dict[str, Any]:
        """Disconnect; reports whether a connection existed."""
        await self.refresh()
        self._config(connector_id)
        return {"id": connector_id, "disconnected": await self.manager.disconnect(connector_id)}

    async def tools_list(self, connector_id: str) -> dict[str, Any]:
        """Bounded catalog (names + descriptions); requires a live connection.

        Schemas stay server-side until tools_describe: discovery must not
        dump every schema into every consumer.
        """
        await self.refresh()
        connection = self._live_connection(connector_id)
        tools, truncated = await connection.list_tools()
        stored = self.manager.registry.set_tools(connector_id, tools)
        return {
            "tools": [
                {
                    "name": tool.name,
                    "remote_name": tool.remote_name,
                    "description": tool.description,
                    "verdict": classify_tool(self._config(connector_id), tool),
                }
                for tool in stored
            ],
            "truncated": bool(truncated),
        }

    async def tools_describe(self, name: str) -> dict[str, Any]:
        """Full schema + policy verdict for one namespaced tool."""
        await self.refresh()
        tool = self.manager.registry.lookup(name)
        if tool is None:
            raise ConnectorToolError(f"unknown tool (discover first): {name}")
        self._live_connection(tool.connector_id)
        return {
            "name": tool.name,
            "remote_name": tool.remote_name,
            "description": tool.description,
            "schema": tool.schema,
            "schema_revision": tool.schema_revision,
            "verdict": classify_tool(self._config(tool.connector_id), tool),
        }

    def _config(self, connector_id: str):
        config = self.registry.get(connector_id)
        if config is None:
            raise ConnectionFailed(f"unknown connector: {connector_id}")
        return config

    def _live_connection(self, connector_id: str):
        self._config(connector_id)
        connection = self.manager.connection(connector_id)
        if connection is None or connection.state != ConnectionState.CONNECTED:
            raise ConnectionFailed("not connected; connect first (listing never auto-launches)")
        return connection


def _state_only(status: dict[str, Any]) -> dict[str, Any]:
    return {
        key: status.get(key)
        for key in ("state", "server_name", "server_version", "protocol_version", "error")
    }
