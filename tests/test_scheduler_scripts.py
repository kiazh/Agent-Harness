"""Script-only scheduled jobs never enter the inference path."""

import uuid

import pytest

from ah.core.scheduler import JobRunner, JobStore
from ah.core.session import SessionManager


async def test_script_job_persists_and_delivers_stdout_without_an_agent(
    db_pool, monkeypatch, tmp_path
):
    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    (tmp_path / "watch.py").write_text("print('disk alert')", encoding="utf-8")
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    session = await SessionManager().create(title="script", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    chunks = []

    async def add_chunk(**kwargs):
        chunks.append(kwargs)

    monkeypatch.setattr("ah.core.context.context_manager.add_chunk", add_chunk)
    try:
        job = await store.create(
            name="watch",
            kind="interval",
            session_id=session.id,
            prompt="",
            interval_seconds=60,
            no_agent=True,
            script_path="watch.py",
        )
        assert job.to_dict()["noAgent"] is True
        assert (await store.get(job.id)).script_path == "watch.py"
        runner = JobRunner(store=store)

        async def no_agent(*args, **kwargs):
            raise AssertionError("LLM agent was built")

        monkeypatch.setattr(runner, "_build_agent", no_agent)
        # AH-AUDIT-009: broker-governed scripts pause first; drive the real
        # pause → human resolve → execute flow.
        from ah.permissions import store as perm_store

        with pytest.raises(PermissionError, match="needs_approval"):
            await runner._execute(job)
        pending = await perm_store.list_pending(str(session.id))
        assert len(pending) == 1
        resolved = await perm_store.resolve_decision(
            pending[0]["request_id"], "approved", principal="tui"
        )
        assert resolved is not None
        await runner._execute(job)
        assert chunks[0]["session_id"] == session.id
        assert chunks[0]["payload"]["content"] == "disk alert"
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


@pytest.mark.asyncio
async def test_script_runner_rejects_traversal_and_hides_provider_secret(monkeypatch, tmp_path):
    from ah.core.job_scripts import run_job_script

    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sentinel-secret")
    (tmp_path / "env.py").write_text(
        "import os\nprint('secret-present' if os.getenv('OPENROUTER_API_KEY') else 'clean')",
        encoding="utf-8",
    )
    assert await run_job_script("env.py") == "clean"
    with pytest.raises(ValueError, match="scripts directory"):
        await run_job_script("../outside.py")
    with pytest.raises(ValueError, match="extension"):
        await run_job_script("env.txt")


@pytest.mark.asyncio
async def test_script_runner_bounds_time_and_suppresses_empty_output(monkeypatch, tmp_path):
    from ah.core import job_scripts

    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    (tmp_path / "silent.py").write_text("print('   ')", encoding="utf-8")
    (tmp_path / "hang.py").write_text("import time\ntime.sleep(5)", encoding="utf-8")
    assert await job_scripts.run_job_script("silent.py") == ""
    monkeypatch.setattr(job_scripts, "SCRIPT_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(TimeoutError):
        await job_scripts.run_job_script("hang.py")


async def test_gateway_creates_script_job_without_prompt(db_pool, monkeypatch, tmp_path):
    from ah.gateway.features.jobs import jobs_create

    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    (tmp_path / "check.py").write_text("print('ok')", encoding="utf-8")
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    session = await SessionManager().create(
        title="gateway script", agent_id=f"agent-{uuid.uuid4()}"
    )

    class GatewayStub:
        def require_db(self):
            return None

        async def get_session(self, params):
            return session

    try:
        result = await jobs_create(
            GatewayStub(),
            {
                "sessionId": str(session.id),
                "kind": "interval",
                "intervalSeconds": 60,
                "noAgent": True,
                "scriptPath": "check.py",
            },
        )
        assert result["job"]["noAgent"] is True
        assert result["job"]["scriptPath"] == "check.py"
        from ah.gateway.errors import INVALID_PARAMS, RpcError

        with pytest.raises(RpcError) as error:
            await jobs_create(
                GatewayStub(),
                {"sessionId": str(session.id), "prompt": "run", "scriptPath": "check.py"},
            )
        assert error.value.code == INVALID_PARAMS
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


async def test_script_job_rejects_an_unused_prompt(db_pool, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    (tmp_path / "check.py").write_text("print('ok')", encoding="utf-8")
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    session = await SessionManager().create(title="script prompt", agent_id=f"agent-{uuid.uuid4()}")
    try:
        with pytest.raises(ValueError, match="prompt"):
            await JobStore().create(
                name="check",
                kind="interval",
                session_id=session.id,
                prompt="ignored instructions",
                interval_seconds=60,
                no_agent=True,
                script_path="check.py",
            )
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


@pytest.mark.asyncio
async def test_script_timeout_terminates_child_processes(monkeypatch, tmp_path):
    import asyncio

    from ah.core import job_scripts

    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    marker = tmp_path / "orphan.txt"
    child_code = (
        f"import time, pathlib; time.sleep(0.4); pathlib.Path({str(marker)!r}).write_text('orphan')"
    )
    (tmp_path / "parent.py").write_text(
        f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {child_code!r}])\ntime.sleep(5)",
        encoding="utf-8",
    )
    monkeypatch.setattr(job_scripts, "SCRIPT_TIMEOUT_SECONDS", 0.2)
    with pytest.raises(TimeoutError):
        await job_scripts.run_job_script("parent.py")
    await asyncio.sleep(0.6)
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("detached_output", [False, True])
async def test_script_children_do_not_outlive_successful_parent(
    monkeypatch, tmp_path, detached_output
):
    import asyncio

    from ah.core import job_scripts

    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    marker = tmp_path / "late-child.txt"
    child_code = (
        f"import time, pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('late')"
    )
    output_args = (
        ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL" if detached_output else ""
    )
    (tmp_path / "parent.py").write_text(
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]{output_args})\n"
        "print('parent done')",
        encoding="utf-8",
    )
    monkeypatch.setattr(job_scripts, "SCRIPT_TIMEOUT_SECONDS", 5.0 if detached_output else 0.8)
    if detached_output:
        assert await job_scripts.run_job_script("parent.py") == "parent done"
    else:
        with pytest.raises(TimeoutError):
            await job_scripts.run_job_script("parent.py")
    await asyncio.sleep(1.7)
    assert not marker.exists()
