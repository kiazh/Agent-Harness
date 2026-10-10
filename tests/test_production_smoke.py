"""Real database smoke checks through the authenticated HTTP surface."""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from ah.api import app as api
from ah.core.models import AgentResponse, StreamEvent

pytestmark = [pytest.mark.integration, pytest.mark.db]


@pytest.fixture
async def smoke_client(db_pool, monkeypatch):
    monkeypatch.setattr("ah.db.connection.db", db_pool)
    monkeypatch.setattr(api, "db", db_pool)
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.context.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    monkeypatch.setattr("ah.core.usage.db", db_pool)
    monkeypatch.setenv("AGENT_HARNESS_API_KEY", "local-smoke-token")
    import ah.services as services

    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    app = api.create_app()
    created = []
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://smoke",
        headers={"Authorization": "Bearer local-smoke-token"},
    ) as client:
        yield client, created
    from ah.core.session import session_manager

    for sid in created:
        await session_manager.delete(uuid.UUID(sid))


async def create_session(client, created):
    response = await client.post("/api/v1/sessions", json={"title": "production smoke"})
    assert response.status_code == 200, response.text
    sid = response.json()["session"]["id"]
    created.append(sid)
    return sid


async def test_health_readiness_metrics_and_auth(smoke_client):
    client, _ = smoke_client
    assert (await client.get("/health")).json()["status"] == "ok"
    assert (await client.get("/ready")).json()["status"] == "ready"
    response = await client.get("/metrics")
    assert response.status_code == 200 and "ah_llm_usage_requests_total" in response.text
    assert "local-smoke-token" not in response.text
    assert (
        await client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    ).status_code == 401


async def test_session_update_and_scheduled_kinds(smoke_client):
    client, created = smoke_client
    sid = await create_session(client, created)
    response = await client.patch(
        f"/api/v1/sessions/{sid}", json={"title": "renamed", "goal": "smoke goal"}
    )
    assert response.status_code == 200
    session = (await client.get(f"/api/v1/sessions/{sid}")).json()["session"]
    assert session["title"] == "renamed"
    context = (await client.get(f"/api/v1/sessions/{sid}/context")).json()
    assert context["goal"] == "smoke goal"
    for kind in ("interval", "heartbeat", "cron"):
        response = await client.post(
            f"/api/v1/sessions/{sid}/jobs",
            json={
                "kind": kind,
                "intervalSeconds": 3600,
                "cronExpression": "0 * * * *" if kind == "cron" else None,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["job"]["kind"] == kind
    assert (await client.delete(f"/api/v1/sessions/{sid}")).status_code == 200
    assert (await client.get(f"/api/v1/sessions/{sid}")).status_code == 404


async def test_sse_completion_releases_claim_and_closes_owned_provider(smoke_client, monkeypatch):
    client, created = smoke_client
    sid = await create_session(client, created)
    closed = []

    class Provider:
        async def close(self):
            closed.append(True)

    class Agent:
        _owns_provider = True
        provider = Provider()

        async def run_stream(self, *args, **kwargs):
            yield StreamEvent(type="text", content="smoke answer")
            yield StreamEvent(type="done", response=AgentResponse(content="smoke answer"))

    import ah.core.agent_factory as factory

    monkeypatch.setattr(factory, "build_agent_for_session", AsyncMock(return_value=Agent()))
    response = await client.post(
        f"/api/v1/sessions/{sid}/prompt", json={"text": "smoke", "protocolVersion": 2}
    )
    assert response.status_code == 200 and response.text.endswith("data: [DONE]\n\n")
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    from ah.protocol import validate_event

    for event in events:
        validate_event(event)
    assert events[-1]["type"] == "message.complete"
    assert events[-1]["text"] == "smoke answer" and closed == [True]
    from ah.core import turns

    token = await turns.try_begin_turn(uuid.UUID(sid))
    assert token
    assert await turns.end_turn(uuid.UUID(sid), token)


async def test_readiness_database_failure_is_unavailable(smoke_client, monkeypatch):
    client, _ = smoke_client
    monkeypatch.setattr(
        api.db, "fetchval", AsyncMock(side_effect=ConnectionError("private database address"))
    )
    response = await client.get("/ready")
    assert response.status_code == 503 and "private database address" not in response.text
