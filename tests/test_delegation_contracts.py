"""S1 delegation output contracts + backtrack (RED phase).

Falsifiable acceptance:
- contract kwargs accepted by tool and forwarded to orchestrator
- artifact present+non-empty -> complete
- artifact missing -> error naming path + should_write_first called
- empty file -> error; directory -> error
- path escape -> ToolError pre-launch (zero child work)
- no-contract delegation unchanged
- waiting_approval bypasses verification; cancellation propagates without verification
- run AND stream parents covered
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from machina.core.agent_def import AgentDef
from machina.core.models import AgentResponse, Session
from machina.core.orchestrator import Orchestrator


@pytest.fixture
def delegation(monkeypatch, tmp_path):
    # Pin the file-backend workspace to tmp_path for contract resolution.
    import machina.tools.file as filemod
    from machina.core import orchestrator as module
    from machina.core.usage import usage_store

    monkeypatch.setattr(filemod, "_base_dir", lambda: tmp_path)

    child = Session(id=uuid4(), agent_id="coder")
    charges = {}
    recorded = []

    async def summary(session_id, agent_id):
        return {"session": {"chargedTokens": charges.get(session_id, 0)}}

    async def record_end(message_id, **kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(
        module.agent_registry, "get", AsyncMock(return_value=AgentDef(name="coder"))
    )
    monkeypatch.setattr(module.session_manager, "create", AsyncMock(return_value=child))
    monkeypatch.setattr(
        "machina.core.session_mode.resolve_effective_mode", AsyncMock(return_value="ask")
    )
    monkeypatch.setattr(module.context_manager, "add_chunk", AsyncMock())
    monkeypatch.setattr(usage_store, "summary", summary)

    def build(agent):
        orch = Orchestrator(agent_factory=lambda _: agent)
        monkeypatch.setattr(orch, "_record_start", AsyncMock(return_value=uuid4()))
        monkeypatch.setattr(orch, "_record_end", record_end)
        monkeypatch.setattr(orch, "_parent_context", AsyncMock(return_value=""))
        return orch

    return SimpleNamespace(
        child=child, charges=charges, recorded=recorded, build=build, ws=tmp_path
    )


def _ok_child(delegation, content="done"):
    class Child:
        async def run(self, session_id, *_args, **_kwargs):
            delegation.charges[session_id] = 5
            return AgentResponse(content=content, tokens_used=5)

    return Child()


async def test_contract_accepted_and_forwarded(monkeypatch, tmp_path):
    import machina.tools.file as filemod
    from machina.core import orchestrator as orch_module
    from machina.tools import agents as agents_tool

    monkeypatch.setattr(filemod, "_base_dir", lambda: tmp_path)
    calls = []

    async def _fake(agent, task, **kwargs):
        calls.append({"agent": agent, "task": task, **kwargs})
        r = SimpleNamespace(status="complete", response="ok", tokens=0, needs_approval=[])
        return r

    monkeypatch.setattr(orch_module.orchestrator, "delegate", _fake)
    receipt = await agents_tool.delegate(
        "coder",
        "write report",
        expected_output="a one-page summary",
        expected_artifact_path="report.md",
    )
    assert calls and calls[0].get("expected_output") == "a one-page summary"
    assert calls[0].get("expected_artifact_path") == "report.md"
    assert "coder" in receipt


async def test_path_escape_rejected_pre_launch(monkeypatch, tmp_path):
    import machina.tools.file as filemod
    from machina.core import orchestrator as orch_module
    from machina.core.exceptions import ToolError
    from machina.tools import agents as agents_tool

    monkeypatch.setattr(filemod, "_base_dir", lambda: tmp_path)
    orch_delegate = AsyncMock()
    monkeypatch.setattr(orch_module.orchestrator, "delegate", orch_delegate)
    with pytest.raises(ToolError):
        await agents_tool.delegate("coder", "work", expected_artifact_path="../../etc/passwd")
    orch_delegate.assert_not_called()
    with pytest.raises(ToolError):
        await agents_tool.delegate("coder", "work", expected_artifact_path="/etc/passwd")
    orch_delegate.assert_not_called()


async def test_artifact_present_completes(delegation):
    (delegation.ws / "report.md").write_text("hello")
    orch = delegation.build(_ok_child(delegation))
    result = await orch.delegate("coder", "work", expected_artifact_path="report.md")
    assert result.status == "complete"
    assert delegation.recorded[-1]["status"] == "complete"


async def test_artifact_missing_errors_and_backtracks(delegation, monkeypatch):
    import machina.core.progress as progress

    called = []
    real = progress.should_write_first

    def _spy(evidence_count, explicit_ask):
        called.append((evidence_count, explicit_ask))
        return real(evidence_count, explicit_ask)

    monkeypatch.setattr(progress, "should_write_first", _spy)
    orch = delegation.build(_ok_child(delegation))
    result = await orch.delegate("coder", "work", expected_artifact_path="missing.md")
    assert result.status == "error"
    assert "missing.md" in result.response
    assert called, "backtrack helper must be called on verification failure"
    assert delegation.recorded[-1]["status"] == "error"


async def test_empty_file_errors(delegation, monkeypatch):
    import machina.core.progress as progress

    monkeypatch.setattr(progress, "should_write_first", MagicMock(return_value=True))
    (delegation.ws / "empty.md").write_text("")
    orch = delegation.build(_ok_child(delegation))
    result = await orch.delegate("coder", "work", expected_artifact_path="empty.md")
    assert result.status == "error"
    assert "empty.md" in result.response


async def test_directory_artifact_errors(delegation, monkeypatch):
    import machina.core.progress as progress

    monkeypatch.setattr(progress, "should_write_first", MagicMock(return_value=True))
    (delegation.ws / "adir").mkdir()
    orch = delegation.build(_ok_child(delegation))
    result = await orch.delegate("coder", "work", expected_artifact_path="adir")
    assert result.status == "error"
    assert "adir" in result.response


async def test_no_contract_unchanged(delegation, monkeypatch):
    """Without new kwargs, missing artifact must not fail (zero behavior change)."""
    import machina.core.progress as progress

    spy = MagicMock(return_value=True)
    monkeypatch.setattr(progress, "should_write_first", spy)
    orch = delegation.build(_ok_child(delegation, content="all done"))
    result = await orch.delegate("coder", "work")
    assert result.status == "complete"
    assert result.response == "all done"
    spy.assert_not_called()


async def test_waiting_approval_bypasses_verification(delegation, monkeypatch):
    import machina.core.progress as progress

    spy = MagicMock(return_value=True)
    monkeypatch.setattr(progress, "should_write_first", spy)
    pause = {
        "request_id": "r1",
        "session_id": "s",
        "agent_id": "coder",
        "operation": "tool.terminal",
        "target": "ls",
        "status": "pending",
    }

    class Child:
        async def run(self, session_id, *_args, **_kwargs):
            delegation.charges[session_id] = 5
            return AgentResponse(content="Waiting for approval.", needs_approval=[pause])

    result = await delegation.build(Child()).delegate(
        "coder", "work", expected_artifact_path="never-written.md"
    )
    assert result.status == "waiting_approval"
    spy.assert_not_called()


async def test_cancelled_mid_write_propagates_without_verification(
    delegation, monkeypatch
):
    import asyncio

    import machina.core.progress as progress

    spy = MagicMock(return_value=True)
    monkeypatch.setattr(progress, "should_write_first", spy)

    class Child:
        async def run(self, session_id, *_args, **_kwargs):
            delegation.charges[session_id] = 5
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await delegation.build(Child()).delegate(
            "coder", "work", expected_artifact_path="x.md"
        )
    spy.assert_not_called()


async def test_absolute_inside_workspace_ok(delegation):
    target = delegation.ws / "sub" / "abs.md"
    target.parent.mkdir(parents=True)
    target.write_text("data")
    orch = delegation.build(_ok_child(delegation))
    result = await orch.delegate("coder", "work", expected_artifact_path=str(target))
    assert result.status == "complete"


async def test_orchestrator_direct_escape_is_error_not_raise(delegation, monkeypatch):
    import machina.core.progress as progress

    monkeypatch.setattr(progress, "should_write_first", MagicMock(return_value=True))
    orch = delegation.build(_ok_child(delegation))
    result = await orch.delegate("coder", "work", expected_artifact_path="/etc/passwd")
    assert result.status == "error"
    assert "/etc/passwd" in result.response


@pytest.mark.parametrize("streaming", [False, True])
async def test_contract_in_run_and_stream_parents(delegation, monkeypatch, streaming, tmp_path):
    """Delegate tool with contract inside real parent run/stream loop."""
    from pathlib import Path

    import machina.tools.file as filemod
    from machina.core import orchestrator as module
    from machina.core.agent import ReActAgent, registry
    from machina.core.agent_def import agent_registry
    from machina.core.execution_context import ExecutionContext
    from machina.core.models import LLMResponse, StreamEvent
    from machina.permissions.broker import permission_broker

    monkeypatch.setattr(filemod, "_base_dir", lambda: tmp_path)
    (tmp_path / "out.md").write_text("contract artifact")

    parent = Session(id=uuid4())
    agent = ReActAgent(provider=SimpleNamespace(model="fake"), allowed_tools=["delegate"])
    orch = delegation.build(_ok_child(delegation))
    # Point the orchestrator's workspace at tmp_path too (it uses filemod._base_dir
    # via resolve_path, already patched). Reuse its child but verify via tmp file.
    real_delegate = orch.delegate

    async def _delegate_with_ws(agent_name, task, **kwargs):
        kwargs.setdefault("expected_artifact_path", "out.md")
        # Temporarily ensure resolve sees tmp_path (already patched globally).
        return await real_delegate(agent_name, task, **kwargs)

    monkeypatch.setattr(module, "orchestrator", SimpleNamespace(delegate=_delegate_with_ws))
    monkeypatch.setattr(
        agent_registry, "list", AsyncMock(return_value=[AgentDef(name="coder")])
    )
    monkeypatch.setattr("machina.tools.agents.db.fetchval", AsyncMock(return_value="harness"))
    monkeypatch.setattr(agent, "_prepare_context", AsyncMock(return_value=(parent, [])))
    monkeypatch.setattr(agent, "_flush_pending_chunks", AsyncMock())
    monkeypatch.setattr("machina.core.agent.session_manager.update_activity", AsyncMock())
    monkeypatch.setattr(agent, "_schedule_memory_consolidation", lambda *_: None)
    monkeypatch.setattr(agent, "_schedule_learning_review", lambda *_: None)
    monkeypatch.setattr(permission_broker, "guard", AsyncMock())
    monkeypatch.setattr(
        "machina.core.execution_context.resolve_execution_context",
        AsyncMock(return_value=ExecutionContext("ask", "host", Path("/tmp"))),
    )

    async def execute(name, **kwargs):
        assert name == "delegate"
        # Route through the real tool but with the orchestrator stub above.
        # The tool's own workspace check uses tmp_path (patched).
        from machina.core import orchestrator as m

        # Call tool's underlying function with contract args directly.
        assert kwargs["agent"] == "coder"
        result = await m.orchestrator.delegate(
            kwargs["agent"], kwargs["task"], **{k: v for k, v in kwargs.items() if k not in ("agent", "task")}
        )
        from machina.tools.agents import DelegationToolResult

        label = "responded" if result.status == "complete" else "failed"
        receipt = DelegationToolResult(
            f"[coder {label}]\n{result.response}",
            needs_approval=getattr(result, "needs_approval", []),
            tokens_used=getattr(result, "tokens", 0),
        )
        receipt.is_error = label == "failed"
        return receipt

    monkeypatch.setattr(registry, "execute", execute)
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        if len(calls) > 1:
            return LLMResponse(content="continued", model="fake", usage={"total_tokens": 1})
        return LLMResponse(
            content="",
            model="fake",
            usage={"total_tokens": 1},
            tool_calls=[
                {
                    "id": "call",
                    "type": "function",
                    "function": {
                        "name": "delegate",
                        "arguments": '{"agent":"coder","task":"work","expected_artifact_path":"out.md"}',
                    },
                }
            ],
        )

    async def stream(**kwargs):
        yield StreamEvent(type="done", response=await complete(**kwargs))

    monkeypatch.setattr(agent, "_call_llm_with_retry", complete)
    monkeypatch.setattr(agent, "_stream_llm_with_retry", stream)
    if streaming:
        events = [
            event
            async for event in ReActAgent.run_stream.__wrapped__(agent, parent.id, "work", False)
        ]
        response = events[-1].response
    else:
        response = await ReActAgent.run.__wrapped__(agent, parent.id, "work", False)
    assert len(calls) == 2
    assert response.tool_calls[0]["outcome"] != "failed"
