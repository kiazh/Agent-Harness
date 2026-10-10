"""Human approval waits pause turn compute budgets, including active waits."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ah.core.models import AgentResponse, Session, StreamEvent
from ah.gateway.server import Gateway


class _AccountingAsyncio:
    """Advance gateway accounting time at event barriers without changing loop time."""

    def __init__(self, approvals_ready, resumed, compute_after_approval):
        real_loop = asyncio.get_running_loop()
        self.now = 0.0
        self.loop = SimpleNamespace(time=lambda: self.now, create_future=real_loop.create_future)
        self.approvals_ready = approvals_ready
        self.resumed = resumed
        self.compute_after_approval = compute_after_approval
        self.pending_poll = asyncio.Event()
        self.extra_poll = asyncio.Event()
        self.polls = 0
        self.inner = None

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def get_running_loop(self):
        return self.loop

    def create_task(self, coroutine, *args, **kwargs):
        task = asyncio.create_task(coroutine, *args, **kwargs)
        if coroutine.cr_code.co_name == "_run_turn":
            self.inner = task
        return task

    async def wait(self, tasks, *, timeout, **kwargs):
        if timeout != 0.2:
            return await asyncio.wait(tasks, timeout=timeout, **kwargs)
        self.polls += 1
        if self.polls == 1:
            await self.approvals_ready.wait()
            self.now = 100.0
            return set(), set(tasks)
        if self.polls == 2:
            self.pending_poll.set()
            await self.resumed.wait()
            self.now += self.compute_after_approval
            await asyncio.sleep(0)
            return set(), set(tasks)
        self.extra_poll.set()
        return await asyncio.wait(tasks, timeout=0.01, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval_count", "compute_after_approval", "timed_out"),
    [(1, 0.25, False), (1, 1.25, True), (2, 1.25, True), (1, 0.25, None)],
)
async def test_approval_waits_preserve_the_compute_budget(
    monkeypatch, approval_count, compute_after_approval, timed_out
):
    from ah import services
    from ah.core.config import config
    from ah.core.session import session_manager
    from ah.gateway import server
    from ah.permissions.broker import _approval_handler

    session = Session(id=uuid.uuid4())
    frames = []
    approvals_ready = asyncio.Event()
    resumed = asyncio.Event()
    stop_compute = asyncio.Event()
    clock = _AccountingAsyncio(approvals_ready, resumed, compute_after_approval)
    monkeypatch.setattr(server, "asyncio", clock)
    monkeypatch.setattr(config, "turn_timeout", 1)
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())

    def write(frame):
        frames.append(frame)
        if sum(f["params"]["type"] == "permission.required" for f in frames) == approval_count:
            approvals_ready.set()

    class Agent:
        async def run_stream(self, *_args, **_kwargs):
            handler = _approval_handler.get()
            await asyncio.gather(
                *(
                    handler(
                        {"request_id": f"approval-{index}", "operation": "file.read", "targets": []}
                    )
                    for index in range(approval_count)
                )
            )
            resumed.set()
            if timed_out:
                await stop_compute.wait()
            yield StreamEvent(type="done", response=AgentResponse(content="approved answer"))

    gateway = Gateway(write, agent_factory=lambda *_args: Agent(), owns_db=False)
    turn_id = "approval-budget-turn"
    sid = str(session.id)
    turn = asyncio.create_task(gateway._run_turn_with_timeout(session.id, turn_id, "work"))
    gateway._turns[sid] = turn
    pending_poll = asyncio.create_task(clock.pending_poll.wait())
    extra_poll = None
    try:
        done, _ = await asyncio.wait(
            {turn, pending_poll}, timeout=1, return_when=asyncio.FIRST_COMPLETED
        )
        assert pending_poll in done, "compute deadline fired while human approval was pending"
        assert not turn.done()
        if timed_out is None:
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn
            await asyncio.wait_for(asyncio.shield(clock.inner), 1)
        else:
            for future, owner_turn, _session in gateway._pending_approvals.values():
                assert owner_turn == turn_id
                future.set_result("approved")
        if timed_out is True:
            extra_poll = asyncio.create_task(clock.extra_poll.wait())
            done, _ = await asyncio.wait(
                {turn, extra_poll}, timeout=1, return_when=asyncio.FIRST_COMPLETED
            )
            assert turn in done, "resumed compute exhausted its budget without timing out"
        elif timed_out is False:
            await asyncio.wait_for(asyncio.shield(turn), 1)
        if timed_out is not None:
            await turn
        events = [frame["params"] for frame in frames]
        completions = [event for event in events if event["type"] == "message.complete"]
        assert len(completions) == 1
        assert completions[0]["cancelled"] is (timed_out is not False)
        errors = [event["message"] for event in events if event["type"] == "error"]
        assert errors == (["turn timed out"] if timed_out else [])
        if timed_out is False:
            assert completions[0]["text"] == "approved answer"
        assert not gateway._pending_approvals
        assert turn_id not in gateway._approval_wait_total
        assert turn_id not in gateway._approval_wait_started
    finally:
        stop_compute.set()
        for task in (turn, pending_poll, extra_poll):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (turn, pending_poll, extra_poll) if task is not None),
            return_exceptions=True,
        )
        inner = clock.inner
        if inner is not None and not inner.done():
            inner.cancel()
            await asyncio.gather(inner, return_exceptions=True)
