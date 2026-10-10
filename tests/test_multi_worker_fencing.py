"""Independent OS processes share only durable PostgreSQL ownership."""

import asyncio
import uuid

import pytest

from tests.support.processes import FencingWorker
from tests.support.schema import isolated_schema

pytestmark = [pytest.mark.integration, pytest.mark.db]


@pytest.fixture
async def workers(db_pool, monkeypatch):
    async with isolated_schema(db_pool) as (schema, database):
        monkeypatch.setattr("ah.db.connection.db", database)
        monkeypatch.setattr("ah.core.session.db", database)
        monkeypatch.setattr("ah.core.scheduler.db", database)
        from ah.core.session import SessionManager

        session = await SessionManager().create(title="independent workers")
        processes = []
        try:
            for _ in range(2):
                processes.append(await FencingWorker.start(schema))
            yield database, str(session.id), processes
        finally:
            await asyncio.gather(*(worker.close() for worker in processes))


@pytest.mark.parametrize("operation", ["gateway", "http", "mutation"])
async def test_two_processes_cannot_claim_same_session(workers, operation):
    database, sid, processes = workers
    results = await asyncio.gather(
        *(worker.call(operation, session_id=sid) for worker in processes)
    )
    assert sorted(result["acquired"] for result in results) == [False, True]
    current = await database.fetchval(
        "SELECT claim_owner FROM sessions WHERE id = $1", uuid.UUID(sid)
    )
    assert current == next(result["token"] for result in results if result["acquired"])


async def test_expired_owner_cannot_release_successor(workers):
    database, sid, (first, second) = workers
    old = await first.call("turn", session_id=sid)
    await database.execute(
        "UPDATE sessions SET claim_expires_at = now() - interval '1 second' WHERE id = $1",
        uuid.UUID(sid),
    )
    new = await second.call("turn", session_id=sid)
    assert old["acquired"] and new["acquired"] and old["token"] != new["token"]
    assert not (await first.call("release", session_id=sid, token=old["token"]))["released"]
    assert (
        await database.fetchval("SELECT claim_owner FROM sessions WHERE id = $1", uuid.UUID(sid))
        == new["token"]
    )
    assert (await second.call("release", session_id=sid, token=new["token"]))["released"]


async def test_two_job_workers_and_stale_finish(workers):
    database, sid, (first, second) = workers
    from ah.core.scheduler import job_store

    job = await job_store.create(
        name="process fencing",
        kind="interval",
        session_id=uuid.UUID(sid),
        prompt="test",
        interval_seconds=10,
    )
    await database.execute(
        "UPDATE jobs SET next_run_at = now() - interval '1 second' WHERE id = $1", job.id
    )
    results = await asyncio.gather(first.call("job"), second.call("job"))
    assert sorted(result["acquired"] for result in results) == [False, True]
    old = next(result for result in results if result["acquired"])
    await database.execute(
        "UPDATE jobs SET next_run_at = now() - interval '1 second' WHERE id = $1", job.id
    )
    new = await second.call("job")
    assert new["acquired"] and new["token"] != old["token"]
    assert not (await first.call("finish", job_id=str(job.id), token=old["token"]))["finished"]
    assert (await second.call("finish", job_id=str(job.id), token=new["token"]))["finished"]
    assert await database.fetchval("SELECT run_count FROM jobs WHERE id = $1", job.id) == 1


async def test_approval_consumed_once_across_processes(workers):
    _, sid, processes = workers
    from ah.permissions import store
    from ah.permissions.policy import ActionRequest, normalize_request

    request = normalize_request(
        ActionRequest(
            session_id=sid,
            turn_id="original-turn",
            agent_id="harness",
            operation="file.write",
            targets=["worker.txt"],
            content_digest="content",
        )
    )
    await store.create_approval(request, "user")
    assert await store.resolve_decision(request.request_id, "approved", "user")
    results = await asyncio.gather(
        *(
            worker.call(
                "approval", session_id=sid, request_id=request.request_id, digest=request.digest
            )
            for worker in processes
        )
    )
    assert sorted(result["claimed"] for result in results) == [False, True]


async def test_mode_change_reaches_other_process_despite_local_cache(workers):
    _, sid, processes = workers
    from ah.core.session_mode import activate_durable_mode

    await activate_durable_mode(sid, "harness", "workspace")
    assert (await processes[0].call("mode", session_id=sid))["mode"] == "workspace"
    await activate_durable_mode(sid, "harness", "ask")
    assert (await processes[1].call("mode", session_id=sid))["mode"] == "ask"
