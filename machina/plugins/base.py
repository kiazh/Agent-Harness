"""Lifecycle hooks implemented by plugins."""

from __future__ import annotations

from typing import Any


class Plugin:
    """Subclass and override only the hooks the plugin needs."""

    name: str = ""

    async def pre_agent_run(self, session: Any, message: str) -> None:
        pass

    async def post_agent_run(self, session: Any, response: Any) -> None:
        pass

    async def on_tool_call(self, tool_name: str, args: dict[str, Any]) -> None:
        pass

    async def on_tool_result(self, tool_name: str, result: str) -> None:
        pass

    async def on_memory_extract(self, memories: list[Any]) -> None:
        pass
