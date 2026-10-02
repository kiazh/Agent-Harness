"""Tool registry — decorator-based with JSON Schema inference."""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ah.core.exceptions import ToolError, ValidationError
from ah.core.models import ToolDefinition
from ah.core.provider import audit_log

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    """A registered tool."""

    name: str
    description: str
    parameters: dict  # JSON Schema
    func: Callable
    is_async: bool = False


class ToolRegistry:
    """Global tool registry — register tools with decorator."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._definitions_cache: list[ToolDefinition] | None = None

    def register(
        self,
        name: Optional[str] = None,
        description: Optional[str] = None,
        parameters: Optional[dict] = None,
    ) -> Callable:
        """Decorator to register a function as a tool.

        Usage:
            @registry.register(description="Read a file")
            def read_file(path: str, offset: int = 1, limit: int = 2000) -> str:
                ...
        """
        def decorator(func: Callable) -> Callable:
            tool_name = name or func.__name__
            tool_desc = description or (func.__doc__ or "").strip().split("\n")[0]
            tool_params = parameters or self._infer_schema(func)
            is_async = inspect.iscoroutinefunction(func)
            self._tools[tool_name] = Tool(
                name=tool_name,
                description=tool_desc,
                parameters=tool_params,
                func=func,
                is_async=is_async,
            )
            # Invalidate definitions cache when a new tool is registered
            self._definitions_cache = None
            return func
        return decorator

    def _infer_schema(self, func: Callable) -> dict:
        """Infer JSON Schema from function signature."""
        sig = inspect.signature(func)
        type_map = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object",
        }
        properties = {}
        required = []
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            param_type = type_map.get(param.annotation, "string")
            properties[param_name] = {
                "type": param_type,
                "description": param_name,
            }
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    def get_tool_definitions(self) -> list[ToolDefinition]:
        """Get all tool definitions for LLM function calling (cached).

        Tool definitions are immutable during a run, so we cache the result
        after the first call to avoid rebuilding the list on every LLM request.
        """
        if self._definitions_cache is None:
            self._definitions_cache = [
                ToolDefinition(
                    name=t.name,
                    description=t.description,
                    parameters=t.parameters,
                )
                for t in self._tools.values()
            ]
        return self._definitions_cache

    def _validate_tool_args(self, tool: Tool, kwargs: dict[str, Any]) -> None:
        """Validate tool arguments against the tool's JSON Schema.

        Checks:
        - Required parameters are present
        - Parameter types match the schema
        - No extra parameters are passed (unless additionalProperties is true)
        """
        schema = tool.parameters
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        # Check required parameters
        for req_param in required:
            if req_param not in kwargs:
                raise ValidationError(
                                    f"Tool '{tool.name}' missing required parameter: '{req_param}'"
                                )

        # Check for unknown parameters
        allowed_params = set(properties.keys())
        provided_params = set(kwargs.keys())
        unknown_params = provided_params - allowed_params
        if unknown_params:
            raise ValidationError(
                            f"Tool '{tool.name}' received unknown parameters: {unknown_params}. "
                            f"Allowed: {allowed_params}"
                        )

        # Type checking
        type_map = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        for param_name, param_value in kwargs.items():
            if param_name not in properties:
                continue
            param_schema = properties[param_name]
            expected_type = param_schema.get("type")
            if expected_type and expected_type in type_map:
                python_type = type_map[expected_type]
                if not isinstance(param_value, python_type):
                    raise ValidationError(
                                            f"Tool '{tool.name}' parameter '{param_name}' expected type "
                                            f"'{expected_type}', got '{type(param_value).__name__}'"
                                        )

    async def execute(self, name: str, **kwargs) -> Any:
        """Execute a tool by name with input validation."""
        if name not in self._tools:
            audit_log("tool_execution_error", tool_name=name, error="not_registered")
            raise ToolError(f"Tool '{name}' not registered")

        tool = self._tools[name]

        # Input validation
        try:
            self._validate_tool_args(tool, kwargs)
        except ValueError as e:
            audit_log("tool_execution_validation_error", tool_name=name, error=str(e))
            raise

        # Execute
        if tool.is_async:
            return await tool.func(**kwargs)
        else:
            return tool.func(**kwargs)

    def list_tools(self) -> list[str]:
        """List all registered tool names."""
        return list(self._tools.keys())

    def get_tool_names(self) -> list[str]:
        """Alias for list_tools."""
        return self.list_tools()

    def get_tool(self, name: str) -> Optional[Tool]:
        """Get a tool by name."""
        return self._tools.get(name)

    @classmethod
    def reset(cls) -> None:
        """Reset the global ToolRegistry singleton to a fresh instance."""
        global registry
        registry = cls()


# Global registry
registry = ToolRegistry()
