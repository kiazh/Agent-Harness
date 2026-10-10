"""Single event schema used by gateway, HTTP, and generated TypeScript types."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA = json.loads(Path(__file__).with_name("events.json").read_text(encoding="utf-8"))
PROTOCOL_VERSION = SCHEMA["version"]
LEGACY_NAMES = {"needs_approval": "permission.required", "approval.resolved": "permission.resolved"}


def _matches(value: Any, kind: str) -> bool:
    if "|" in kind:
        return any(_matches(value, choice) for choice in kind.split("|"))
    if kind.endswith("[]"):
        return isinstance(value, list) and all(_matches(item, kind[:-2]) for item in value)
    return {
        "string": isinstance(value, str),
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
        "null": value is None,
        "object": isinstance(value, dict),
    }[kind]


def validate_event(data: dict[str, Any]) -> None:
    name = data.get("type")
    if name not in SCHEMA["events"] or data.get("protocolVersion") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol event or version")
    fields = {
        "type": "string",
        "protocolVersion": "number",
        "sessionId": "string",
        "turnId": "string",
        **SCHEMA["events"][name],
    }
    allowed = {key.removesuffix("?") for key in fields}
    if set(data) - allowed:
        raise ValueError("undeclared protocol field")
    for raw, kind in fields.items():
        key = raw.removesuffix("?")
        if key not in data:
            if not raw.endswith("?"):
                raise ValueError(f"missing protocol field {key}")
        elif not _matches(data[key], kind):
            raise ValueError(f"invalid protocol field {key}")


def wire_event(event_type: str, session_id: str, turn_id: str, **fields: Any) -> dict[str, Any]:
    data = {
        "protocolVersion": PROTOCOL_VERSION,
        "type": event_type,
        "sessionId": session_id,
        "turnId": turn_id,
        **fields,
    }
    validate_event(data)
    return data


def gateway_event(
    event_type: str, session_id: str, turn_id: str, version: int, **fields: Any
) -> dict[str, Any]:
    # Legacy aliases are confined to the negotiated transport boundary.
    canonical = wire_event(event_type, session_id, turn_id, **fields)
    if version == 2:
        return canonical
    if version != 1:
        raise ValueError("unsupported protocol version")
    canonical.pop("protocolVersion")
    canonical["type"] = LEGACY_NAMES.get(event_type, event_type)
    return canonical


def approval_fields(card: dict[str, Any]) -> dict[str, Any]:
    proposal = card.get("proposal") or card
    return {
        "requestId": str(card.get("request_id") or card.get("requestId") or ""),
        "operation": str(card.get("operation") or proposal.get("operation") or ""),
        "tool": str(proposal.get("tool") or ""),
        "target": str(card.get("target") or "|".join(proposal.get("targets") or [])),
        "argv": list(proposal.get("argv") or []),
        "shell": str(proposal.get("shell") or ""),
        "cwd": str(proposal.get("cwd") or ""),
        "backend": str(proposal.get("backend") or ""),
        "timeout": proposal.get("timeout") or 60,
        "network": list(proposal.get("network") or []),
        "capabilities": list(proposal.get("capabilities") or []),
        "contentDigest": str(proposal.get("content_digest") or ""),
        "contentPreview": str(card.get("content_preview") or proposal.get("content_preview") or ""),
        "contentLength": proposal.get("content_length") or 0,
        "fileDiff": card.get("file_diff") or {},
        "durable": card.get("durable"),
        "status": str(card.get("status") or "pending"),
        "originTurnId": str(card.get("turn_id") or ""),
    }


def canonical_http_event(data: dict[str, Any], session_id: str, turn_id: str) -> dict[str, Any]:
    name = data["type"]
    if name == "text":
        return wire_event("message.delta", session_id, turn_id, text=data["content"])
    if name == "tool_call":
        return wire_event(
            "tool.start",
            session_id,
            turn_id,
            id=data.get("id", ""),
            name=data["tool"],
            args=data["args"],
        )
    if name == "tool_result":
        return wire_event(
            "tool.complete",
            session_id,
            turn_id,
            id=data.get("id", ""),
            name=data["tool"],
            result=data["result"],
            isError=data.get("isError", False),
        )
    if name == "token_usage":
        return wire_event("usage", session_id, turn_id, tokens=data["tokens"])
    if name == "done":
        fields = {
            "text": data.get("content", ""),
            "tokens": data.get("tokens", 0),
            "iterations": data.get("iterations", 0),
            "toolCalls": data.get("toolCalls", 0),
            "cancelled": False,
        }
        if "needsApproval" in data:
            fields["needsApproval"] = data["needsApproval"]
        return wire_event("message.complete", session_id, turn_id, **fields)
    if name == "needs_approval" and "proposal" in data:
        return wire_event(
            name,
            session_id,
            turn_id,
            **approval_fields(
                {
                    "requestId": data["requestId"],
                    "operation": data["operation"],
                    "proposal": data["proposal"],
                }
            ),
        )
    fields = {
        key: value
        for key, value in data.items()
        if key not in ("type", "sessionId", "turnId", "protocolVersion")
    }
    return wire_event(name, session_id, turn_id, **fields)


def typescript_source() -> str:
    """Deterministic output; conformance tests reject stale generated types."""

    def field(raw: str, kind: str) -> str:
        mapped = kind.replace("object", "Record<string, unknown>")
        return f"{raw}: {mapped}"

    entries = [
        "{ "
        + "; ".join([f'type: "{name}"', *(field(key, value) for key, value in fields.items())])
        + " }"
        for name, fields in SCHEMA["events"].items()
    ]
    legacy = [
        entry.replace('"needs_approval"', '"permission.required"').replace(
            '"approval.resolved"', '"permission.resolved"'
        )
        for entry in entries
    ]
    return (
        "// Generated from ah/protocol/events.json; edit the schema, then regenerate.\n"
        "export type CanonicalEvent = { protocolVersion: 2; sessionId: string; turnId: string } & (\n\t"
        + "\n\t| ".join(entries)
        + "\n);\n"
        + "export type LegacyGatewayEvent = { protocolVersion?: 1; sessionId: string; turnId: string } & (\n\t"
        + "\n\t| ".join(legacy)
        + "\n);\n"
        + "export type GatewayEvent = CanonicalEvent | LegacyGatewayEvent;\n"
    )
