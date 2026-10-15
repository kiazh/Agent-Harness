"""Both transports use the declared v2 event envelope; v1 remains explicit."""

import json
from pathlib import Path

import pytest

from machina.api.app import _serialize_event
from machina.core.models import AgentResponse, StreamEvent


@pytest.mark.parametrize(
    "event, name",
    [
        (StreamEvent(type="text", content="hello"), "message.delta"),
        (
            StreamEvent(
                type="tool_call",
                tool_name="read_file",
                tool_args={"path": "a"},
                tool_call_id="call",
            ),
            "tool.start",
        ),
        (
            StreamEvent(
                type="tool_result", tool_name="read_file", tool_result="value", tool_call_id="call"
            ),
            "tool.complete",
        ),
        (StreamEvent(type="token_usage", tokens_used=3), "usage"),
        (StreamEvent(type="done", response=AgentResponse(content="hello")), "message.complete"),
    ],
)
def test_http_v2_uses_shared_fields(event, name):
    payload = _serialize_event(event, protocol_version=2, session_id="session", turn_id="turn")
    assert payload["type"] == name
    assert payload["protocolVersion"] == 2
    assert payload["sessionId"] == "session"
    assert payload["turnId"] == "turn"
    from machina.protocol import validate_event

    validate_event(payload)


def test_legacy_http_keeps_existing_shape():
    assert _serialize_event(StreamEvent(type="text", content="hello")) == {
        "type": "text",
        "content": "hello",
    }


def test_v2_tool_complete_carries_real_iserror_flag():
    """P10: v2 ``tool.complete`` must reflect the engine's failure flag rather
    than defaulting to ``false``; the v1 compatibility shape is unchanged."""
    failed = _serialize_event(
        StreamEvent(type="tool_result", tool_name="terminal", tool_result="Error: boom"),
        protocol_version=2,
        session_id="session",
        turn_id="turn",
        tool_is_error=True,
    )
    assert failed["type"] == "tool.complete"
    assert failed["isError"] is True

    legacy = _serialize_event(
        StreamEvent(type="tool_result", tool_name="terminal", tool_result="Error: boom")
    )
    assert legacy["type"] == "tool_result"
    assert "isError" not in legacy


def test_schema_rejects_undeclared_fields_and_wrong_types():
    from machina.protocol import wire_event

    with pytest.raises(ValueError, match="field"):
        wire_event("message.delta", "session", "turn", text="hello", surprise=True)
    with pytest.raises(ValueError, match="field"):
        wire_event("usage", "session", "turn", tokens=True)
    with pytest.raises(ValueError, match="event"):
        wire_event("undeclared", "session", "turn")


def test_typescript_types_generated_from_wire_schema():
    from machina.protocol import typescript_source

    assert Path("ui/src/events.ts").read_text(encoding="utf-8") == typescript_source()
    schema = json.loads(Path("machina/protocol/events.json").read_text(encoding="utf-8"))
    assert schema["version"] == 2
    for event in schema["events"]:
        assert f'"{event}"' in typescript_source()


async def test_gateway_v2_emitted_events_conform(db_pool, monkeypatch):
    from machina.protocol import validate_event
    from tests.test_gateway import Harness

    monkeypatch.setattr("machina.gateway.server.db", db_pool)
    monkeypatch.setattr("machina.core.session.db", db_pool)
    monkeypatch.setattr("machina.core.context.db", db_pool)
    monkeypatch.setattr("machina.db.connection.db", db_pool)
    monkeypatch.setattr("machina.core.usage.db", db_pool)
    h = Harness()
    try:
        initialized = await h.call("initialize", {"protocolVersion": 2})
        assert initialized["result"]["protocolVersion"] == 2
        created = await h.call("session.create")
        sid = created["result"]["session"]["id"]
        await h.call("prompt.submit", {"sessionId": sid, "text": "hi"})
        await h.wait_for("message.complete")
        assert h.events()
        for event in h.events():
            validate_event(event)
    finally:
        import uuid

        from machina.core.session import session_manager

        if "sid" in locals():
            await session_manager.delete(uuid.UUID(sid))
        await h.gateway.close()
