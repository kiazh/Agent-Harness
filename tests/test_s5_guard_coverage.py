"""S5.2 enforcement-point audit: every side-effect path passes broker.guard.

Path inventory (see report for the full table):
  P1 agent loop            machina/core/agent.py::_execute_tool_calls_stream -> guard
  P2 orchestrator children machina/core/orchestrator.py::delegate -> agent.run -> guard
                           (+ LP-08 parent caps enforced inside guard)
  P3 scheduler script jobs machina/core/scheduler.py::_execute (no-agent) -> guard
  P4 scheduler agent jobs  scheduler._execute -> agent.run -> guard (covered via P1)
  P5 MCP direct exec       connectors/mcp_client.py::call_tool reachable only via
                           connectors/execution.py::execute_call, which requires a
                           guard-issued execution snapshot + digest equality
  P6 OAuth operations      connectors/oauth.py::_run_operation -> guard
  P7 connector connect     connectors/execution.py::connect_authorized -> guard
                           (covered by existing test_connectors_execution tests)

All tests use the real permission_broker path (no broker mocks), except P6
where the broker verdict is forced to deny to prove the guard is load-bearing
(deny -> no token exchange, no credential persisted).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from machina.core.models import LLMResponse


def _tool_response(name: str, args: dict, call_id: str = "call-1") -> LLMResponse:
    import json

    return LLMResponse(
        content="",
        model="s5-probe",
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    )


async def test_p1_agent_loop_blocks_unguarded_exec(tmp_path):
    """P1: agent loop must not execute a pending action without approval."""
    from machina.core.agent import BaseReActAgent
    from machina.permissions.broker import clear_execution_context, set_approval_handler

    marker = tmp_path / "s5_unguarded_marker"
    agent = BaseReActAgent(provider=AsyncMock(), agent_id="s5-agent", allowed_tools=["terminal"])
    set_approval_handler(None)  # headless: pending actions pause, never run
    made: list = []
    pending: list = []
    try:
        events = [
            event
            async for event in agent._execute_tool_calls_stream(
                _tool_response("terminal", {"command": f"touch {marker}"}),
                [],
                made,
                uuid.uuid4(),
                pending,
            )
        ]
        assert made and made[0].get("needs_approval"), made
        assert "Needs approval" in made[0]["result_preview"], made[0]["result_preview"]
        assert not marker.exists(), "side effect executed without broker approval"
        assert not any("pwned" in str(getattr(e, "tool_result", "")) for e in events)
    finally:
        set_approval_handler(None)
        clear_execution_context()


async def test_p2_parent_authority_enforced_at_broker():
    """P2/LP-08: a restricted parent can never mint a broader child action."""
    from machina.core.agent_factory import parent_authority_var
    from machina.permissions.broker import (
        ApprovalDenied,
        clear_execution_context,
        permission_broker,
        set_approval_handler,
    )
    from machina.permissions.policy import build_request

    set_approval_handler(None)
    token = parent_authority_var.set({"tools": ["read_file"], "max_mode": "ask"})
    try:
        req = build_request(
            operation="tool.write_file",
            tool="write_file",
            content="x",
            mode="ask",
            backend="host",
            agent_id="child",
            session_id="sess-s5",
        )
        with pytest.raises(ApprovalDenied, match="outside parent authority"):
            await permission_broker.guard(req)
        # Control: a tool inside the parent's authority is not denied on LP-08.
        ok_req = build_request(
            operation="tool.read_file",
            tool="read_file",
            mode="ask",
            backend="host",
            agent_id="child",
            session_id="sess-s5",
        )
        await permission_broker.guard(ok_req)
    finally:
        parent_authority_var.reset(token)
        set_approval_handler(None)
        clear_execution_context()


async def test_p3_scheduler_script_requires_guard(monkeypatch, tmp_path):
    """P3: a no-agent job script must not run when the broker does not allow it."""
    import uuid as _uuid

    monkeypatch.setenv("MACHINA_SCRIPTS_DIR", str(tmp_path))
    from tests.support.scripts import allow_scripts

    allow_scripts(tmp_path, {"probe.py": []})
    marker = tmp_path / "s5_job_marker"
    (tmp_path / "probe.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    from datetime import UTC, datetime

    from machina.core.scheduler import Job, JobRunner
    from machina.permissions.broker import clear_execution_context, set_approval_handler

    set_approval_handler(None)  # headless: script proposal pauses
    now = datetime.now(UTC)
    job = Job(
        id=_uuid.uuid4(),
        name="s5-probe",
        kind="interval",
        session_id=_uuid.uuid4(),
        agent_name="s5-agent",
        prompt="",
        interval_seconds=60,
        enabled=True,
        status="idle",
        last_run_at=None,
        next_run_at=now,
        last_error=None,
        run_count=0,
        no_agent=True,
        script_path="probe.py",
    )
    runner = JobRunner()
    try:
        with pytest.raises(PermissionError, match="needs_approval"):
            await runner._execute(job)
        assert not marker.exists(), "job script executed without broker approval"
    finally:
        set_approval_handler(None)
        clear_execution_context()
        from machina.core import turns as _turns

        _turns._local_reset_for_tests()


async def test_p5_execute_call_requires_guard_snapshot():
    """P5: MCP dispatch without a guard-issued snapshot is impossible."""
    from machina.connectors.execution import execute_call
    from machina.connectors.models import ConnectorToolError
    from machina.permissions.broker import clear_execution_context

    class _Tool:
        connector_id = "svc"
        remote_name = "remote.echo"

    class _Registry:
        def lookup(self, name):
            return _Tool() if name == "svc.echo" else None

    class _Config:
        owner = ""
        enabled = True
        transport = "stdio"
        call_timeout = 5
        concurrency = 1
        oauth = None

    class _Service:
        registry = _Registry()

        async def refresh(self):
            return None

        def _config(self, connector_id):
            return _Config()

    clear_execution_context()  # no guard snapshot active
    with pytest.raises(ConnectorToolError, match="approved active session"):
        await execute_call(_Service(), "svc.echo", {}, session_id="s", agent_id="a")


async def test_p6_oauth_exchange_blocked_when_guard_denies(monkeypatch, tmp_path):
    """P6: denying the broker guard aborts the OAuth flow before token exchange."""
    import asyncio

    from machina.connectors.models import ConnectorConfig
    from machina.connectors.oauth import OAuthCoordinator, OAuthOwner
    from machina.connectors.oauth_store import OAuthCredentialStore
    from machina.connectors.registry import ConnectorRegistry
    from machina.connectors.service import ConnectorService
    from machina.permissions.broker import ApprovalDenied
    from tests.support.mcp_oauth_fixture import OAuthFixture

    fixture = OAuthFixture()
    await fixture.start()
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
        owner = OAuthOwner(session_id="sess-s5", agent_id="agent-s5")

        async def _deny(req):
            raise ApprovalDenied("s5-deny-test")

        monkeypatch.setattr("machina.permissions.broker.permission_broker.guard", _deny)
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
        # Guard denied before registration/exchange: nothing persisted.
        assert fixture.counters["register"] == 0
        assert fixture.counters["exchange"] == 0
        assert creds._read_index()["grants"] == {}
        auth = await coordinator.status("remote", owner)
        assert auth["auth"]["state"] == "signed_out", auth
    finally:
        await coordinator.shutdown()
        await fixture.stop()
