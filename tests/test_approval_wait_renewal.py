"""Claims renew throughout human waits and fail closed on the first loss."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from ah.core.models import AgentResponse, Session, StreamEvent
from ah.gateway import server
from ah.permissions.broker import _approval_handler


@pytest.mark.parametrize("outcome", ["approve", "cancel", "loss"])
async def test_approval_wait_renews_and_stops_owned_work(monkeypatch, outcome):
    from ah import services
    from ah.core import turns
    from ah.core.session import session_manager

    frames, effects, renewals = [], [], []
    pending, twice = asyncio.Event(), asyncio.Event()
    session = Session(id=uuid.uuid4())
    monkeypatch.setattr(server, "CLAIM_RENEW_INTERVAL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())
    end = AsyncMock(return_value=True)
    monkeypatch.setattr(turns, "end_turn", end)
    monkeypatch.setattr(turns, "owns_claim", AsyncMock(return_value=True))

    async def renew(sid, token):
        assert sid == session.id and token == "approval-owner"
        renewals.append(token)
        if len(renewals) >= 2:
            twice.set()
        return outcome != "loss"

    monkeypatch.setattr(turns, "renew_turn", renew)

    class Agent:
        async def run_stream(self, *args, **kwargs):
            verdict = await _approval_handler.get()(
                {"request_id": "wait-request", "operation": "file.write", "targets": []}
            )
            if verdict == "approved":
                effects.append("authorized effect")
            yield StreamEvent(type="done", response=AgentResponse(content="finished"))

    def write(frame):
        frames.append(frame["params"])
        if frame["params"]["type"] == "permission.required":
            pending.set()

    gateway = server.Gateway(write, agent_factory=lambda *args: Agent(), owns_db=False)
    gateway._turn_tokens[str(session.id)] = "approval-owner"
    turn = asyncio.create_task(gateway._run_turn(session.id, "renewal-turn", "work"))
    gateway._turns[str(session.id)] = turn
    try:
        await asyncio.wait_for(pending.wait(), 0.5)
        if outcome == "loss":
            done, _ = await asyncio.wait({turn}, timeout=0.5)
            assert turn in done, "lost renewal left approval work active"
            assert any(event["type"] == "turn.ownership_lost" for event in frames)
        else:
            await asyncio.wait_for(twice.wait(), 0.5)
            assert not turn.done(), "a human wait must keep the claim alive"
            if outcome == "cancel":
                turn.cancel()
            else:
                future = gateway._pending_approvals["wait-request"][0]
                future.set_result("approved")
        await asyncio.gather(turn, return_exceptions=True)
        assert effects == (["authorized effect"] if outcome == "approve" else [])
        assert not gateway._pending_approvals
        end.assert_awaited_once_with(session.id, "approval-owner")
        count = len(renewals)
        await asyncio.sleep(0.03)
        assert len(renewals) == count, "renewal leaked after turn release"
    finally:
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
