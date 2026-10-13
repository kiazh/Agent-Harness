"""Leaf seam — tool registry interfaces (Stream C).

Typing.Protocol interfaces mirroring ``machina.tools.base`` exactly so
core/consumers can depend on this seam instead of importing the
implementation. No redesign: names, parameters, and defaults match
``machina/tools/base.py``; core-coupled types are ``Any`` here to keep
this module importable without ``machina.core``.

Source anchors: machina/tools/base.py: Tool (46-62), ToolRegistry
register (80-115), declare_effects (117-128), get_tool_definitions
(167-182), execute (251-301), list_tools (303-305), get_tool (311-313).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ToolLike(Protocol):
    """Structural mirror of ``Tool`` (base.py:46-62)."""

    name: str
    description: str
    parameters: dict
    func: Callable
    is_async: bool
    effects: tuple[str, ...]


@runtime_checkable
class ToolRegistryLike(Protocol):
    """Structural mirror of ``ToolRegistry`` public seam (base.py).

    Covers what core/consumers need: tool execute + effects declarations
    + definitions for LLM function calling.
    """

    def register(
        self,
        name: str | None = None,
        description: str | None = None,
        parameters: dict | None = None,
        effects: tuple[str, ...] | list[str] | None = None,
    ) -> Callable:
        """Mirror of ToolRegistry.register (base.py:80-93)."""
        ...

    def declare_effects(self, effects: dict[str, tuple[str, ...] | list[str]]) -> None:
        """Mirror of ToolRegistry.declare_effects (base.py:117-123)."""
        ...

    def get_tool_definitions(self) -> list[Any]:
        """Mirror of get_tool_definitions (base.py:167-182).

        Returns ``ToolDefinition`` objects in the implementation; ``Any``
        here to avoid a ``machina.core`` import at this seam.
        """
        ...

    async def execute(self, name: str, /, **kwargs: Any) -> Any:
        """Mirror of ToolRegistry.execute (base.py:251)."""
        ...

    def list_tools(self) -> list[str]:
        """Mirror of list_tools (base.py:303)."""
        ...

    def get_tool_names(self) -> list[str]:
        """Mirror of get_tool_names (base.py:307)."""
        ...

    def get_tool(self, name: str) -> Any:
        """Mirror of get_tool (base.py:311). Returns ``Tool | None``."""
        ...
