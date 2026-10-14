"""FM8 #8: S5 P6/P7 enforcement paths prove load-bearing against the REAL broker guard.

``tests/test_s5_guard_coverage.py`` covers P1-P5 with the real broker and P6
with a forced deny (guard monkeypatched). These tests close the remaining
real-guard gap without editing that file:

- P6 ``connectors/oauth.py::_run_operation`` -> real ``permission_broker.guard``
  denies (LP-08 parent authority) before registration/exchange, and nothing is
  persisted.
- P7 ``connectors/execution.py::connect_authorized`` -> real
  ``permission_broker.guard`` denies before any connector launch.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from machina.core.models import LLMResponse  # noqa: F401  (fixture parity with S5)


async def test_p6_real_guard_denies_oauth_before_exchange(monkeypatch, tmp_path):
    """P6: the real guard aborts OAuth before token registration/exchange."""
    from machina.connectors.models import ConnectorConfig
    from machina.connectors.oauth import OAuthCoordinator, OAuthOwner
    from machina.connectors.oauth_store import OAuthCredentialStore
    from machina.connectors.registry import ConnectorRegistry
    from machina.connectors.service import ConnectorService
    from machina.core.agent_factory import parent_authority_var
    from tests.support.mcp_oauth_fixture import OAuthFixture

    fixture = OAuthFixture()
    await fixture.start()
    coordinator = None
    # LP-08: a restricted parent can never mint a broader child action. The
    # guard consults this live ContextVar (no broker mock anywhere).
    authority_token = parent_authority_var.set({"tools": ["read_file"], "max_mode": "ask"})
    try:
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
        coordinator = OAuthCoordinator(credential_store=creds)
        service = ConnectorService(registry=registry, oauth_coordinator=coordinator)
        coordinator._service = service
        owner = OAuthOwner(session_id="sess-fm8", agent_id="agent-fm8")

        preview = await coordinator.preview("remote", owner)
        started = await coordinator.start(
            "remote", owner, preview_id=preview["previewId"], digest=preview["digest"]
        )
        operation_id = started["operation"]["operationId"]
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            status = await coordinator.status("remote", owner, operation_id=operation_id)
            state = status["operation"]["state"]
            if state in ("failed", "cancelled", "expired", "complete"):
                break
            assert asyncio.get_running_loop().time() < deadline, f"stuck at {state}"
            await asyncio.sleep(0.1)
        assert state == "failed", status
        # Real guard denied before registration/exchange: nothing persisted.
        assert fixture.counters["register"] == 0
        assert fixture.counters["exchange"] == 0
        assert creds._read_index()["grants"] == {}
        auth = await coordinator.status("remote", owner)
        assert auth["auth"]["state"] == "signed_out", auth
    finally:
        if coordinator is not None:
            await coordinator.shutdown()
        await fixture.stop()
        parent_authority_var.reset(authority_token)


async def test_p7_real_guard_denies_connect_before_launch(monkeypatch, tmp_path):
    """P7: the real guard aborts connector connect before any process launch."""
    from machina.connectors.execution import connect_authorized, prepare_connect
    from machina.connectors.models import ConnectorConfig
    from machina.connectors.service import ConnectorService
    from machina.core.agent_factory import parent_authority_var
    from machina.core.execution_context import ExecutionContext
    from machina.permissions import store
    from machina.permissions.broker import ApprovalDenied, clear_execution_context

    monkeypatch.setattr(
        "machina.core.execution_context.resolve_execution_context",
        AsyncMock(return_value=ExecutionContext("ask", "host", tmp_path)),
    )
    monkeypatch.setattr(store, "_db", AsyncMock(return_value=None))

    service = ConnectorService()
    service.registry.add(ConnectorConfig(id="fixture", command="python3", max_retries=0))
    launched = AsyncMock()
    monkeypatch.setattr(service.manager, "connect", launched)
    preview = await prepare_connect(service, "fixture", "session", "harness")

    authority_token = parent_authority_var.set({"tools": ["read_file"], "max_mode": "ask"})
    try:
        with pytest.raises(ApprovalDenied, match="outside parent authority"):
            await connect_authorized(service, "fixture", "session", "harness", preview["digest"])
        launched.assert_not_awaited()
    finally:
        parent_authority_var.reset(authority_token)
        clear_execution_context()
