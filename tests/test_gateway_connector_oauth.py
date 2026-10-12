"""Task 8: authenticated gateway OAuth contracts (serialized, owner-bound)."""

import asyncio
import json
import os

import httpx
import pytest

from ah.connectors.models import ConnectorConfig
from ah.connectors.oauth import OAuthCoordinator
from ah.connectors.oauth_store import OAuthCredentialStore
from ah.connectors.registry import ConnectorRegistry
from ah.connectors.service import ConnectorService
from ah.core.runtime import RuntimeServices
from ah.core.session import session_manager
from ah.db.connection import db
from ah.gateway import features
from ah.gateway.errors import METHOD_NOT_FOUND, UNAUTHORIZED
from ah.gateway.server import Gateway
from tests.support.mcp_oauth_fixture import OAuthFixture

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL"),
    reason="AGENT_HARNESS_TEST_DATABASE_URL not set",
)


class Harness:
    def __init__(self, runtime):
        os.environ["AH_GATEWAY_NO_SCHEDULER"] = "1"
        self.frames: list[dict] = []
        self.gateway = Gateway(self.frames.append, owns_db=False, runtime=runtime)
        self.gateway._auth_token = None
        self._next_id = 0

    async def call(self, method, params=None):
        self._next_id += 1
        rid = self._next_id
        frame = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        await self.gateway.handle_line(json.dumps(frame))
        return next(f for f in self.frames if f.get("id") == rid and "method" not in f)


async def _runtime(fixture, tmp_path, *, browser=None):
    registry = ConnectorRegistry()
    registry.add(
        ConnectorConfig(
            id="remote",
            transport="streamable_http",
            endpoint=fixture.endpoint,
            local_access=True,
            oauth={"client_mode": "auto"},
        )
    )
    creds = OAuthCredentialStore(tmp_path / "oauth", key_provider=lambda: b"k" * 32)
    coordinator = OAuthCoordinator(
        credential_store=creds, browser_opener=browser or (lambda url: True)
    )
    service = ConnectorService(registry=registry, oauth_coordinator=coordinator)
    coordinator._service = service
    return RuntimeServices(_connector_service=service), coordinator


async def _poll(harness, session_id, operation_id, want="complete", timeout=30):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await harness.call(
            "mcp.oauth.status",
            {"id": "remote", "sessionId": session_id, "operationId": operation_id},
        )
        assert "result" in response, response
        state = response["result"]["operation"]["state"]
        if state == want or state in ("failed", "cancelled", "expired"):
            return response["result"]
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"stuck at {state}")
        await asyncio.sleep(0.1)


@pytest.mark.db
async def test_oauth_methods_require_token_and_reject_unknown_params(
    db_pool, monkeypatch, tmp_path
):
    monkeypatch.setattr(db, "_pool", db_pool.pool)
    fixture = OAuthFixture()
    await fixture.start()
    try:
        runtime, coordinator = await _runtime(fixture, tmp_path)
        harness = Harness(runtime)
        harness.gateway._auth_token = "fixture-token"
        session = await session_manager.create(title="oauth", agent_id="harness")
        session_id = str(session.id)
        try:
            for method, params in (
                ("mcp.oauth.preview", {"id": "remote", "sessionId": session_id}),
                (
                    "mcp.oauth.start",
                    {"id": "remote", "sessionId": session_id, "previewId": "x", "digest": "y"},
                ),
                ("mcp.oauth.status", {"id": "remote", "sessionId": session_id}),
                ("mcp.oauth.url", {"id": "remote", "sessionId": session_id, "operationId": "x"}),
                ("mcp.oauth.open", {"id": "remote", "sessionId": session_id, "operationId": "x"}),
                ("mcp.oauth.cancel", {"id": "remote", "sessionId": session_id, "operationId": "x"}),
                (
                    "mcp.oauth.forget",
                    {"id": "remote", "sessionId": session_id, "expectedGeneration": 0},
                ),
            ):
                assert (await harness.call(method, params))["error"]["code"] == UNAUTHORIZED
            authed = {"token": "fixture-token", "id": "remote", "sessionId": session_id}
            response = await harness.call(
                "mcp.oauth.preview", {**authed, "issuer": "x", "callback": "evil"}
            )
            assert response["error"]["code"] in (400, -32602), response
            assert (await harness.call("mcp.call", {"token": "fixture-token"}))["error"][
                "code"
            ] == METHOD_NOT_FOUND
        finally:
            await session_manager.delete(session.id)
            await coordinator.shutdown()
    finally:
        await fixture.stop()


