"""Tests for the FastAPI HTTP API (ah.api)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

# Set a test API key before importing the app
os.environ["AGENT_HARNESS_API_KEY"] = "test-key-123"

from ah.api.app import create_app
from ah.core.models import AgentResponse, Session, StreamEvent

TEST_API_KEY = "test-key-123"


def make_session(**kwargs) -> Session:
    """Create a Session with sensible defaults for testing."""
    defaults = dict(
        id=uuid.uuid4(),
        title="Test Session",
        model="test-model",
        provider="test-provider",
        status="active",
        last_activity=datetime.now(),
    )
    defaults.update(kwargs)
    return Session(**defaults)


@pytest.fixture
async def client():
    with patch("ah.api.app.db.connect", new_callable=AsyncMock):
        with patch("ah.api.app.db.close", new_callable=AsyncMock):
            app = create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                yield c


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


# ─── health ──────────────────────────────────────────────────────────────────


class TestHealth:
    async def test_health_no_auth(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data


# ─── auth ────────────────────────────────────────────────────────────────────


class TestAuth:
    async def test_auth_rejection_no_key(self, client):
        resp = await client.get("/sessions")
        assert resp.status_code == 401

    async def test_auth_rejection_wrong_key(self, client):
        resp = await client.get("/sessions", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    async def test_auth_acceptance_with_key(self, client, auth_headers):
        # Mock the DB call so we only test auth passes (not 401)
        with patch("ah.api.app.session_manager.list_sessions", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = []
            resp = await client.get("/sessions", headers=auth_headers)
            assert resp.status_code != 401


# ─── sessions ────────────────────────────────────────────────────────────────


class TestSessions:
    async def test_create_session(self, client, auth_headers):
        mock_session = make_session(title="New Test")

        with patch("ah.api.app.session_manager.create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_session
            resp = await client.post("/sessions", headers=auth_headers, json={"title": "New Test"})
            assert resp.status_code == 200
            data = resp.json()
            assert "session" in data
            assert data["session"]["title"] == "New Test"

    async def test_list_sessions(self, client, auth_headers):
        mock_sessions = [make_session(title="Session 1"), make_session(title="Session 2")]

        with patch("ah.api.app.session_manager.list_sessions", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = mock_sessions
            resp = await client.get("/sessions", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "sessions" in data
            assert len(data["sessions"]) == 2

    async def test_get_session(self, client, auth_headers):
        mock_session = make_session()

        with patch("ah.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_session
            with patch(
                "ah.api.app.context_manager.get_chunks", new_callable=AsyncMock
            ) as mock_chunks:
                mock_chunks.return_value = []
                resp = await client.get(f"/sessions/{mock_session.id}", headers=auth_headers)
                assert resp.status_code == 200
                data = resp.json()
                assert "session" in data
                assert "history" in data

    async def test_get_session_not_found(self, client, auth_headers):
        with patch("ah.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            resp = await client.get(f"/sessions/{uuid.uuid4()}", headers=auth_headers)
            assert resp.status_code == 404

    async def test_get_session_invalid_id(self, client, auth_headers):
        resp = await client.get("/sessions/not-a-uuid", headers=auth_headers)
        assert resp.status_code == 400

    async def test_usage_requires_auth_and_returns_summary(self, client, auth_headers):
        mock_session = make_session(agent_id="harness")
        path = f"/api/v1/sessions/{mock_session.id}/usage"
        assert (await client.get(path)).status_code == 401
        with patch("ah.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_session
            with patch("ah.core.usage.usage_store.summary", new_callable=AsyncMock) as summary:
                summary.return_value = {"sessionId": str(mock_session.id), "session": {"requests": 2}}
                resp = await client.get(path, headers=auth_headers)
                assert resp.status_code == 200
                assert resp.json()["session"]["requests"] == 2
                summary.assert_awaited_once_with(mock_session.id, "harness")


# ─── prompt SSE ──────────────────────────────────────────────────────────────


class TestPromptSSE:
    async def test_prompt_sse_stream(self, client, auth_headers):
        mock_session = make_session()

        class FakeAgent:
            async def run_stream(self, session_id, user_message, verbose=False):
                yield StreamEvent(type="text", content="Hello")
                yield StreamEvent(
                    type="done",
                    response=AgentResponse(content="Hello", tokens_used=10, iterations=1),
                )

        with patch("ah.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_session
            with patch("ah.core.provider.get_provider") as mock_provider:
                mock_provider.return_value = MagicMock()
                with patch("ah.core.agent.ReActAgent", return_value=FakeAgent()):
                    resp = await client.post(
                        f"/sessions/{mock_session.id}/prompt",
                        headers=auth_headers,
                        json={"text": "Hi"},
                    )
                    assert resp.status_code == 200
                    assert "text/event-stream" in resp.headers.get("content-type", "")
                    # Read the SSE stream
                    lines = []
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            lines.append(line[6:])
                    assert len(lines) >= 2  # At least one event + [DONE]


# ─── context ─────────────────────────────────────────────────────────────────


class TestContext:
    async def test_get_context(self, client, auth_headers):
        mock_session = make_session()

        with patch("ah.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_session
            with patch(
                "ah.api.app.context_manager.get_chunks", new_callable=AsyncMock
            ) as mock_chunks:
                mock_chunks.return_value = []
                with patch(
                    "ah.api.app.context_manager.get_token_usage", new_callable=AsyncMock
                ) as mock_tokens:
                    mock_tokens.return_value = 0
                    resp = await client.get(
                        f"/sessions/{mock_session.id}/context", headers=auth_headers
                    )
                    assert resp.status_code == 200
                    data = resp.json()
                    assert "chunks" in data
                    assert "totalTokens" in data


# ─── memory ──────────────────────────────────────────────────────────────────


class TestMemory:
    async def test_get_memory(self, client, auth_headers):
        mock_session = make_session()

        with patch("ah.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_session
            with patch(
                "ah.memory.store.memory_store.search", new_callable=AsyncMock
            ) as mock_search:
                mock_search.return_value = []
                with patch(
                    "ah.memory.store.memory_store.count", new_callable=AsyncMock
                ) as mock_count:
                    mock_count.return_value = 0
                    resp = await client.get(
                        f"/sessions/{mock_session.id}/memory", headers=auth_headers
                    )
                    assert resp.status_code == 200
                    data = resp.json()
                    assert "memories" in data
                    assert "total" in data
                    mock_search.assert_awaited_once_with(session_id=mock_session.id, limit=20)
                    mock_count.assert_awaited_once_with(session_id=mock_session.id)


# ─── jobs ────────────────────────────────────────────────────────────────────


class TestJobs:
    async def test_create_job(self, client, auth_headers):
        mock_session = make_session()
        mock_job = MagicMock()
        mock_job.to_dict.return_value = {
            "id": str(uuid.uuid4()),
            "name": "test-job",
            "kind": "interval",
            "sessionId": str(mock_session.id),
            "agent": "harness",
            "prompt": "test",
            "intervalSeconds": 300,
            "enabled": True,
            "status": "pending",
            "lastRunAt": None,
            "nextRunAt": None,
            "lastError": None,
            "runCount": 0,
        }

        with patch("ah.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_session
            with patch("ah.core.scheduler.job_store.create", new_callable=AsyncMock) as mock_create:
                mock_create.return_value = mock_job
                resp = await client.post(
                    f"/sessions/{mock_session.id}/jobs",
                    headers=auth_headers,
                    json={"kind": "interval", "prompt": "test", "intervalSeconds": 300},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert "job" in data

    async def test_list_jobs(self, client, auth_headers):
        mock_session = make_session()
        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"id": str(uuid.uuid4())}

        with patch("ah.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_session
            with patch("ah.core.scheduler.job_store.list", new_callable=AsyncMock) as mock_list:
                mock_list.return_value = [mock_job]
                resp = await client.get(f"/sessions/{mock_session.id}/jobs", headers=auth_headers)
                assert resp.status_code == 200
                data = resp.json()
                assert "jobs" in data


# ─── agents ──────────────────────────────────────────────────────────────────


class TestAgents:
    async def test_list_agents(self, client, auth_headers):
        mock_agent = MagicMock()
        mock_agent.to_dict.return_value = {
            "name": "test-agent",
            "description": "Test",
            "systemPrompt": "",
            "tools": [],
            "model": None,
            "provider": None,
            "maxIterations": 10,
            "source": "builtin",
        }

        with patch("ah.core.agent_def.agent_registry.list", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = [mock_agent]
            resp = await client.get("/agents", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "agents" in data
            assert len(data["agents"]) == 1


# ─── rpc ─────────────────────────────────────────────────────────────────────


class TestRpc:
    async def test_rpc_dispatch(self, client, auth_headers):
        with patch("ah.api.app._RpcGateway") as MockGateway:
            mock_gw = AsyncMock()
            mock_gw.call.return_value = {"result": "ok"}
            mock_gw.close = AsyncMock()
            MockGateway.return_value = mock_gw

            resp = await client.post(
                "/rpc",
                headers=auth_headers,
                json={"method": "session.list", "params": {}},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "result" in data
