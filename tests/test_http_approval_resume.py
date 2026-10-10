"""HTTP continuations execute only the reviewed action under a new claim."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from ah.api.app import create_app
from ah.core.agent import ReActAgent
from ah.core.config import config
from ah.core.models import AgentResponse, LLMResponse, StreamEvent
from ah.core.session import SessionManager
from ah.permissions import store


def events(response):
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


@pytest.fixture
async def approval_http(db_pool, monkeypatch, tmp_path):
    from ah import services
    import ah.core.agent_factory as factory
    from ah.tools import file  # noqa: F401

    monkeypatch.setenv("AGENT_HARNESS_API_KEY", "approval-resume-test")
    monkeypatch.setattr("ah.db.connection.db", db_pool)
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr(config, "execution_mode", "ask")
    monkeypatch.setattr(config, "workspace_root", str(tmp_path))
    monkeypatch.setattr(config, "agent_harness_home", str(tmp_path))
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    session = await SessionManager().create(title="HTTP approval")
    arguments = {"path": "approved.txt", "content": "exact reviewed bytes"}
    agent = ReActAgent(provider=SimpleNamespace(), allowed_tools=["write_file"])

    async def initial_stream(sid, text, verbose=False):
        made = []
        response = LLMResponse(
            content="",
            model="controlled",
            tool_calls=[
                {
                    "id": "approved-call",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": json.dumps(arguments)},
                }
            ],
        )
        async for event in agent._execute_tool_calls_stream(response, [], made, sid):
            yield event
        yield StreamEvent(
            type="done",
            response=AgentResponse(
                content="paused",
                tool_calls=made,
                needs_approval=[
                    entry["needs_approval"] for entry in made if "needs_approval" in entry
                ],
            ),
        )

    agent.run_stream = initial_stream
    monkeypatch.setattr(factory, "build_agent_for_session", AsyncMock(return_value=agent))
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"Authorization": "Bearer approval-resume-test"},
    ) as client:
        response = await client.post(
            f"/api/v1/sessions/{session.id}/prompt", json={"text": "write"}
        )
        assert response.status_code == 200
        assert not any(event["type"] == "error" for event in events(response))
        pause = next(event for event in events(response) if event["type"] == "needs_approval")
        record = await store.get_approval(pause["requestId"])
        assert not (tmp_path / arguments["path"]).exists()
        yield client, session, record, arguments, tmp_path, db_pool
    await SessionManager().delete(session.id)
    store._mem.approvals.pop(record["request_id"], None)


@pytest.mark.parametrize("protocol_version", [1, 2])
async def test_http_approved_action_resumes_once_with_new_fence(approval_http, protocol_version):
    client, session, record, arguments, root, database = approval_http
    rid = record["request_id"]
    decision = await client.post(
        f"/api/v1/approvals/{rid}/resolve",
        json={"sessionId": str(session.id), "verdict": "approved"},
    )
    assert decision.status_code == 200
    body = {"sessionId": str(session.id), "turnId": record["turn_id"], "toolArgs": arguments}
    body["protocolVersion"] = protocol_version
    resumed = await client.post(f"/api/v1/approvals/{rid}/resume", json=body)
    assert resumed.status_code == 200, resumed.text
    emitted = events(resumed)
    assert resumed.headers["X-AH-Protocol-Version"] == str(protocol_version)
    if protocol_version == 2:
        from ah.protocol import validate_event

        for event in emitted:
            validate_event(event)
    assert not any(event["type"] == "error" for event in emitted), emitted
    started = next(event for event in emitted if event["type"] == "turn.started")
    assert started["turnId"] != record["turn_id"]
    assert any(event["type"] == "approval.resumed" for event in emitted)
    assert (root / arguments["path"]).read_text() == arguments["content"]
    assert (await store.get_approval(rid))["status"] == "completed"
    assert (
        await database.fetchval("SELECT claim_owner FROM sessions WHERE id=$1", session.id) is None
    )
    assert (await client.post(f"/api/v1/approvals/{rid}/resume", json=body)).status_code == 409


@pytest.mark.parametrize("verdict", ["denied", "cancelled", "expired"])
async def test_nonapproved_resolution_cannot_execute(approval_http, verdict):
    client, session, record, arguments, root, database = approval_http
    rid = record["request_id"]
    decision = await client.post(
        f"/api/v1/approvals/{rid}/resolve", json={"sessionId": str(session.id), "verdict": verdict}
    )
    assert decision.status_code == 200
    assert decision.json()["event"]["type"] == "approval.resolved"
    resumed = await client.post(
        f"/api/v1/approvals/{rid}/resume",
        json={"sessionId": str(session.id), "turnId": record["turn_id"], "toolArgs": arguments},
    )
    assert resumed.status_code == 409
    assert not (root / arguments["path"]).exists()


@pytest.mark.parametrize("change", ["content", "session", "turn", "mode"])
async def test_resume_revalidates_action_and_origin(approval_http, change):
    client, session, record, arguments, root, database = approval_http
    rid = record["request_id"]
    await client.post(
        f"/api/v1/approvals/{rid}/resolve",
        json={"sessionId": str(session.id), "verdict": "approved"},
    )
    body = {"sessionId": str(session.id), "turnId": record["turn_id"], "toolArgs": dict(arguments)}
    if change == "content":
        body["toolArgs"]["content"] = "changed after approval"
    elif change == "session":
        import uuid

        body["sessionId"] = str(uuid.uuid4())
    elif change == "turn":
        body["turnId"] = "foreign-turn"
    else:
        await database.execute(
            "UPDATE sessions SET execution_mode='workspace' WHERE id=$1", session.id
        )
    response = await client.post(f"/api/v1/approvals/{rid}/resume", json=body)
    assert response.status_code in (403, 409)
    assert not (root / arguments["path"]).exists()
    assert (await store.get_approval(rid))["status"] == "approved"


async def test_mode_change_between_validation_and_execution_requires_new_approval(
    approval_http, monkeypatch
):
    import ah.core.agent_factory as factory

    client, session, record, arguments, root, database = approval_http
    rid = record["request_id"]
    await client.post(
        f"/api/v1/approvals/{rid}/resolve",
        json={"sessionId": str(session.id), "verdict": "approved"},
    )
    original = factory.build_agent_for_session

    async def changed_mode(*args, **kwargs):
        await database.execute(
            "UPDATE sessions SET execution_mode='workspace' WHERE id=$1", session.id
        )
        return await original(*args, **kwargs)

    monkeypatch.setattr(factory, "build_agent_for_session", changed_mode)
    response = await client.post(
        f"/api/v1/approvals/{rid}/resume",
        json={"sessionId": str(session.id), "turnId": record["turn_id"], "toolArgs": arguments},
    )
    assert response.status_code == 200
    assert not (root / arguments["path"]).exists()
    assert any(event["type"] == "needs_approval" for event in events(response))
    assert (await store.get_approval(rid))["status"] == "approved"
