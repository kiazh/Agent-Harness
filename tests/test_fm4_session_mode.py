"""FM4 m1: turn-scope snapshots must never serve stale modes to child tasks.

RED before fix: the turn-scope ContextVar held one mutable dict shared by
reference with task contexts copied at ``create_task`` time. A child task
born inside a turn kept a stale mode after the turn ended and the mode was
changed (invalidation only popped the *caller's* context).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest


class ModeStubDb:
    """Mocked durable mode authority with a fetch counter."""

    connected = True

    def __init__(self, mode: str = "workspace"):
        self.mode = mode
        self.fetchrows = 0

    async def fetchrow(self, query, _sid):
        self.fetchrows += 1
        return {"execution_mode": self.mode}


@pytest.fixture
def clean_turn():
    from machina.core import session_mode

    session_mode.reset_for_tests()
    try:
        yield
    finally:
        session_mode.reset_for_tests()


async def test_child_task_cannot_read_stale_mode_after_invalidation(clean_turn, monkeypatch):
    from machina.core import session_mode

    sid = str(uuid.uuid4())
    stub = ModeStubDb("workspace")
    monkeypatch.setattr("machina.db.connection.db", stub)

    gate = asyncio.Event()

    async def probe():
        await gate.wait()
        return await session_mode.resolve_effective_mode(sid)

    token = session_mode.begin_turn_scope()
    try:
        assert await session_mode.resolve_effective_mode(sid) == "workspace"
        child = asyncio.create_task(probe())
    finally:
        session_mode.end_turn_scope(token)

    # The turn is over; authority and the routing override change while the
    # child task (born inside the turn) is still alive.
    stub.mode = "full"
    session_mode.set_session_mode(sid, "full")
    gate.set()
    try:
        assert await asyncio.wait_for(child, 1) == "full"
    finally:
        session_mode.reset_for_tests()
    assert stub.fetchrows == 2, "the child must re-resolve after invalidation"


async def test_child_task_inside_turn_shares_snapshot(clean_turn, monkeypatch):
    """The fix must not disable caching inside a live turn."""
    from machina.core import session_mode

    sid = str(uuid.uuid4())
    stub = ModeStubDb("workspace")
    monkeypatch.setattr("machina.db.connection.db", stub)

    async def probe():
        return await session_mode.resolve_effective_mode(sid)

    with session_mode.turn_scope():
        assert await session_mode.resolve_effective_mode(sid) == "workspace"
        child = asyncio.create_task(probe())
        assert await child == "workspace"
    assert stub.fetchrows == 1


async def test_peek_turn_mode_follows_invalidation(clean_turn, monkeypatch):
    from machina.core import session_mode

    sid = str(uuid.uuid4())
    stub = ModeStubDb("sandbox")
    monkeypatch.setattr("machina.db.connection.db", stub)
    with session_mode.turn_scope():
        assert await session_mode.resolve_effective_mode(sid) == "sandbox"
        assert session_mode.peek_turn_mode(sid) == "sandbox"
        session_mode.set_session_mode(sid, "full")
        assert session_mode.peek_turn_mode(sid) is None
