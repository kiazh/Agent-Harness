"""Plugin registry and failure-isolated hook dispatch."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

HOOKS = frozenset(
    {"pre_agent_run", "post_agent_run", "on_tool_call", "on_tool_result", "on_memory_extract"}
)


class PluginRegistry:
    def __init__(self, timeout_seconds: float | None = None) -> None:
        self._plugins: dict[str, Any] = {}
        try:
            configured = (
                timeout_seconds
                if timeout_seconds is not None
                else float(os.environ.get("AGENT_HARNESS_PLUGIN_TIMEOUT_SECONDS", "5"))
            )
            self.timeout_seconds = max(0.01, configured)
        except ValueError:
            self.timeout_seconds = 5.0

    def register(self, plugin: Any) -> None:
        name = getattr(plugin, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("plugin must have a non-empty name")
        if name in self._plugins:
            raise ValueError(f"plugin {name!r} is already registered")
        self._plugins[name] = plugin

    def unregister(self, name: str) -> None:
        self._plugins.pop(name, None)

    def list(self) -> list[str]:
        return sorted(self._plugins)

    async def dispatch(self, hook: str, *args: Any) -> None:
        if hook not in HOOKS:
            raise ValueError(f"unknown plugin hook {hook!r}")
        for name, plugin in tuple(self._plugins.items()):
            method = getattr(plugin, hook, None)
            if method is None:
                continue
            try:
                if inspect.iscoroutinefunction(method):
                    result = method(*args)
                else:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(method, *args), timeout=self.timeout_seconds
                    )
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=self.timeout_seconds)
            except Exception:
                logger.exception("Plugin %s failed in %s", name, hook)


plugin_registry = PluginRegistry()
