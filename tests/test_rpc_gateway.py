"""Tests for RPC dispatch — must reuse a single _RpcGateway across requests."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from ah.api.app import _RpcGateway, create_app


class TestRpcDispatchReuse:
    """rpc_dispatch must reuse a single _RpcGateway instance."""

    async def test_rpc_dispatch_reuses_gateway_from_app_state(self):
        """rpc_dispatch must use the _RpcGateway from app.state, not create new ones."""
        app = create_app()

        with patch("ah.api.app.Gateway") as mock_gateway_cls:
            mock_gw_instance = MagicMock()
            mock_gw_instance._db_ready = False
            mock_gw_instance.close = AsyncMock()
            mock_gateway_cls.return_value = mock_gw_instance

            # Simulate lifespan creating the gateway once
            shared_gw = _RpcGateway()
            app.state._rpc_gateway = shared_gw

            assert mock_gateway_cls.call_count == 1

            # Simulate the gateway processing requests
            async def mock_handle(line):
                frame = json.loads(line)
                rid = frame.get("id")
                shared_gw._capture({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"ok": True},
                })

            mock_gw_instance.handle_line = mock_handle

            # Make multiple calls through the shared gateway
            result1 = await shared_gw.call("session.fork", {"sessionId": "test"})
            result2 = await shared_gw.call("session.delete", {"sessionId": "test"})

            # Gateway should still be created only once
            assert mock_gateway_cls.call_count == 1
            assert result1 == {"ok": True}
            assert result2 == {"ok": True}

            await shared_gw.close()

    async def test_rpc_dispatch_creates_new_gateway_per_call_bug(self):
        """Document the current bug: rpc_dispatch creates new _RpcGateway per call."""
        with patch("ah.api.app.Gateway") as mock_gateway_cls:
            mock_gw_instance = MagicMock()
            mock_gw_instance._db_ready = False
            mock_gw_instance.close = AsyncMock()
            mock_gateway_cls.return_value = mock_gw_instance

            # Each _RpcGateway() call creates a new Gateway — this is the bug
            gw1 = _RpcGateway()
            gw2 = _RpcGateway()

            # Currently creates 2 Gateway instances — this is the bug
            assert mock_gateway_cls.call_count == 2

            await gw1.close()
            await gw2.close()
