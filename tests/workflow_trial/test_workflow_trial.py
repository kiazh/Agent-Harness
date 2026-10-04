"""Compare restartable native and LangGraph approval with one DB effect."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid

import pytest

from ah.core.session import SessionManager
from ah.research.workflow_trial import GraphApprovalTrial, NativeApprovalTrial


async def test_native_approval_is_scoped_and_idempotent_after_reopen(db_pool, monkeypatch):
    monkeypatch.setattr("ah.core.session.db", db_pool)
    owner = f"trial-{uuid.uuid4()}"
    session = await SessionManager().create(title="trial", agent_id=owner)
    run_id = uuid.uuid4()
    first = NativeApprovalTrial(db_pool)
    await first.setup()
    try:
        assert await first.start(run_id, session.id, owner, "approved action") == "pending"
        second = NativeApprovalTrial(db_pool)
        with pytest.raises(PermissionError):
            await second.resume(run_id, "foreign-agent", "approve")
        assert await second.resume(run_id, owner, "approve") == "complete"
        assert await second.resume(run_id, owner, "approve") == "complete"
        assert await second.effect_count(run_id) == 1
    finally:
        await db_pool.execute("DELETE FROM workflow_trial_effects WHERE run_id = $1", run_id)
        await db_pool.execute("DELETE FROM workflow_trial_runs WHERE id = $1", run_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


@pytest.mark.parametrize("decision, expected", [("deny", "denied"), ("cancel", "cancelled")])
async def test_native_denial_and_cancellation_never_execute(
    db_pool, monkeypatch, decision, expected
):
    monkeypatch.setattr("ah.core.session.db", db_pool)
    owner = f"trial-{uuid.uuid4()}"
    session = await SessionManager().create(title="trial", agent_id=owner)
    run_id = uuid.uuid4()
    trial = NativeApprovalTrial(db_pool)
    await trial.setup()
    try:
        await trial.start(run_id, session.id, owner, "blocked action")
        assert await trial.resume(run_id, owner, decision) == expected
        assert await trial.resume(run_id, owner, "approve") == expected
        assert await trial.effect_count(run_id) == 0
    finally:
        await db_pool.execute("DELETE FROM workflow_trial_runs WHERE id = $1", run_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


async def test_graph_postgres_checkpoint_resumes_after_reopen_and_blocks_replay(
    db_pool, monkeypatch
):
    pytest.importorskip("langgraph")
    monkeypatch.setattr("ah.core.session.db", db_pool)
    owner = f"trial-{uuid.uuid4()}"
    session = await SessionManager().create(title="trial", agent_id=owner)
    run_id = uuid.uuid4()
    try:
        async with GraphApprovalTrial(db_pool, db_pool.dsn) as first:
            assert await first.start(run_id, session.id, owner, "approved action") == "pending"
        async with GraphApprovalTrial(db_pool, db_pool.dsn) as second:
            with pytest.raises(PermissionError):
                await second.resume(run_id, "foreign-agent", "approve")
            assert await second.resume(run_id, owner, "approve") == "complete"
        async with GraphApprovalTrial(db_pool, db_pool.dsn) as third:
            assert await third.resume(run_id, owner, "approve") == "complete"
            assert await third.effect_count(run_id) == 1
            await third.delete_thread(run_id)
    finally:
        await db_pool.execute("DELETE FROM workflow_trial_effects WHERE run_id = $1", run_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


@pytest.mark.parametrize("decision, expected", [("deny", "denied"), ("cancel", "cancelled")])
async def test_graph_denial_and_cancellation_never_execute(
    db_pool, monkeypatch, decision, expected
):
    pytest.importorskip("langgraph")
    monkeypatch.setattr("ah.core.session.db", db_pool)
    owner = f"trial-{uuid.uuid4()}"
    session = await SessionManager().create(title="graph trial", agent_id=owner)
    run_id = uuid.uuid4()
    try:
        async with GraphApprovalTrial(db_pool, db_pool.dsn) as trial:
            await trial.start(run_id, session.id, owner, "blocked action")
            assert await trial.resume(run_id, owner, decision) == expected
            assert await trial.resume(run_id, owner, "approve") == expected
            assert await trial.effect_count(run_id) == 0
            await trial.delete_thread(run_id)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


async def test_graph_approval_survives_a_new_process(db_pool, monkeypatch):
    pytest.importorskip("langgraph")
    monkeypatch.setattr("ah.core.session.db", db_pool)
    owner = f"trial-{uuid.uuid4()}"
    session = await SessionManager().create(title="process trial", agent_id=owner)
    run_id = uuid.uuid4()
    inherited = {"PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "VIRTUAL_ENV"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in inherited}
    environment["AGENT_HARNESS_TEST_DATABASE_URL"] = db_pool.dsn

    async def stage(command: str, value: str) -> dict:
        process = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "ah.research.workflow_trial",
                "--backend",
                "graph",
                "--command",
                command,
                "--run-id",
                str(run_id),
                "--session-id",
                str(session.id),
                "--agent",
                owner,
                "--value",
                value,
            ],
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        assert process.returncode == 0, process.stderr
        return json.loads(process.stdout)

    try:
        assert (await stage("start", "approved action"))["status"] == "pending"
        assert (await stage("resume", "approve"))["status"] == "complete"
        assert (await stage("resume", "approve"))["status"] == "complete"
        assert (
            await db_pool.fetchval(
                "SELECT COUNT(*) FROM workflow_trial_effects WHERE run_id = $1", run_id
            )
            == 1
        )
        async with GraphApprovalTrial(db_pool, db_pool.dsn) as trial:
            await trial.delete_thread(run_id)
    finally:
        await db_pool.execute("DELETE FROM workflow_trial_effects WHERE run_id = $1", run_id)
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)
