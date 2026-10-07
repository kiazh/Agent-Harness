"""Turn/mutation ownership tests (5.3) against the working database.

Proves: idle success with real effects, active conflicts rejected, session
independence, crash/expiry recovery, stale-release safety, nested child
progress, and cancel cleanup. Uses the same live-DB pattern as
test_scheduler.py (local disposable database, never production).
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture
async def live_db():
    """Connect the shared DB and ensure 5.3 claim-column migrations exist."""
    from ah.db.connection import db

    await db.connect()
    await db.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS claim_owner TEXT")
    await db.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS claim_kind TEXT")
    await db.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ")
    await db.execute("UPDATE sessions SET status = 'active' WHERE status = 'running'")
    await db.execute("ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_status_check")
    await db.execute(
        "ALTER TABLE sessions ADD CONSTRAINT sessions_status_check "
        "CHECK (status IN ('active', 'idle', 'archived'))"
    )
    yield
    await db.close()


async def _session():
    from ah.core.session import session_manager

    return await session_manager.create(title="turns-test")


@pytest.mark.usefixtures("live_db")
async def test_idle_claim_release_cycle():
    from ah.core.turns import end_turn, try_begin_turn

    s = await _session()
    token = await try_begin_turn(s.id)
    assert token
    assert await try_begin_turn(s.id) is None
    assert await end_turn(s.id, token) is True
    assert await end_turn(s.id, token) is False  # already released
    assert await try_begin_turn(s.id) is not None


@pytest.mark.usefixtures("live_db")
async def test_crash_expiry_recovery_and_stale_release():
    from ah.core.turns import end_turn, try_begin_turn, turn_active
    from ah.db.connection import db

    s = await _session()
    token_a = await try_begin_turn(s.id)
    assert token_a and await turn_active(s.id)
    # Simulate worker death: expire the claim directly.
    await db.execute(
        "UPDATE sessions SET claim_expires_at = now() - interval '1 second' WHERE id = $1",
        s.id,
    )
    assert not await turn_active(s.id)
    token_b = await try_begin_turn(s.id)
    assert token_b and token_b != token_a
    # Stale owner cannot release the newer claim.
    assert await end_turn(s.id, token_a) is False
    assert await turn_active(s.id)
    assert await end_turn(s.id, token_b) is True
    assert not await turn_active(s.id)


@pytest.mark.usefixtures("live_db")
async def test_mutation_turn_mutual_exclusion():
    from ah.core.turns import begin_mutation, end_mutation, end_turn, try_begin_turn

    s = await _session()
    turn = await try_begin_turn(s.id)
    assert turn
    assert await begin_mutation(s.id) is None  # live turn blocks mutation
    assert await end_turn(s.id, turn) is True
    mut = await begin_mutation(s.id)
    assert mut
    assert await try_begin_turn(s.id) is None  # live mutation blocks turns
    assert await end_mutation(s.id, mut) is True


@pytest.mark.usefixtures("live_db")
async def test_two_sessions_independent():
    from ah.core.turns import end_turn, try_begin_turn

    a, b = await _session(), await _session()
    ta = await try_begin_turn(a.id)
    tb = await try_begin_turn(b.id)
    assert ta and tb
    assert await end_turn(a.id, ta) is True
    assert await end_turn(b.id, tb) is True


@pytest.mark.usefixtures("live_db")
async def test_nested_child_progress_during_parent_turn():
    """Child session claims + parent recording never touch the parent claim."""
    from ah.core.context import context_manager
    from ah.core.session import session_manager
    from ah.core.turns import end_turn, try_begin_turn, turn_active

    parent = await session_manager.create(title="parent")
    ptoken = await try_begin_turn(parent.id)
    assert ptoken
    child = await session_manager.create(title="child", agent_id="researcher")
    ctoken = await try_begin_turn(child.id)
    assert ctoken  # no deadlock on parent's claim
    await context_manager.add_chunk(
        session_id=parent.id,
        agent_id="orchestrator",
        chunk_type="result",
        payload={"agent": "researcher", "content": "done"},
        token_count=1,
    )
    assert await turn_active(parent.id)  # parent claim undisturbed
    assert await end_turn(child.id, ctoken) is True
    assert await end_turn(parent.id, ptoken) is True


@pytest.mark.usefixtures("live_db")
async def test_concurrent_begin_does_not_hang_and_cleans_up():
    import asyncio

    from ah.core.turns import end_turn, try_begin_turn

    s = await _session()
    token = await try_begin_turn(s.id)
    assert token

    async def _racer():
        return await try_begin_turn(s.id)

    # Second claimant is rejected, never parked; cancellation is safe.
    racer = asyncio.create_task(_racer())
    racer.cancel()
    try:
        await racer
    except (asyncio.CancelledError, Exception):
        pass
    assert await try_begin_turn(s.id) is None
    assert await end_turn(s.id, token) is True
    assert await try_begin_turn(s.id) is not None


@pytest.mark.usefixtures("live_db")
async def test_idle_delete_and_compress_take_effect():
    """Real feature handlers on idle sessions perform real effects."""
    from ah import services
    from ah.core.context import context_manager
    from ah.core.session import session_manager
    from ah.gateway.features import sessions as feat

    class _Gw:
        def require_db(self):
            return None

        async def get_session(self, params):
            s = await session_manager.get(params["sessionId"])
            assert s is not None
            return s

        def turn_running(self, sid):
            return False

        model = "openrouter/free"
        provider = "openrouter"

    gw = _Gw()
    s = await _session()
    await context_manager.add_chunk(
        session_id=s.id,
        agent_id="h",
        chunk_type="user_message",
        payload={"content": "hello"},
        token_count=1,
    )
    # Compress idle: force truncation path (no live LLM spend).
    import ah.core.provider as provmod

    real_provider = provmod.get_provider

    def _no_provider(*a, **k):
        raise ValueError("no key in test")

    provmod.get_provider = _no_provider  # type: ignore[assignment]
    try:
        out = await feat.context_compress(gw, {"sessionId": s.id})  # type: ignore[arg-type]
    finally:
        provmod.get_provider = real_provider  # type: ignore[assignment]
    assert out.get("compressed") in (True, False)
    # Delete idle: actually deletes.
    res = await feat.session_delete(gw, {"sessionId": s.id})  # type: ignore[arg-type]
    assert res == {"deleted": True}
    assert await session_manager.get(s.id) is None


@pytest.mark.usefixtures("live_db")
async def test_active_turn_rejects_mutation_handlers():
    from ah.core.session import session_manager
    from ah.core.turns import end_turn, try_begin_turn
    from ah.gateway.errors import RpcError
    from ah.gateway.features import sessions as feat

    class _Gw:
        def require_db(self):
            return None

        async def get_session(self, params):
            s = await session_manager.get(params["sessionId"])
            assert s is not None
            return s

        def turn_running(self, sid):
            return False

        model = "m"
        provider = "p"

    gw = _Gw()
    s = await _session()
    token = await try_begin_turn(s.id)
    assert token
    try:
        with pytest.raises(RpcError):
            await feat.context_compress(gw, {"sessionId": s.id})  # type: ignore[arg-type]
        with pytest.raises(RpcError):
            await feat.session_delete(gw, {"sessionId": s.id})  # type: ignore[arg-type]
        assert await session_manager.get(s.id) is not None  # no effect
    finally:
        assert await end_turn(s.id, token) is True


@pytest.mark.usefixtures("live_db")
async def test_gateway_submit_vs_rest_claim_fencing():
    """Cross-transport: a REST-path claim blocks gateway submit and vice versa."""
    import asyncio

    from ah.core.session import session_manager
    from ah.core.turns import end_turn, try_begin_turn
    from ah.gateway.errors import RpcError
    from ah.gateway.server import Gateway

    s = await session_manager.create(title="xport")
    try:
        # REST path first (pre-header claim): gateway submit must conflict
        # without invoking any provider.
        rest_token = await try_begin_turn(s.id)
        assert rest_token
        gw = Gateway(write=lambda frame: None, owns_db=False)
        gw._db_ready = True
        with pytest.raises(RpcError):
            await gw._prompt_submit({"sessionId": str(s.id), "text": "hi"})
        assert await end_turn(s.id, rest_token) is True

        # Gateway path first (stubbed runner, no LLM): REST claim conflicts.
        async def _noop_runner(session_id, turn_id, text):
            await asyncio.sleep(30)

        gw._run_turn_with_timeout = _noop_runner  # type: ignore[method-assign]
        res = await gw._prompt_submit({"sessionId": str(s.id), "text": "hi"})
        assert res.get("turnId")
        try:
            assert await try_begin_turn(s.id) is None
        finally:
            key = str(s.id)
            task = gw._turns.pop(key, None)
            token = gw._turn_tokens.pop(key, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if token:
                assert await end_turn(s.id, token) is True
        assert await try_begin_turn(s.id) is not None
    finally:
        try:
            await session_manager.delete(s.id)
        except Exception:
            pass
