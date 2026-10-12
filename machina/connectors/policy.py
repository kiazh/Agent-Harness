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
from typing import Any

from machina.connectors.models import (
    MAX_SCHEMA_DEPTH,
    ConnectorConfig,
    ConnectorToolError,
    DiscoveredTool,
    Trust,
)

MAX_ARGS_BYTES = 65_536

# Schema keywords the validator understands. Anything else fails closed —
# silently discarding constraints would change what was approved.
_SUPPORTED_KEYWORDS = frozenset(
    {
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
    _validate(schema, args, 0, "")


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
