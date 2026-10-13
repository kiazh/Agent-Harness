"""Effect classification and approval binding for connector tools.

Server content is untrusted input: descriptions, annotations, and schemas
inform display only. Classification upgrades come exclusively from explicit
host configuration (trust + allowlist). Argument validation enforces the
discovered schema's declared constraints and fails closed on anything
outside a supported subset — it protects the host from malformed calls,
not the server from its own schema.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from contextvars import ContextVar
from typing import Any

import regex

from machina.connectors.models import (
    MAX_SCHEMA_DEPTH,
    ConnectorConfig,
    ConnectorToolError,
    DiscoveredTool,
    Trust,
)

MAX_ARGS_BYTES = 65_536
# Untrusted server schemas may carry hostile regexes: the classic nested
# quantifier ("(a+)+$") backtracks exponentially and a length bound on the
# PATTERN does not bound MATCHING time, so matching runs through the `regex`
# engine with a per-match timeout plus a per-call budget (enforced across
# propertyNames fan-out via a ContextVar). Exhausting either fails closed.
_PATTERN_TIMEOUT_SECONDS = 0.05
_PATTERN_TOTAL_BUDGET_SECONDS = 0.25
_pattern_deadline: ContextVar[float | None] = ContextVar(
    "connector_pattern_deadline", default=None
)

# Schema keywords the validator understands. Anything else fails closed —
# silently discarding constraints would change what was approved.
_SUPPORTED_KEYWORDS = frozenset(
    {
        # Inert dialect/annotation keywords emitted by real MCP servers
        # (draft-07 "$schema" from FastMCP/pydantic, "$id", "$comment",
        # "examples"): they change no validation semantics and are ignored.
        # "format" is also treated as an annotation (not enforced). "pattern"
        # and "propertyNames" are enforced below, bounded like every other
        # constraint. "$ref" stays unsupported — reference resolution is
        # different semantics; fail closed until it is implemented.
        "$schema",
        "$id",
        "$comment",
        "examples",
        "format",
        "pattern",
        "propertyNames",
        "type",
        "properties",
        "required",
        "enum",
        "items",
        "description",
        "title",
        "default",
        "additionalProperties",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
        "uniqueItems",
        "anyOf",
        "oneOf",
        "allOf",
        "const",
    }
)

_TYPE_NAMES = ("string", "number", "integer", "boolean", "object", "array", "null")

# The regex engine unrolls counted repetitions at COMPILE time and its
# timeout only covers matching, so `a{100000000}` (12 chars) or nested
# `(a{10000}){10000}` freeze compilation synchronously on the event loop.
# Counts are bounded and counted repetitions may not nest; over-budget
# patterns fail closed with an explicit error.
_MAX_REPETITION_COUNT = 100


def _screen_pattern_budget(pattern: str) -> None:
    """Reject repetition counts that make compilation expand without bound."""
    escaped = False
    in_class = False
    group_stack: list[bool] = []
    quantifies_a_counted_group = False
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif in_class:
            if char == "]":
                in_class = False
        elif char == "[":
            in_class = True
        elif char == "(":
            group_stack.append(False)
            quantifies_a_counted_group = False
        elif char == ")":
            popped = group_stack.pop() if group_stack else False
            if popped and group_stack:
                group_stack[-1] = True
            quantifies_a_counted_group = popped
        elif char == "{":
            cursor = index + 1
            while cursor < length and pattern[cursor].isdigit():
                cursor += 1
            low_text = pattern[index + 1 : cursor]
            high_text = ""
            if cursor < length and pattern[cursor] == ",":
                end = cursor + 1
                while end < length and pattern[end].isdigit():
                    end += 1
                high_text = pattern[cursor + 1 : end]
                cursor = end
            if cursor < length and pattern[cursor] == "}" and (low_text or high_text):
                low = int(low_text) if low_text else 0
                high = int(high_text) if high_text else None
                if low > _MAX_REPETITION_COUNT or (
                    high is not None and high > _MAX_REPETITION_COUNT
                ):
                    raise ConnectorToolError("pattern repetition count too large")
                if quantifies_a_counted_group:
                    raise ConnectorToolError(
                        "nested counted repetitions are not supported"
                    )
                if group_stack:
                    group_stack[-1] = True
                index = cursor
            quantifies_a_counted_group = False
        else:
            quantifies_a_counted_group = False
        index += 1


def _check_schema(schema: Any, depth: int = 0) -> None:
    """Validate every schema branch before evaluating any call arguments."""
    if depth > MAX_SCHEMA_DEPTH or not isinstance(schema, dict):
        raise ConnectorToolError("unsupported schema shape or depth")
    if unknown := set(schema) - _SUPPORTED_KEYWORDS:
        raise ConnectorToolError(f"unsupported schema keywords: {sorted(unknown)}")
    if "type" in schema:
        names = [schema["type"]] if isinstance(schema["type"], str) else schema["type"]
        if (
            not isinstance(names, list)
            or not names
            or any(name not in _TYPE_NAMES for name in names)
        ):
            raise ConnectorToolError("invalid schema type")
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            branches = schema[key]
            if not isinstance(branches, list) or not branches:
                raise ConnectorToolError(f"invalid {key}")
            for branch in branches:
                _check_schema(branch, depth + 1)
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or any(not isinstance(name, str) for name in properties):
        raise ConnectorToolError("invalid schema properties")
    for sub_schema in properties.values():
        _check_schema(sub_schema, depth + 1)
    if "required" in schema and (
        not isinstance(schema["required"], list)
        or any(not isinstance(name, str) for name in schema["required"])
    ):
        raise ConnectorToolError("invalid schema required list")
    additional = schema.get("additionalProperties", True)
    if isinstance(additional, dict):
        _check_schema(additional, depth + 1)
    elif not isinstance(additional, bool):
        raise ConnectorToolError("invalid additionalProperties")
    if "items" in schema:
        items = schema["items"]
        if not isinstance(items, (dict, list)):
            raise ConnectorToolError("invalid schema items")
        for item in items if isinstance(items, list) else [items]:
            _check_schema(item, depth + 1)
    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str) or len(pattern) > 200:
            raise ConnectorToolError("invalid schema pattern")
        _screen_pattern_budget(pattern)
    if "format" in schema and not isinstance(schema["format"], str):
        raise ConnectorToolError("invalid schema format")
    if "propertyNames" in schema:
        _check_schema(schema["propertyNames"], depth + 1)
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (
            not isinstance(schema[key], int) or isinstance(schema[key], bool) or schema[key] < 0
        ):
            raise ConnectorToolError(f"invalid {key}")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and (
            not isinstance(schema[key], (int, float))
            or isinstance(schema[key], bool)
            or (isinstance(schema[key], float) and not math.isfinite(schema[key]))
        ):
            raise ConnectorToolError(f"invalid {key}")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ConnectorToolError("invalid enum")
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        raise ConnectorToolError("invalid uniqueItems")


def _same_json(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _check_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return False


def _validate(schema: Any, value: Any, depth: int, path: str) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise ConnectorToolError(f"schema exceeds max depth at {path or 'root'}")
    if not isinstance(schema, dict):
        raise ConnectorToolError(f"unsupported schema shape at {path or 'root'}")
    if unknown := set(schema) - _SUPPORTED_KEYWORDS:
        raise ConnectorToolError(
            f"unsupported schema keywords at {path or 'root'}: {sorted(unknown)}"
        )
    if "pattern" in schema and isinstance(value, str):
        deadline = _pattern_deadline.get()
        remaining = (
            _PATTERN_TIMEOUT_SECONDS if deadline is None else deadline - time.monotonic()
        )
        if remaining <= 0:
            raise ConnectorToolError(f"pattern time budget exhausted at {path or 'root'}")
        try:
            matched = (
                regex.search(
                    schema["pattern"], value, timeout=min(_PATTERN_TIMEOUT_SECONDS, remaining)
                )
                is not None
            )
        except (MemoryError, OverflowError):
            raise ConnectorToolError(
                f"pattern compilation exceeded its resource budget at {path or 'root'}"
            ) from None
        except TimeoutError:
            raise ConnectorToolError(
                f"pattern check exceeded its time budget at {path or 'root'}"
            ) from None
        except regex.error:
            raise ConnectorToolError(f"invalid schema pattern at {path or 'root'}") from None
        if not matched:
            raise ConnectorToolError(f"string does not match pattern at {path or 'root'}")
    if "propertyNames" in schema and isinstance(value, dict):
        for key in value:
            _validate(schema["propertyNames"], key, depth + 1, f"{path}/{key}")
    for key in ("anyOf", "oneOf", "allOf"):
        if key not in schema:
            continue
        combined = schema[key]
        if key == "allOf":
            for i, branch in enumerate(combined):
                _validate(branch, value, depth + 1, f"{path}/{key}[{i}]")
            continue
        passes = 0
        first_error: ConnectorToolError | None = None
        for i, branch in enumerate(combined):
            try:
                _validate(branch, value, depth + 1, f"{path}/{key}[{i}]")
                passes += 1
            except ConnectorToolError as exc:
                if first_error is None:
                    first_error = exc
        if key == "oneOf" and passes != 1:
            raise ConnectorToolError(f"value matches {passes} oneOf branches at {path or 'root'}")
        if key == "anyOf" and passes == 0:
            raise first_error or ConnectorToolError(f"value matches no branch at {path or 'root'}")
    expected = schema.get("type")
    if expected is not None:
        names = [expected] if isinstance(expected, str) else expected
        if not isinstance(names, list) or any(n not in _TYPE_NAMES for n in names):
            raise ConnectorToolError(f"unsupported type at {path or 'root'}")
        if not any(_check_type(value, name) for name in names):
            raise ConnectorToolError(f"wrong type at {path or 'root'}: expected {names}")
    if "const" in schema and not _same_json(value, schema["const"]):
        raise ConnectorToolError(f"value mismatch at {path or 'root'}")
    if "enum" in schema:
        allowed = schema["enum"]
        if not any(_same_json(value, option) for option in allowed):
            raise ConnectorToolError(f"value not in enum at {path or 'root'}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ConnectorToolError(f"string too short at {path or 'root'}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ConnectorToolError(f"string too long at {path or 'root'}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, cmp in (
            ("minimum", lambda a, b: a >= b),
            ("maximum", lambda a, b: a <= b),
            ("exclusiveMinimum", lambda a, b: a > b),
            ("exclusiveMaximum", lambda a, b: a < b),
        ):
            if key in schema and not cmp(value, schema[key]):
                raise ConnectorToolError(f"number out of range at {path or 'root'} ({key})")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ConnectorToolError(f"invalid properties at {path or 'root'}")
        for name in schema.get("required", []) or []:
            if name not in value:
                raise ConnectorToolError(f"missing required '{name}' at {path or 'root'}")
        for name, sub in value.items():
            if name in properties:
                _validate(properties[name], sub, depth + 1, f"{path}.{name}" if path else name)
            elif schema.get("additionalProperties") is False:
                raise ConnectorToolError(f"undeclared argument '{name}' at {path or 'root'}")
            elif isinstance(schema.get("additionalProperties"), dict):
                _validate(schema["additionalProperties"], sub, depth + 1, f"{path}.{name}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ConnectorToolError(f"too few items at {path or 'root'}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ConnectorToolError(f"too many items at {path or 'root'}")
        if schema.get("uniqueItems") and len(
            {json.dumps(v, sort_keys=True, default=str) for v in value}
        ) != len(value):
            raise ConnectorToolError(f"duplicate items at {path or 'root'}")
        if "items" in schema:
            items = schema["items"]
            if isinstance(items, dict):
                for i, sub in enumerate(value):
                    _validate(items, sub, depth + 1, f"{path}[{i}]")
            elif isinstance(items, list):
                if len(value) > len(items):
                    raise ConnectorToolError(f"too many items at {path or 'root'}")
                for i, (sub_schema, sub) in enumerate(zip(items, value)):
                    _validate(sub_schema, sub, depth + 1, f"{path}[{i}]")
            else:
                raise ConnectorToolError(f"invalid items at {path or 'root'}")


def validate_arguments(remote_name: str, args: dict[str, Any], schema: dict[str, Any]) -> None:
    """Enforce a discovered schema on call arguments (fail closed)."""
    if not isinstance(args, dict):
        raise ConnectorToolError(f"arguments for {remote_name} must be an object")
    try:
        encoded_args = json.dumps(args, allow_nan=False).encode("utf-8")
        encoded_schema = json.dumps(schema, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ConnectorToolError("arguments and schemas must be finite JSON values") from exc
    if len(encoded_args) > MAX_ARGS_BYTES or len(encoded_schema) > MAX_ARGS_BYTES:
        raise ConnectorToolError(f"arguments for {remote_name} exceed {MAX_ARGS_BYTES} bytes")
    _check_schema(schema)
    if not schema:
        if args:
            raise ConnectorToolError(f"{remote_name} declares no parameters but got arguments")
        return
    token = _pattern_deadline.set(time.monotonic() + _PATTERN_TOTAL_BUDGET_SECONDS)
    try:
        _validate(schema, args, 0, "")
    finally:
        _pattern_deadline.reset(token)


def classify_tool(
    config: ConnectorConfig,
    tool: DiscoveredTool,
) -> str:
    """Return 'trusted_read' or 'needs_approval'.

    Only explicit host configuration upgrades: trust == trusted_read AND
    the remote name is on the connector's allowlist. Annotations,
    descriptions, and titles are display-only — a read-only annotation on
    an unknown tool, or a mutation hint on an allowlisted one, changes
    nothing.
    """
    if config.trust == Trust.TRUSTED_READ.value and tool.remote_name in config.tool_allowlist:
        return "trusted_read"
    return "needs_approval"


def approval_binding(
    config: ConnectorConfig,
    tool: DiscoveredTool,
    args: dict[str, Any],
    *,
    mode: str,
    backend: str,
    timeout: int,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    """Canonical approval fields for one connector call (Phase C contract).

    Binds connector id/owner/revision, transport destination or executable
    identity, credential-profile identity (never values), tool + schema
    revision, canonical arguments, and execution context. The digest covers
    the complete values; any change requires fresh approval.
    """
    if config.transport == "streamable_http":
        destination = config.endpoint
    else:
        destination = " ".join([config.command, *config.argv]).strip()
    canonical = {
        "connector_id": config.id,
        "owner": config.owner,
        "config_revision": config.revision,
        "transport": config.transport,
        "destination": destination,
        "credential_profile": config.credential_profile,
        "tool": tool.remote_name,
        "schema_revision": tool.schema_revision,
        "args": args,
        "mode": mode,
        "backend": backend,
        "timeout": timeout,
        "capabilities": sorted(capabilities or []),
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**canonical, "digest": digest}