@pytest.mark.db
async def test_full_rpc_flow_across_separate_calls(db_pool, monkeypatch, tmp_path):
    monkeypatch.setattr(db, "_pool", db_pool.pool)
    opened = []
    fixture = OAuthFixture()
    await fixture.start()
    try:
        runtime, coordinator = await _runtime(
            fixture, tmp_path, browser=lambda url: opened.append(url) or True
        )
        harness = Harness(runtime)
        session = await session_manager.create(title="oauth", agent_id="harness")
        other = await session_manager.create(title="other", agent_id="intruder")
        session_id, other_id = str(session.id), str(other.id)
        try:
            preview = (
                await harness.call("mcp.oauth.preview", {"id": "remote", "sessionId": session_id})
            )["result"]
            assert "previewId" in preview
            assert opened == []  # no browser before explicit open
            started = (
                await harness.call(
                    "mcp.oauth.start",
                    {
                        "id": "remote",
                        "sessionId": session_id,
                        "previewId": preview["previewId"],
                        "digest": preview["digest"],
                    },
                )
            )["result"]
            operation_id = started["operation"]["operationId"]
            await _poll(harness, session_id, operation_id, want="awaiting_browser")
            # Foreign session cannot read the URL or the operation.
            denied = await harness.call(
                "mcp.oauth.url",
                {"id": "remote", "sessionId": other_id, "operationId": operation_id},
            )
            assert "error" in denied, denied
            denied_status = await harness.call(
                "mcp.oauth.status",
                {"id": "remote", "sessionId": other_id, "operationId": operation_id},
            )
            assert "error" in denied_status
            # Explicit open invokes the browser exactly once.
            url = (
                await harness.call(
                    "mcp.oauth.url",
                    {"id": "remote", "sessionId": session_id, "operationId": operation_id},
                )
            )["result"]["authorizationUrl"]
            assert "code_challenge=" in url
            assert (
                await harness.call(
                    "mcp.oauth.open",
                    {"id": "remote", "sessionId": session_id, "operationId": operation_id},
                )
            )["result"] == {"opened": True}
            assert len(opened) == 1
            # External browser consent delivered to the loopback listener.
            async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
                response = await client.get(url)
                assert response.status_code == 302
                delivered = await client.get(response.headers["location"])
                assert delivered.status_code == 200
            final = await _poll(harness, session_id, operation_id, want="complete")
            assert final["operation"]["state"] == "complete"
            # Status is local-only: no connection was launched.
            status = (
                await harness.call("mcp.oauth.status", {"id": "remote", "sessionId": session_id})
            )["result"]
            assert status["auth"]["state"] == "authorized"
            assert not runtime.get_connector_service().manager._connections
            # Forget with a stale generation cannot delete the new grant.
            stale = await harness.call(
                "mcp.oauth.forget",
                {"id": "remote", "sessionId": session_id, "expectedGeneration": 0, "revoke": True},
            )
            assert "error" in stale, stale
            generation = status["auth"]["generation"]
            forgotten = (
                await harness.call(
                    "mcp.oauth.forget",
                    {
                        "id": "remote",
                        "sessionId": session_id,
                        "expectedGeneration": generation,
                        "revoke": True,
                    },
                )
            )["result"]
            assert forgotten["auth"]["state"] != "authorized"
            assert forgotten["remoteRevocation"] in (
                "succeeded",
                "unsupported",
                "failed",
                "not_requested",
            )
        finally:
            await session_manager.delete(session.id)
            await session_manager.delete(other.id)
            await coordinator.shutdown()
    finally:
        await fixture.stop()


def test_exact_oauth_methods_registered_and_no_raw_call():
    for method in (
        "mcp.oauth.preview",
        "mcp.oauth.start",
        "mcp.oauth.status",
        "mcp.oauth.url",
        "mcp.oauth.open",
        "mcp.oauth.cancel",
        "mcp.oauth.forget",
    ):
        assert method in features.METHODS
    assert "mcp.call" not in features.METHODS
