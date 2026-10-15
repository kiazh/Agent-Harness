"""Tests for RPC dispatch — the real ``/rpc`` route must reuse one _RpcGateway.

The provider/transport boundary is the only mock: a scripted ``Gateway``
double stands in for the JSON-RPC backend, while the route, its auth
dependency, the redaction pass, the error mapping, and the real
``_RpcGateway`` id/pending-response bookkeeping all run.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("MACHINA_API_KEY", "test-key-123")

from machina.api.app import _RpcGateway, create_app  # noqa: E402
from machina.gateway.errors import METHOD_NOT_FOUND  # noqa: E402

TEST_API_KEY = "test-key-123"
AUTH = {"Authorization": f"Bearer {TEST_API_KEY}"}


@pytest.fixture
async def rpc():
    """An app served through ASGI with a real _RpcGateway over a scripted Gateway.

    ``state.responses`` controls the backend reply per call:
    - ``{"result": {...}}`` — respond with that result (default ``{"ok": True}``)
    - ``{"error": {...}}`` — respond with a JSON-RPC error frame
    - ``{"no_response": True}`` — never respond (drops the frame)
    """
    with patch("machina.api.app.Gateway") as gateway_cls:
        fake_gw = MagicMock()
        fake_gw._db_ready = False
        fake_gw.close = AsyncMock()
        gateway_cls.return_value = fake_gw

        app = create_app()
        shared = _RpcGateway()
        app.state._rpc_gateway = shared
        state = SimpleNamespace(
            shared=shared,
            fake=fake_gw,
            gateway_cls=gateway_cls,
            seen=[],
            responses={},
        )

        async def handle_line(line: str) -> None:
            frame = json.loads(line)
            state.seen.append(frame)
            rid = frame["id"]
            if state.responses.get("no_response"):
                return
            if "error" in state.responses:
                shared._capture({"jsonrpc": "2.0", "id": rid, "error": state.responses["error"]})
            else:
                shared._capture(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": state.responses.get("result", {"ok": True}),
                    }
                )

        fake_gw.handle_line = handle_line
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            state.client = client
            yield state
        await shared.close()


class TestRpcDispatchReuse:
    """The /rpc route must dispatch through the app-state _RpcGateway."""

    async def test_route_dispatches_and_reuses_one_gateway(self, rpc):
        """Two route calls hit the same shared gateway (one Gateway built)."""
        first = await rpc.client.post(
            "/rpc", headers=AUTH, json={"method": "session.fork", "params": {"a": 1}}
        )
        second = await rpc.client.post(
            "/rpc", headers=AUTH, json={"method": "session.delete", "params": {"b": 2}}
        )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json() == {"result": {"ok": True}}
        assert second.json() == {"result": {"ok": True}}

        # Exactly one Gateway was constructed for the whole app lifetime.
        assert rpc.gateway_cls.call_count == 1
        # Both frames reached the backend with the route's method and params.
        assert [(f["method"], f["params"]) for f in rpc.seen] == [
            ("session.fork", {"a": 1}),
            ("session.delete", {"b": 2}),
        ]
        # The shared gateway assigned both ids and left nothing pending.
        assert rpc.shared._next_id == 2
        assert rpc.shared._responses == {}

    async def test_route_applies_redaction_to_results(self, rpc):
        """The route redacts secret-looking result values before returning."""
        secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
        rpc.responses["result"] = {"apiKey": secret, "nested": {"list": [secret]}}

        resp = await rpc.client.post(
            "/rpc", headers=AUTH, json={"method": "config.get", "params": {}}
        )

        assert resp.status_code == 200, resp.text
        assert secret not in resp.text
        assert resp.json()["result"]["apiKey"] == "[REDACTED_OPENAI_KEY]"
        assert resp.json()["result"]["nested"]["list"] == ["[REDACTED_OPENAI_KEY]"]

    async def test_route_maps_gateway_errors_to_http_status(self, rpc):
        """A JSON-RPC method-not-found error becomes an HTTP 400 with its message."""
        rpc.responses["error"] = {"code": METHOD_NOT_FOUND, "message": "no such method"}

        resp = await rpc.client.post(
            "/rpc", headers=AUTH, json={"method": "nope.method", "params": {}}
        )

        assert resp.status_code == 400
        assert resp.json()["detail"] == "no such method"

    async def test_route_maps_missing_response_to_500(self, rpc):
        """A backend that never responds surfaces as an internal error, not a hang."""
        rpc.responses["no_response"] = True

        resp = await rpc.client.post(
            "/rpc", headers=AUTH, json={"method": "session.list", "params": {}}
        )

        assert resp.status_code == 500
        assert resp.json()["detail"] == "RPC dispatch failed"
        assert rpc.shared._responses == {}

    async def test_route_rejects_prompt_methods(self, rpc):
        """prompt.submit/cancel/shutdown belong to the stream route, not /rpc."""
        for method in ("prompt.submit", "prompt.cancel", "shutdown"):
            resp = await rpc.client.post(
                "/rpc", headers=AUTH, json={"method": method, "params": {}}
            )
            assert resp.status_code == 400, method
            assert "session prompt stream" in resp.json()["detail"]
        # Rejected before dispatch: the backend never saw a frame.
        assert rpc.seen == []


class TestRpcDispatchWithoutGateway:
    async def test_route_returns_503_when_gateway_missing(self):
        app = create_app()
        app.state._rpc_gateway = None
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/rpc", headers=AUTH, json={"method": "session.list", "params": {}}
            )
        assert resp.status_code == 503
        assert resp.json()["detail"] == "RPC gateway not initialized"
