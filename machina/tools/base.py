"""Tool registry — decorator-based with JSON Schema inference."""

from __future__ import annotations

import hashlib
import importlib as _importlib
import inspect
import json
import logging
import sys as _sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, get_type_hints

if TYPE_CHECKING:
    from machina.core.models import ToolDefinition


# SEAM: leaf-core seam for machina.core (exceptions ToolError/ValidationError,
# models ToolDefinition, provider audit_log). Deferred via sys.modules/importlib
# so this module imports with machina.core blocked; resolves to the same objects
# when core is present. No logic change.
def _core_symbol(module: str, name: str):
    mod = _sys.modules.get(module)
    if mod is None:
        mod = _importlib.import_module(module)
    return getattr(mod, name)


logger = logging.getLogger(__name__)

# Legacy name heuristics. These are a fallback ONLY for tools that declare no
# effects; declared effects are authoritative for cache eligibility.
_SIDE_EFFECT_PREFIXES = ("write_", "delete_", "create_", "update_", "send_", "post_")
_SIDE_EFFECT_TOOLS = frozenset(
    {
        "terminal",
        "remember",
        "index_document",
        "web_research",
        "web_crawl",
        "delegate",
        "share_memory",
    }
)
# Read-only but non-deterministic / state-dependent: never cached.
_UNCACHEABLE_TOOLS = frozenset(
    {
        "mcp_list",
        "mcp_describe",
        "mcp_call",
        "recall",
        "harness_status",
        "learn_directory",
        "search_documents",
        "web_research",
        "web_crawl",
        "list_agents",
        "session_recall",
        "session_recall_window",
        "skill_list",
        "skill_read",
        "skill_resource",
    }
)
# Read-type effects. Declared reads are never cached: a read result has no
# freshness contract and a stale hit can cross workspace/session/backend
# boundaries (audit 2026-10-13: the cache returned workspace A's file while
# executing in workspace B). Any other declared effect (exec, fs.write, net,
# memory, delegate, rag.write, or an unknown extensible effect) is treated as
# mutating: never cached, and its run flushes the cache.
_READ_EFFECTS = frozenset({"fs.read", "rag.read", "skill.read", "context.read"})


def _cache_policy(tool: Tool) -> tuple[bool, bool]:
    """Return ``(cacheable, flushes_cache)`` for *tool* from its effects.

    Declared effects win over name heuristics; anything that is not a
    recognized read capability is treated as mutating/exec (fail closed for
    unknown, extensible effects).
    """
    effects = frozenset(tool.effects or ())
    if effects:
        return False, not effects <= _READ_EFFECTS
    side_effect_name = (
        tool.name.startswith(_SIDE_EFFECT_PREFIXES) or tool.name in _SIDE_EFFECT_TOOLS
    )
    cacheable = not side_effect_name and tool.name not in _UNCACHEABLE_TOOLS
    return cacheable, side_effect_name


@dataclass
class Tool:
    """A registered tool.

    ``effects`` declares side-effect capabilities for the permission broker
    (e.g. ``fs.read``, ``fs.write``, ``exec``, ``net``, ``memory``,
    ``delegate``). It is broker metadata only — never sent to the model
    (see get_tool_definitions). Tools without mutating effects stay
    in-process work; extensible side-effecting tools must declare effects or
    they fall through to approval as unknown mutating operations.
    """

    name: str
    description: str
    parameters: dict  # JSON Schema
    func: Callable
    is_async: bool = False
    effects: tuple[str, ...] = ()


