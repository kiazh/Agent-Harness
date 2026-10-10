"""Scheduled execution preserves approval identity and terminal outcomes."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from ah.core.models import AgentResponse
from ah.core.scheduler import Job, JobRunner


def _job(*, no_agent=False):
    return Job(
        id=uuid.uuid4(),
        name="approval regression",
        kind="interval",
        session_id=uuid.uuid4(),
        agent_name="harness",
        prompt="" if no_agent else "work",
        interval_seconds=60,
        enabled=True,
        status="running",
        last_run_at=None,
        next_run_at=datetime.now(UTC),
        last_error=None,
        run_count=0,
        no_agent=no_agent,
        script_path="job.py" if no_agent else None,
        claim_token=uuid.uuid4(),
    )


@pytest.fixture
def local_turns(monkeypatch):
    """Replace only the database turn lease boundary, retaining runner behavior."""
    from ah.core import turns

    held = set()

    async def begin(session_id):
        held.add(session_id)
        return "turn-token"

    async def end(session_id, token):
        assert token == "turn-token"
        held.remove(session_id)

    monkeypatch.setattr(turns, "try_begin_turn", begin)
    monkeypatch.setattr(turns, "end_turn", end)
    return held


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_script_approval_records_its_execution_outcome(
    monkeypatch, tmp_path, local_turns, outcome
):
    from ah.core import job_scripts
    from ah.permissions import store
    from ah.permissions.broker import clear_execution_context, set_approval_handler

    job = _job(no_agent=True)
    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    (tmp_path / "job.py").write_text("pass", encoding="utf-8")
    monkeypatch.setattr("ah.core.session_mode.get_effective_mode", lambda _sid: "ask")
    set_approval_handler(None)
    runner = JobRunner()

    async def script(script_path, *, approved_content, approved_path):
        assert script_path == "job.py"
        assert approved_content == b"pass"
        assert approved_path == (tmp_path / "job.py").resolve()
        if outcome == "failed":
            raise RuntimeError("script exited 7")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return ""

    # Subprocess execution is external; the broker and approval store remain real.
    monkeypatch.setattr(job_scripts, "run_job_script", script)
    request_id = None
    try:
        with pytest.raises(PermissionError, match="needs_approval") as paused:
            await runner._execute(job)
        request_id = paused.value.approval_request_id
        approved = await store.resolve_decision(request_id, "approved", principal="tui")
        assert approved is not None

        if outcome == "failed":
            with pytest.raises(RuntimeError, match="script exited 7"):
                await runner._execute(job)
        elif outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await runner._execute(job)
        else:
            await runner._execute(job)

        record = await store.get_approval(request_id)
        assert record["status"] == outcome
        assert not local_turns
    finally:
        clear_execution_context()
        if request_id:
            store._mem.approvals.pop(request_id, None)


@pytest.mark.asyncio
async def test_agent_pause_parks_the_exact_structured_approval_id(local_turns):
    job = _job()
    request_id = "approval:read-script-17"
    outcomes = []

    class Store:
        async def claim_due(self):
            return job

        async def renew_lease(self, job_id, claim_token):
            assert (job_id, claim_token) == (job.id, job.claim_token)
            return True

        async def finish(self, job_id, *, error, claim_token, paused_for):
            assert (job_id, claim_token) == (job.id, job.claim_token)
            outcomes.append(paused_for)
            return True

    class Agent:
        async def run(self, session_id, prompt, verbose):
            assert (session_id, prompt, verbose) == (job.session_id, "work", False)
            return AgentResponse(
                content="",
                needs_approval=[{"request_id": request_id, "operation": "file.read"}],
            )

    runner = JobRunner(store=Store(), agent_factory=lambda _name: Agent())
    assert await runner.run_due_once()
    assert outcomes == [request_id]
    assert not local_turns
