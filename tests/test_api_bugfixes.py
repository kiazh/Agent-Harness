"""Tests for API bug fixes: thread safety, session ownership, auth bypass, and TTL."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

# Set a test API key before importing the app
os.environ["MACHINA_API_KEY"] = "test-key-123"

from machina.api.app import _RpcGateway, create_app
from machina.core.models import Session

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
def auth_headers():
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


@pytest.fixture
async def client():
    with patch("machina.api.app.db.connect", new_callable=AsyncMock):
        with patch("machina.api.app.db.close", new_callable=AsyncMock):
            app = create_app()
            mock_gw = AsyncMock()
            mock_gw.call.return_value = {"result": "ok"}
            mock_gw.close = AsyncMock()
            app.state._rpc_gateway = mock_gw
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                yield c


# ─── Bug 1: _RpcGateway thread safety ───────────────────────────────────────


class TestRpcGatewayThreadSafety:
    """_RpcGateway must be safe for concurrent use across requests."""

    async def test_concurrent_calls_get_unique_ids(self):
        """Concurrent calls to _RpcGateway.call() must each get a unique request ID."""
        with patch("machina.api.app.Gateway") as mock_gateway_cls:
            mock_gw_instance = MagicMock()
            mock_gw_instance._db_ready = False
            mock_gw_instance.close = AsyncMock()
            mock_gateway_cls.return_value = mock_gw_instance

            gw = _RpcGateway()

            # Simulate the gateway processing requests with a delay
            async def mock_handle(line):
                frame = json.loads(line)
                rid = frame.get("id")
                # Small delay to increase chance of interleaving
                await asyncio.sleep(0.001)
                gw._capture(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"ok": True},
                    }
                )

            mock_gw_instance.handle_line = mock_handle

            # Fire 10 concurrent calls
            results = await asyncio.gather(*[gw.call("test.method", {"i": i}) for i in range(10)])

            # All calls should succeed
            assert all(r == {"ok": True} for r in results)

            # All IDs should be unique (1 through 10)
            assert gw._next_id == 10

            await gw.close()

    async def test_concurrent_calls_no_response_overwrite(self):
        """Concurrent calls must not overwrite each other's responses."""
        with patch("machina.api.app.Gateway") as mock_gateway_cls:
            mock_gw_instance = MagicMock()
            mock_gw_instance._db_ready = False
            mock_gw_instance.close = AsyncMock()
            mock_gateway_cls.return_value = mock_gw_instance

            gw = _RpcGateway()

            # Track which IDs were seen
            seen_ids = []
            lock = asyncio.Lock()

            async def mock_handle(line):
                frame = json.loads(line)
                rid = frame.get("id")
                async with lock:
                    seen_ids.append(rid)
                # Variable delay to increase interleaving
                await asyncio.sleep(0.001 * (rid % 3))
                gw._capture(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"id": rid},
                    }
                )

            mock_gw_instance.handle_line = mock_handle

            # Fire 20 concurrent calls
            results = await asyncio.gather(*[gw.call("test.method", {}) for _ in range(20)])

            # Each result should have a unique ID matching its request
            result_ids = [r["id"] for r in results]
            assert len(set(result_ids)) == 20  # All unique
            assert len(seen_ids) == 20  # All requests processed

            await gw.close()

    async def test_next_id_increment_is_atomic(self):
        """_next_id increment must be atomic (no duplicate IDs under concurrency)."""
        with patch("machina.api.app.Gateway") as mock_gateway_cls:
            mock_gw_instance = MagicMock()
            mock_gw_instance._db_ready = False
            mock_gw_instance.close = AsyncMock()
            mock_gateway_cls.return_value = mock_gw_instance

            gw = _RpcGateway()

            # Track all IDs that were assigned
            assigned_ids = []
            id_lock = asyncio.Lock()

            async def mock_handle(line):
                frame = json.loads(line)
                rid = frame.get("id")
                async with id_lock:
                    assigned_ids.append(rid)
                await asyncio.sleep(0.001)
                gw._capture(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"ok": True},
                    }
                )

            mock_gw_instance.handle_line = mock_handle

            # Fire 50 concurrent calls
            await asyncio.gather(*[gw.call("test.method", {}) for _ in range(50)])

            # All 50 IDs must be unique
            assert len(assigned_ids) == 50
            assert len(set(assigned_ids)) == 50

            await gw.close()


