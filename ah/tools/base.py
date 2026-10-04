"""Tool registry — decorator-based with JSON Schema inference."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_type_hints

from ah.core.exceptions import ToolError, ValidationError
from ah.core.models import ToolDefinition
from ah.core.provider import audit_log

logger = logging.getLogger(__name__)

# Tools that mutate state: never cached, and flush the result cache when run.
_SIDE_EFFECT_PREFIXES = ("write_", "delete_", "create_", "update_", "send_", "post_")
_SIDE_EFFECT_TOOLS = frozenset(
    {"terminal", "remember", "index_document", "delegate", "share_memory"}
)
# Read-only but non-deterministic / state-dependent: never cached.
_UNCACHEABLE_TOOLS = frozenset(
    {
        "recall",
        "search_documents",
        "list_agents",
        "session_recall",
        "session_recall_window",
        "skill_list",
        "skill_read",
    }
)


@dataclass
class Tool:
    """A registered tool."""

    name: str
    description: str
    parameters: dict  # JSON Schema
    func: Callable
    is_async: bool = False


class ToolRegistry:
    """Global tool registry — register tools with decorator.

    Includes a TTL cache for tool execution results to avoid redundant
    calls with identical arguments.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._definitions_cache: list[ToolDefinition] | None = None
        # TTL cache: (tool_name, args_hash) -> (timestamp, result)
        self._result_cache: dict[tuple[str, str], tuple[float, Any]] = {}
        self._cache_ttl: float = 60.0  # 1 minute default
        self._cache_max_size: int = 256

    def register(
        self,
        name: str | None = None,
        description: str | None = None,
        parameters: dict | None = None,
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
        # ``from __future__ import annotations`` makes every annotation a string
        # at runtime, so ``param.annotation`` is e.g. ``"int"`` and never matches
        # ``type_map``. Resolve the real runtime types with get_type_hints().
        try:
            hints = get_type_hints(func)
        except Exception:
            hints = {}
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            annotation = hints.get(param_name, param.annotation)
            param_type = type_map.get(annotation, "string")
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
        """Execute a tool by name with input validation and TTL caching."""
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

        # TTL cache: only for read-only, deterministic tools. Side-effecting tools
        # are never cached and flush the cache (so read_file after write_file,
        # or repeated `git status`, never returns stale results).
        is_side_effect = name.startswith(_SIDE_EFFECT_PREFIXES) or name in _SIDE_EFFECT_TOOLS
        cache_key = None
        if not is_side_effect and name not in _UNCACHEABLE_TOOLS:
            args_hash = hashlib.sha256(
                json.dumps(kwargs, sort_keys=True, default=str).encode()
            ).hexdigest()
            cache_key = (name, args_hash)
            now = time.monotonic()
            if cache_key in self._result_cache:
                cached_time, cached_result = self._result_cache[cache_key]
                if now - cached_time < self._cache_ttl:
                    logger.debug("Tool result cache hit for %s", name)
                    return cached_result
                else:
                    del self._result_cache[cache_key]

        # Execute
        if tool.is_async:
            result = await tool.func(**kwargs)
        else:
            result = tool.func(**kwargs)

        if is_side_effect:
            self._result_cache.clear()

        # Store in cache
        if cache_key is not None:
            self._result_cache[cache_key] = (time.monotonic(), result)
            if len(self._result_cache) > self._cache_max_size:
                oldest_key = min(self._result_cache, key=lambda k: self._result_cache[k][0])
                del self._result_cache[oldest_key]

        return result

    def list_tools(self) -> list[str]:
        """List all registered tool names."""
        return list(self._tools.keys())

    def get_tool_names(self) -> list[str]:
        """Alias for list_tools."""
        return self.list_tools()

    def get_tool(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    @classmethod
    def reset(cls) -> None:
        """Reset the global ToolRegistry singleton to a fresh instance."""
        global registry
        registry = cls()


# Global registry
registry = ToolRegistry()