class ToolRegistry:
    """Global tool registry — register tools with decorator.

    Includes a TTL cache for tool execution results to avoid redundant
    calls with identical arguments.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._definitions_cache: list[ToolDefinition] | None = None
        # TTL cache keyed by tool name, argument hash, and the effective
        # execution context (workspace, session, backend) so results can never
        # cross those boundaries. See _cache_scope / _cache_policy.
        self._result_cache: dict[tuple[str, str, str, str, str], tuple[float, Any]] = {}
        self._cache_ttl: float = 60.0  # 1 minute default
        self._cache_max_size: int = 256

    def register(
        self,
        name: str | None = None,
        description: str | None = None,
        parameters: dict | None = None,
        effects: tuple[str, ...] | list[str] | None = None,
    ) -> Callable:
        """Decorator to register a function as a tool.

        Usage:
            @registry.register(description="Read a file")
            def read_file(path: str, offset: int = 1, limit: int = 2000) -> str:
                ...
        """
        declared = tuple(effects or ())

        def decorator(func: Callable) -> Callable:
            tool_name = name or func.__name__
            if tool_name == "__no_tools__":
                raise ValueError("__no_tools__ is reserved for an empty tool allowlist")
            tool_desc = description or (func.__doc__ or "").strip().split("\n")[0]
            tool_params = parameters or self._infer_schema(func)
            is_async = inspect.iscoroutinefunction(func)
            self._tools[tool_name] = Tool(
                name=tool_name,
                description=tool_desc,
                parameters=tool_params,
                func=func,
                is_async=is_async,
                effects=declared,
            )
            # Invalidate definitions cache when a new tool is registered
            self._definitions_cache = None
            return func

        return decorator

    def declare_effects(self, effects: dict[str, tuple[str, ...] | list[str]]) -> None:
        """Attach broker effect metadata to already-registered tools.

        Explicit declarations live next to each tool module (LP additional
        check); unknown side-effecting tools without declarations fall
        through to approval instead of silent in-process execution.
        """
        for name, eff in effects.items():
            tool = self._tools.get(name)
            if tool is not None:
                tool.effects = tuple(eff)
        self._definitions_cache = None

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
                _core_symbol("machina.core.models", "ToolDefinition")(
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
                raise _core_symbol("machina.core.exceptions", "ValidationError")(
                    f"Tool '{tool.name}' missing required parameter: '{req_param}'"
                )

        # Check for unknown parameters
        allowed_params = set(properties.keys())
        provided_params = set(kwargs.keys())
        unknown_params = provided_params - allowed_params
        if unknown_params:
            raise _core_symbol("machina.core.exceptions", "ValidationError")(
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
        # Maximum accepted string argument length — bounds memory / prompt
        # injection blast radius for a single tool call.
        _MAX_STRING_ARG_CHARS = 20_000

        for param_name, param_value in kwargs.items():
            if param_name not in properties:
                continue
            param_schema = properties[param_name]
            expected_type = param_schema.get("type")
            if expected_type and expected_type in type_map:
                # bool is a subclass of int: reject it explicitly where a
                # real integer/number is expected.
                if expected_type in ("integer", "number") and isinstance(param_value, bool):
                    raise _core_symbol("machina.core.exceptions", "ValidationError")(
                        f"Tool '{tool.name}' parameter '{param_name}' expected type "
                        f"'{expected_type}', got 'bool'"
                    )
                python_type = type_map[expected_type]
                if not isinstance(param_value, python_type):
                    raise _core_symbol("machina.core.exceptions", "ValidationError")(
                        f"Tool '{tool.name}' parameter '{param_name}' expected type "
                        f"'{expected_type}', got '{type(param_value).__name__}'"
                    )
            if isinstance(param_value, str) and len(param_value) > _MAX_STRING_ARG_CHARS:
                raise _core_symbol("machina.core.exceptions", "ValidationError")(
                    f"Tool '{tool.name}' parameter '{param_name}' is too long "
                    f"(max {_MAX_STRING_ARG_CHARS} characters)"
                )

    def _cache_scope(self) -> tuple[str, str, str] | None:
        """Effective (workspace, session, backend) identity for cache entries.

        Returns ``None`` when the boundary cannot be resolved: the call still
        executes, it is just never read from or written to the cache (fail
        closed).
        """
        try:
            from machina.core.execution_context import execution_context

            ctx = execution_context()
            from machina.tools.agents import current_session_id

            session = current_session_id.get()
        except Exception:
            return None
        return (str(ctx.workspace), str(session) if session else "", ctx.backend)

    async def execute(self, name: str, /, **kwargs) -> Any:
        """Execute a tool by name with input validation and TTL caching."""
        if name not in self._tools:
            _core_symbol("machina.core.provider", "audit_log")(
                "tool_execution_error", tool_name=name, error="not_registered"
            )
            raise _core_symbol("machina.core.exceptions", "ToolError")(
                f"Tool '{name}' not registered"
            )

        tool = self._tools[name]

        # Input validation
        try:
            self._validate_tool_args(tool, kwargs)
        except ValueError as e:
            _core_symbol("machina.core.provider", "audit_log")(
                "tool_execution_validation_error", tool_name=name, error=str(e)
            )
            raise

        # TTL cache: eligibility comes from the tool's declared effects, not
        # its name. Reads and mutating/exec tools are never cached; cache hits
        # are scoped to the effective execution context so a result produced in
        # one workspace/session/backend can never be served in another.
        cacheable, flushes_cache = _cache_policy(tool)
        cache_key = None
        if cacheable:
            scope = self._cache_scope()
            if scope is not None:
                args_hash = hashlib.sha256(
                    json.dumps(kwargs, sort_keys=True, default=str).encode()
                ).hexdigest()
                cache_key = (name, args_hash, *scope)
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

        if flushes_cache:
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