# ─── Bug 2: Session ownership on documents ──────────────────────────────────


class TestDocumentOwnership:
    """Document endpoints must verify session ownership."""

    async def test_index_document_rejects_wrong_agent(self, client, auth_headers):
        """POST /api/v1/documents must reject when agent doesn't own the session."""
        session = make_session(agent_id="owner-agent")

        with patch("machina.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = session
            resp = await client.post(
                "/api/v1/documents",
                headers=auth_headers,
                json={
                    "sessionId": str(session.id),
                    "source": "test",
                    "content": "hello",
                    "agent": "intruder-agent",
                },
            )
            assert resp.status_code == 403

    async def test_index_document_accepts_correct_agent(self, client, auth_headers):
        """POST /api/v1/documents must accept when agent owns the session."""
        session = make_session(agent_id="owner-agent")

        with patch("machina.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = session
            with patch(
                "machina.tools.rag.get_rag_pipeline", new_callable=AsyncMock
            ) as mock_pipeline:
                mock_pipeline.return_value = AsyncMock()
                mock_pipeline.return_value.index_document = AsyncMock(return_value=[object()])
                resp = await client.post(
                    "/api/v1/documents",
                    headers=auth_headers,
                    json={
                        "sessionId": str(session.id),
                        "source": "test",
                        "content": "hello",
                        "agent": "owner-agent",
                    },
                )
                assert resp.status_code == 200

    async def test_index_document_accepts_default_agent(self, client, auth_headers):
        """POST /api/v1/documents must accept when no agent is specified (uses session's agent_id)."""
        session = make_session(agent_id="harness")

        with patch("machina.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = session
            with patch(
                "machina.tools.rag.get_rag_pipeline", new_callable=AsyncMock
            ) as mock_pipeline:
                mock_pipeline.return_value = AsyncMock()
                mock_pipeline.return_value.index_document = AsyncMock(return_value=[object()])
                resp = await client.post(
                    "/api/v1/documents",
                    headers=auth_headers,
                    json={
                        "sessionId": str(session.id),
                        "source": "test",
                        "content": "hello",
                    },
                )
                assert resp.status_code == 200

    async def test_search_documents_rejects_wrong_agent(self, client, auth_headers):
        """GET /api/v1/documents/search must reject when agent doesn't own the session."""
        session = make_session(agent_id="owner-agent")

        with patch("machina.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = session
            resp = await client.get(
                f"/api/v1/documents/search?sessionId={session.id}&query=test&agent=intruder-agent",
                headers=auth_headers,
            )
            assert resp.status_code == 403

    async def test_search_documents_accepts_correct_agent(self, client, auth_headers):
        """GET /api/v1/documents/search must accept when agent owns the session."""
        session = make_session(agent_id="owner-agent")

        with patch("machina.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = session
            with patch(
                "machina.tools.rag.get_rag_pipeline", new_callable=AsyncMock
            ) as mock_pipeline:
                mock_pipeline.return_value = AsyncMock()
                mock_pipeline.return_value.search = AsyncMock(return_value=[])
                resp = await client.get(
                    f"/api/v1/documents/search?sessionId={session.id}&query=test&agent=owner-agent",
                    headers=auth_headers,
                )
                assert resp.status_code == 200

    async def test_search_documents_accepts_default_agent(self, client, auth_headers):
        """GET /api/v1/documents/search must accept when no agent is specified."""
        session = make_session(agent_id="harness")

        with patch("machina.api.app.session_manager.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = session
            with patch(
                "machina.tools.rag.get_rag_pipeline", new_callable=AsyncMock
            ) as mock_pipeline:
                mock_pipeline.return_value = AsyncMock()
                mock_pipeline.return_value.search = AsyncMock(return_value=[])
                resp = await client.get(
                    f"/api/v1/documents/search?sessionId={session.id}&query=test",
                    headers=auth_headers,
                )
                assert resp.status_code == 200


# ─── Bug 3: MACHINA_API_KEY=disabled bypass ────────────────────────────


class TestAuthDisabledBypass:
    """MACHINA_API_KEY=disabled must not bypass auth."""

    async def test_disabled_key_still_requires_auth(self):
        """When API key is 'disabled', requests without a key must still be rejected."""
        from machina.api.auth import require_api_key

        # Temporarily set the key to 'disabled'
        with patch("machina.api.auth.configured_key", return_value="disabled"):
            # A request without any auth headers should be rejected
            with pytest.raises(HTTPException) as exc_info:
                await require_api_key(authorization=None, x_api_key=None)
            assert exc_info.value.status_code == 401

    async def test_disabled_key_with_correct_key_succeeds(self):
        """When API key is 'disabled', presenting 'disabled' as the key should succeed."""
        from machina.api.auth import require_api_key

        with patch("machina.api.auth.configured_key", return_value="disabled"):
            # Presenting 'disabled' as the key should succeed
            result = await require_api_key(
                authorization="Bearer disabled",
                x_api_key=None,
            )
            assert result is None  # No exception raised

    async def test_disabled_key_with_wrong_key_rejected(self):
        """When API key is 'disabled', presenting a wrong key must be rejected."""
        from machina.api.auth import require_api_key

        with patch("machina.api.auth.configured_key", return_value="disabled"):
            with pytest.raises(HTTPException) as exc_info:
                await require_api_key(
                    authorization="Bearer wrong-key",
                    x_api_key=None,
                )
            assert exc_info.value.status_code == 401


# ─── Bug 4: _RpcGateway._responses unbounded growth ─────────────────────────


class TestRpcGatewayTTL:
    """_RpcGateway._responses must not grow unboundedly — via the call site.

    The cleanup runs at the top of ``_RpcGateway.call()``, so these tests
    drive ``gw.call()`` and age the pending entries instead of poking
    ``_cleanup_old_responses`` directly.
    """

    @staticmethod
    def _build_gateway():
        """Build a _RpcGateway whose backend Gateway is a scripted double."""
        with patch("machina.api.app.Gateway") as gateway_cls:
            fake = MagicMock()
            fake._db_ready = False
            fake.close = AsyncMock()
            gateway_cls.return_value = fake
            gw = _RpcGateway()
        return gw, fake

    @staticmethod
    def _age(entry_id: int, gw) -> None:
        """Backdate a captured entry beyond the TTL."""
        gw._response_timestamps[entry_id] = time.monotonic() - (gw._RESPONSE_TTL_SECONDS + 1)

    async def test_stale_entry_cleaned_up_by_next_call(self):
        """An expired pending response is swept by the next call's cleanup."""
        gw, fake = self._build_gateway()
        stale_id = 999
        gw._responses[stale_id] = {"jsonrpc": "2.0", "id": stale_id, "result": {}}
        self._age(stale_id, gw)

        async def handle_line(line):
            frame = json.loads(line)
            gw._capture({"jsonrpc": "2.0", "id": frame["id"], "result": {"ok": True}})

        fake.handle_line = handle_line

        result = await gw.call("session.list", {})

        assert result == {"ok": True}
        assert stale_id not in gw._responses
        assert stale_id not in gw._response_timestamps
        await gw.close()

    async def test_responses_dict_does_not_grow_unboundedly(self):
        """Unanswered responses expire; 100 calls leave one aged entry, not 100."""
        gw, fake = self._build_gateway()

        async def handle_line(line):
            frame = json.loads(line)
            rid = frame["id"]
            gw._capture({"jsonrpc": "2.0", "id": rid, "result": {"id": rid}})
            # Simulate a response the caller never claimed: mismatched id,
            # aged beyond the TTL by the time the next call's cleanup runs.
            stray = rid + 1000
            gw._responses[stray] = {"jsonrpc": "2.0", "id": stray, "result": {}}
            self._age(stray, gw)

        fake.handle_line = handle_line

        for _ in range(100):
            await gw.call("test.method", {})

        # Without call-site cleanup the dict would hold all 100 strays.
        assert len(gw._responses) == 1
        assert 1100 in gw._responses
        await gw.close()

    async def test_ttl_cleanup_keeps_recent_entries(self):
        """Cleanup is TTL-bounded: a recent pending entry survives the call."""
        gw, fake = self._build_gateway()
        recent_id = 999
        gw._responses[recent_id] = {"jsonrpc": "2.0", "id": recent_id, "result": {}}
        gw._response_timestamps[recent_id] = time.monotonic()

        async def handle_line(line):
            frame = json.loads(line)
            gw._capture({"jsonrpc": "2.0", "id": frame["id"], "result": {"ok": True}})

        fake.handle_line = handle_line

        result = await gw.call("session.list", {})

        assert result == {"ok": True}
        assert recent_id in gw._responses
        assert recent_id in gw._response_timestamps
        await gw.close()
