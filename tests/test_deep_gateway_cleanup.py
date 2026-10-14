"""Gateway factory injection and optional work preserve the turn contract."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from machina.core.models import AgentResponse, Session, StreamEvent
from machina.gateway.server import Gateway


async def test_gateway_honors_explicit_factory_with_stored_session_settings(monkeypatch):
    import machina.core.agent_factory as factory
    from machina import services
    from machina.core.session import session_manager

    session = Session(id=uuid.uuid4(), model="saved-model", provider="saved-provider")
    frames, calls = [], []

    class Agent:
        async def run_stream(self, *_args, **_kwargs):
            yield StreamEvent(type="done", response=AgentResponse(content="injected answer"))

    def injected(model, provider):
        calls.append((model, provider))
        return Agent()

    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(
        factory,
        "build_agent_for_session",
        AsyncMock(side_effect=RuntimeError("unexpected default")),
    )
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    gateway = Gateway(frames.append, agent_factory=injected, owns_db=False)
    await gateway._run_turn(session.id, "injected-turn", "hello")
    completions = [f["params"] for f in frames if f["params"]["type"] == "message.complete"]
    assert calls == [("saved-model", "saved-provider")]
    assert len(completions) == 1
    assert completions[0]["text"] == "injected answer"


async def test_gateway_completion_does_not_wait_for_resistant_learning(monkeypatch):
    import machina.core.agent_factory as factory
    from machina import services
    from machina.core.session import session_manager

    started, release = asyncio.Event(), asyncio.Event()

    async def learning():
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    worker = asyncio.create_task(learning())
    await started.wait()
    session = Session(id=uuid.uuid4())
    frames = []

    class Agent:
        _learning_tasks = {worker}

        async def run_stream(self, *_args, **_kwargs):
            yield StreamEvent(type="done", response=AgentResponse(content="finished"))

    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(factory, "build_agent_for_session", AsyncMock(return_value=Agent()))
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    gateway = Gateway(frames.append, owns_db=False)
    turn = asyncio.create_task(gateway._run_turn(session.id, "learning-turn", "hello"))
    try:
        done, _ = await asyncio.wait({turn}, timeout=2.5)
        assert turn in done, "optional learning retained the turn past its cleanup deadline"
        await turn
        assert any(
            f["params"]["type"] == "message.complete" and f["params"]["text"] == "finished"
            for f in frames
        )
    finally:
        release.set()
        await asyncio.gather(worker, turn, return_exceptions=True)


async def test_late_turn_cleanup_cannot_release_replacement_turn(monkeypatch):
    import machina.core.agent_factory as factory
    from machina import services
    from machina.core import turns
    from machina.core.session import session_manager

    started, release = asyncio.Event(), asyncio.Event()
    session = Session(id=uuid.uuid4())
    released = []

    class Agent:
        async def run_stream(self, *_args, **_kwargs):
            started.set()
            await release.wait()
            yield StreamEvent(type="done", response=AgentResponse(content="old answer"))

    async def end_turn(session_id, token):
        released.append((session_id, token))
        return True

    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(factory, "build_agent_for_session", AsyncMock(return_value=Agent()))
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    monkeypatch.setattr(turns, "end_turn", end_turn)
    gateway = Gateway(lambda _frame: None, owns_db=False)
    sid = str(session.id)
    gateway._turn_tokens[sid] = "old-owner"
    turn = asyncio.create_task(gateway._run_turn(session.id, "old-turn", "hello"))
    await started.wait()
    gateway._turn_tokens[sid] = "replacement-owner"
    release.set()
    await turn
    assert released == [(session.id, "old-owner")]
    assert gateway._turn_tokens[sid] == "replacement-owner"


# ─── W7 batch 2: gateway error-boundary consistency ─────────────────────────


async def test_approval_renew_import_failure_records_diagnostic(monkeypatch, caplog):
    """W7: a failed approval-wait renewal import is diagnosed, not silent."""
    import logging

    from machina import services
    from machina.core import turns
    from machina.core.metrics import metrics
    from machina.core.session import session_manager
    from machina.permissions.broker import _approval_handler

    session = Session(id=uuid.uuid4())
    frames = []

    class Agent:
        async def run_stream(self, *_args, **_kwargs):
            verdict = await _approval_handler.get()(
                {"request_id": "renew-import", "operation": "file.write", "targets": []}
            )
            yield StreamEvent(type="done", response=AgentResponse(content=f"verdict:{verdict}"))

    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    monkeypatch.setattr(turns, "end_turn", AsyncMock(return_value=True))
    # The renewal import inside the approval wait fails (removed attribute).
    monkeypatch.delattr(turns, "renew_turn")

    gateway = Gateway(frames.append, agent_factory=lambda *args, **kwargs: Agent(), owns_db=False)
    sid = str(session.id)
    gateway._turn_tokens[sid] = "approval-owner"
    before = metrics.get_counter("diagnostic.failures")
    with caplog.at_level(logging.WARNING, logger="machina.observability.diagnostics"):
        turn = asyncio.create_task(gateway._run_turn(session.id, "renew-import-turn", "work"))
        gateway._turns[sid] = turn
        try:
            deadline = asyncio.get_running_loop().time() + 2
            while "renew-import" not in gateway._pending_approvals:
                assert asyncio.get_running_loop().time() < deadline, "approval wait never started"
                await asyncio.sleep(0.01)
            gateway._pending_approvals["renew-import"][0].set_result("approved")
            await asyncio.wait_for(turn, 2)
        finally:
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)

    assert metrics.get_counter("diagnostic.failures") >= before + 1
    assert any(
        "boundary failed: server.approval_renew" in record.getMessage() for record in caplog.records
    ), "renewal-import failure must surface through record_failure"


async def test_close_propagates_cancellation_while_stopping_job_runner():
    """W7: close() cancellation during the job-runner join is not absorbed."""

    class StuckRunner:
        def __init__(self):
            self.started = asyncio.Event()

        async def stop(self):
            self.started.set()
            await asyncio.sleep(3600)

    gateway = Gateway(lambda _frame: None, owns_db=False)
    runner = StuckRunner()
    gateway._job_runner = runner
    close = asyncio.create_task(gateway.close(timeout=30))
    await asyncio.wait_for(runner.started.wait(), 1)
    close.cancel()
    done, _ = await asyncio.wait({close}, timeout=1)
    assert close in done and close.cancelled(), (
        "shutdown cancellation must propagate through close()"
    )
    with pytest.raises(asyncio.CancelledError):
        await close


async def test_close_absorbs_bounded_job_runner_stop_timeout():
    """W7: a bounded stop timeout still continues close() (behavior kept)."""

    class SlowRunner:
        def __init__(self):
            self.cancelled = asyncio.Event()

        async def stop(self):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    gateway = Gateway(lambda _frame: None, owns_db=False)
    runner = SlowRunner()
    gateway._job_runner = runner
    await gateway.close(timeout=0.05)
    assert gateway._job_runner is None
    await asyncio.wait_for(runner.cancelled.wait(), 1)


async def test_shutdown_cancellation_propagates_through_resistant_renewal_join(monkeypatch):
    """W7: cancelling a turn mid-teardown is not absorbed by the renewal join."""
    import machina.core.agent_factory as factory
    from machina import services
    from machina.core import turns
    from machina.core.session import session_manager

    release = asyncio.Event()
    renewing = asyncio.Event()
    renewal_tasks: list[asyncio.Task] = []
    end_calls: list = []
    frames = []

    async def resistant_renewal_loop(*_args, **_kwargs):
        renewal_tasks.append(asyncio.current_task())
        renewing.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    session = Session(id=uuid.uuid4())

    class Agent:
        async def run_stream(self, *_args, **_kwargs):
            # Yield so the renewal task starts before the turn's cleanup
            # cancels it (mock-only paths otherwise never leave the loop).
            await asyncio.sleep(0)
            yield StreamEvent(type="done", response=AgentResponse(content="finished"))

    async def end_turn(session_id, token):
        end_calls.append(token)
        return True

    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(factory, "build_agent_for_session", AsyncMock(return_value=Agent()))
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    monkeypatch.setattr(turns, "renewal_loop", resistant_renewal_loop)
    monkeypatch.setattr(turns, "end_turn", end_turn)
    gateway = Gateway(frames.append, owns_db=False)
    sid = str(session.id)
    gateway._turn_tokens[sid] = "shutdown-owner"

    turn = asyncio.create_task(gateway._run_turn(session.id, "shutdown-turn", "hello"))
    gateway._turns[sid] = turn
    try:
        await asyncio.wait_for(renewing.wait(), 1)
        deadline = asyncio.get_running_loop().time() + 2
        while not end_calls:
            assert asyncio.get_running_loop().time() < deadline, "claim release never ran"
            await asyncio.sleep(0.01)
        # The turn is now in its finally block joining the (cancellation-
        # resistant) renewal task; a shutdown cancellation must propagate.
        await asyncio.sleep(0.05)
        turn.cancel()
        done, _ = await asyncio.wait({turn}, timeout=1)
        assert turn in done and turn.cancelled(), (
            "shutdown cancellation was absorbed by the renewal join"
        )
    finally:
        release.set()
        for task in renewal_tasks:
            if task is not None:
                task.cancel()
        await asyncio.gather(*renewal_tasks, turn, return_exceptions=True)
