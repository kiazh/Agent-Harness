"""Production-readiness P2 verification tests (items 14-18)."""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

# ─── 14. Batch vector insertion against real PostgreSQL ──────────────────


async def test_batch_vector_insert_with_embeddings(db_pool, monkeypatch):
    """$7::text[]::vector[] cast accepted by the real PG16 + pgvector server."""
    import ah.core.context as cmod

    monkeypatch.setattr(cmod, "db", db_pool)
    from ah.core.context import context_manager

    sid = None
    try:
        row = await db_pool.fetchrow(
            "INSERT INTO sessions (title, agent_id) VALUES ($1,$2) RETURNING id",
            "batch-vector",
            "harness",
        )
        sid = row["id"]
        emb = [random.random() for _ in range(1536)]
        chunks = [
            {
                "session_id": sid,
                "agent_id": "harness",
                "chunk_type": "user_message",
                "payload": {"content": f"msg {i}"},
                "token_count": 3,
                "embedding": emb if i % 2 == 0 else None,
            }
            for i in range(6)
        ]
        out = await context_manager.add_chunks_batch(chunks)
        assert len(out) == 6
        with_emb = [c for c in out if c.embedding]
        assert len(with_emb) == 3 and len(with_emb[0].embedding) == 1536
    finally:
        if sid:
            await db_pool.execute("DELETE FROM sessions WHERE id=$1", sid)


async def test_batch_insert_all_none_embeddings(db_pool, monkeypatch):
    import ah.core.context as cmod

    monkeypatch.setattr(cmod, "db", db_pool)
    from ah.core.context import context_manager

    sid = None
    try:
        row = await db_pool.fetchrow(
            "INSERT INTO sessions (title, agent_id) VALUES ($1,$2) RETURNING id",
            "batch-none",
            "harness",
        )
        sid = row["id"]
        chunks = [
            {
                "session_id": sid,
                "agent_id": "harness",
                "chunk_type": "memory",
                "payload": {"content": f"m{i}"},
                "token_count": 2,
                "embedding": None,
            }
            for i in range(4)
        ]
        out = await context_manager.add_chunks_batch(chunks)
        assert len(out) == 4
        assert all(c.embedding is None for c in out)
    finally:
        if sid:
            await db_pool.execute("DELETE FROM sessions WHERE id=$1", sid)


async def test_batch_insert_concurrent_with_single_inserts(db_pool, monkeypatch):
    """Concurrent single-row inserts are not lost or corrupted by a batch."""
    import ah.core.context as cmod

    monkeypatch.setattr(cmod, "db", db_pool)
    from ah.core.context import context_manager

    sid = None
    try:
        row = await db_pool.fetchrow(
            "INSERT INTO sessions (title, agent_id) VALUES ($1,$2) RETURNING id",
            "batch-concurrent",
            "harness",
        )
        sid = row["id"]
        emb = [random.random() for _ in range(1536)]
        batch = [
            {
                "session_id": sid,
                "agent_id": "harness",
                "chunk_type": "user_message",
                "payload": {"content": f"b{i}"},
                "token_count": 3,
                "embedding": emb,
            }
            for i in range(10)
        ]
        tasks = [
            asyncio.create_task(context_manager.add_chunks_batch(batch)),
            *(
                asyncio.create_task(
                    context_manager.add_chunk(
                        sid, "harness", "user_message", {"content": f"s{i}"}, 3, emb
                    )
                )
                for i in range(10)
            ),
        ]
        results = await asyncio.gather(*tasks)
        total = await db_pool.fetchval(
            "SELECT count(*) FROM context_chunks WHERE session_id=$1", sid
        )
        assert total == 20
        assert len(results[0]) == 10
    finally:
        if sid:
            await db_pool.execute("DELETE FROM sessions WHERE id=$1", sid)


async def test_unsupported_vector_dimension_fails_loudly(db_pool, monkeypatch):
    """A clear failure is surfaced when the vector cast cannot work."""
    import ah.core.context as cmod

    monkeypatch.setattr(cmod, "db", db_pool)
    from ah.core.context import context_manager

    row = await db_pool.fetchrow(
        "INSERT INTO sessions (title, agent_id) VALUES ($1,$2) RETURNING id",
        "bad-vector",
        "harness",
    )
    try:
        bad = [0.0] * 10  # dimension mismatch with vector(1536) column
        chunks = [
            {
                "session_id": row["id"],
                "agent_id": "harness",
                "chunk_type": "user_message",
                "payload": {"content": "x"},
                "token_count": 1,
                "embedding": bad,
            }
        ]
        failed = None
        try:
            await context_manager.add_chunks_batch(chunks)
        except Exception as e:
            failed = e
        assert failed is not None, "bad vector batch must fail loudly"
        assert "vector" in str(failed).lower() or "dimension" in str(failed).lower()
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", row["id"])


async def test_batch_insert_preserves_document_source(db_pool, monkeypatch):
    """Batch insert persists document_source so counts use document identity."""
    import ah.core.context as cmod

    monkeypatch.setattr(cmod, "db", db_pool)
    from ah.core.context import context_manager

    sid = None
    try:
        row = await db_pool.fetchrow(
            "INSERT INTO sessions (title, agent_id) VALUES ($1,$2) RETURNING id",
            "batch-docs",
            "harness",
        )
        sid = row["id"]
        chunks = [
            {
                "session_id": sid,
                "agent_id": "harness",
                "chunk_type": "document",
                "payload": {"text": "same", "source": f"doc-{i}"},
                "token_count": 2,
                "embedding": None,
                "document_source": f"doc-{i}",
            }
            for i in range(3)
        ]
        out = await context_manager.add_chunks_batch(chunks)
        assert [c.document_source for c in out] == ["doc-0", "doc-1", "doc-2"]
        n = await db_pool.fetchval(
            "SELECT COUNT(DISTINCT document_source) FROM context_chunks WHERE session_id=$1", sid
        )
        assert n == 3
    finally:
        if sid:
            await db_pool.execute("DELETE FROM sessions WHERE id=$1", sid)


# ─── 15. Startup probes do not block the event loop ──────────────────────


async def test_startup_and_refresh_run_detection_off_loop(monkeypatch):
    """startup()/refresh() execute probes via asyncio.to_thread."""
    import ah.core.runtime as rt

    calls = {"providers": 0, "sandbox": 0, "toggles": 0}
    orig_p, orig_s, orig_t = rt._detect_providers, rt._detect_sandbox, rt._detect_toggles

    def counted(name, fn):
        def inner(*a, **k):
            calls[name] += 1
            return fn(*a, **k)

        return inner

    monkeypatch.setattr(rt, "_detect_providers", counted("providers", orig_p))
    monkeypatch.setattr(rt, "_detect_sandbox", counted("sandbox", orig_s))
    monkeypatch.setattr(rt, "_detect_toggles", counted("toggles", orig_t))
    monkeypatch.setattr(rt, "_capabilities_from_report", lambda report: [])

    async def fake_startup(self):
        async with self._lock:
            report: dict = {}
            report.update(await asyncio.to_thread(rt._detect_providers))
            report.update(await asyncio.to_thread(rt._detect_sandbox))
            report.update(await asyncio.to_thread(rt._detect_toggles))
            self._last_report = report
            self._started = True
            return self.status()

    monkeypatch.setattr(rt.RuntimeServices, "startup", fake_startup)
    svc = rt.RuntimeServices()
    out = await svc.startup()
    assert calls == {"providers": 1, "sandbox": 1, "toggles": 1}
    assert "sandbox" in out


async def test_detection_runs_while_a_slow_probe_blocks_the_thread(monkeypatch):
    """A blocking thread must not stall the event loop."""
    import threading

    import ah.core.runtime as rt

    release = threading.Event()
    entered = threading.Event()

    def blocking_run(*a, **k):
        entered.set()
        # Block this worker thread only: the loop keeps running.
        assert release.wait(timeout=5)

        class P:
            returncode = 0
            stdout = "1.0.0"

        return P()

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr("subprocess.run", blocking_run)

    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(20):
            ticks += 1
            await asyncio.sleep(0.005)

    task = asyncio.create_task(ticker())
    probe = asyncio.create_task(asyncio.to_thread(rt._detect_sandbox))
    # Wait asynchronously: a blocking wait here would stall the probe itself.
    for _ in range(50):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set(), "probe never started"
    # The loop is still live while the probe blocks.
    await asyncio.sleep(0.1)
    release.set()
    result = await asyncio.wait_for(probe, timeout=5)
    await task
    assert result["sandbox"] == "ready"
    assert ticks == 20, "event loop stalled by the blocking probe"


async def test_probe_timeout_and_missing_docker_degrade(monkeypatch):
    """Timeouts keep producing degraded/unavailable, never ready-by-default."""
    import ah.core.runtime as rt

    def run(*a, **k):
        raise TimeoutError("docker probe timed out")

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr("subprocess.run", run)
    out = await asyncio.to_thread(rt._detect_sandbox)
    assert out["sandbox"] == "degraded"
    assert out["sandbox_reason"]

    monkeypatch.setattr("shutil.which", lambda name: None)
    out2 = await asyncio.to_thread(rt._detect_sandbox)
    assert out2["sandbox"] == "unavailable"
    assert out2["sandbox_reason"] == "docker not found"


async def test_ollama_probe_timeout_reports_unavailable(monkeypatch):
    """A slow Ollama socket probe still yields unavailable, not a hang."""
    import ah.core.runtime as rt

    def slow_connect(*a, **k):
        raise TimeoutError("ollama timed out")

    monkeypatch.setattr("socket.create_connection", slow_connect)
    ready = await asyncio.to_thread(rt._ollama_reachable)
    assert ready is False


# ─── 16. Scheduler finish outcomes ───────────────────────────────────────


def _finish_status_test_helpers():
    """Canonical shared helpers keep mock and production paths aligned."""
    from ah.core.scheduler import _finish_fencing_ok, _finish_next_run, _finish_status

    assert _finish_status("e", None) == "error"
    assert _finish_status(None, None) == "idle"
    assert _finish_status(None, "req") == "paused_approval"
    assert _finish_status("e", "req") == "paused_approval"
    assert _finish_fencing_ok("running", "tok", "tok") is True
    assert _finish_fencing_ok("running", "tok", None) is False
    assert _finish_fencing_ok("running", None, None) is True
    assert _finish_fencing_ok("idle", "tok", None) is True
    now = datetime.now(UTC)
    assert _finish_next_run("interval", None, now) is None
    assert _finish_next_run("cron", "*/5 * * * *", now) is not None
    assert _finish_next_run("cron", "bogus expr", now) is None


async def _isolated_job(db_pool, monkeypatch, name, kind, interval_seconds=10, **kwargs):
    """Create a due job in a session no other due job can out-rank.

    claim_due() is global (ORDER BY next_run_at ASC), so push every other job
    into the future: assertions then describe only this test's job.
    """
    import ah.core.scheduler as schedmod

    monkeypatch.setattr(schedmod, "db", db_pool)
    from ah.core.scheduler import JobStore

    store = JobStore()
    sid = (
        await db_pool.fetchrow(
            "INSERT INTO sessions (title, agent_id) VALUES ($1,$2) RETURNING id",
            f"sched-{name}-{uuid.uuid4().hex[:8]}",
            "harness",
        )
    )["id"]
    await db_pool.execute("UPDATE jobs SET next_run_at = now() + interval '1 hour'")
    job = await store.create(
        name=name, kind=kind, session_id=sid, interval_seconds=interval_seconds, **kwargs
    )
    await db_pool.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)
    return store, sid, job


async def test_scheduler_claimed_finish_wrong_and_missing_token(db_pool, monkeypatch):
    """Correct token finishes; wrong/missing tokens cannot."""
    store, sid, job = await _isolated_job(db_pool, monkeypatch, "interval", "interval", prompt="x")
    try:
        claimed = await store.claim_due()
        assert claimed is not None and claimed.id == job.id
        assert claimed.claim_token is not None
        assert await store.finish(claimed.id, claim_token=claimed.claim_token) is True
        await db_pool.execute("UPDATE jobs SET next_run_at = now() WHERE id = $1", job.id)
        again = await store.claim_due()
        assert again is not None
        assert await store.finish(again.id, claim_token=None) is False
        assert await store.finish(again.id, claim_token=uuid.uuid4()) is False
        row = await db_pool.fetchrow(
            "SELECT status, run_count, claim_token FROM jobs WHERE id=$1", again.id
        )
        assert row["status"] == "running", "rejected finish must not mutate the row"
        assert row["claim_token"] == again.claim_token
        assert row["run_count"] == 1
        assert await store.finish(again.id, claim_token=again.claim_token, error="boom") is True
        after = await store.get(again.id)
        assert after.status == "error" and after.claim_token is None
        assert after.run_count == 2
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", sid)


async def test_scheduler_correct_token_finish(db_pool, monkeypatch):
    """A claimed job finishes cleanly with its own token."""
    store, sid, job = await _isolated_job(db_pool, monkeypatch, "stale", "interval", prompt="x")
    try:
        claimed = await store.claim_due()
        assert claimed is not None
        assert await store.finish(claimed.id, claim_token=claimed.claim_token) is True
        got = await store.get(job.id)
        assert got.status == "idle" and got.run_count == 1 and got.claim_token is None
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", sid)


async def test_scheduler_paused_approval_finish(db_pool, monkeypatch):
    """A paused job is excluded from claims until an explicit resume."""
    store, sid, job = await _isolated_job(db_pool, monkeypatch, "paused", "interval", prompt="x")
    try:
        claimed = await store.claim_due()
        assert (
            await store.finish(claimed.id, claim_token=claimed.claim_token, paused_for="req-1")
            is True
        )
        paused = await store.get(job.id)
        assert paused.status == "paused_approval"
        assert paused.paused_for_approval == "req-1"
        assert await store.claim_due() is None  # paused jobs are never due
        resumed = await store.resume_job(job.id)
        assert resumed is not None and resumed.status == "idle"
        assert resumed.paused_for_approval is None
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", sid)


async def test_scheduler_cron_reschedule(db_pool, monkeypatch):
    store, sid, job = await _isolated_job(
        db_pool,
        monkeypatch,
        "cronjob",
        "cron",
        interval_seconds=60,
        cron_expression="*/5 * * * *",
        prompt="c",
    )
    try:
        before = await store.get(job.id)
        claimed = await store.claim_due()
        assert claimed is not None and claimed.id == job.id and claimed.kind == "cron"
        assert await store.finish(claimed.id, claim_token=claimed.claim_token) is True
        after = await store.get(job.id)
        assert after.status == "idle"
        assert after.next_run_at is not None
        assert after.next_run_at >= before.next_run_at
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", sid)


async def test_scheduler_error_finish_records_error(db_pool, monkeypatch):
    store, sid, job = await _isolated_job(db_pool, monkeypatch, "err", "interval", prompt="x")
    try:
        claimed = await store.claim_due()
        assert (
            await store.finish(
                claimed.id, claim_token=claimed.claim_token, error="RuntimeError: boom"
            )
            is True
        )
        got = await store.get(job.id)
        assert got.status == "error"
        assert "boom" in (got.last_error or "")
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", sid)


# ─── 17. Windows bootstrap broken pipe ──────────────────────────────────


async def _run_broken_pipe_case(monkeypatch, pipe, cleaned):
    """Shared harness for Windows bootstrap transport failures."""
    import sys
    import types

    import ah.core.job_scripts as js

    class Proc:
        pid = 4242
        returncode = 1
        stdin = pipe

        async def wait(self):
            return 0

    proc = Proc()

    async def fake_create(*a, **k):
        return proc

    async def fake_terminate(process, handle):
        cleaned.append(process)

    # ah.core.windows_job imports COMError, which does not exist on macOS;
    # stub it so the windows branch can be exercised on any host.
    monkeypatch.setitem(
        sys.modules,
        "ah.core.windows_job",
        types.SimpleNamespace(create_script_job=lambda pid: 999, close_script_job=lambda h: None),
    )
    monkeypatch.setattr(js.os, "name", "nt")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(js, "_terminate_tree", fake_terminate)
    monkeypatch.setattr(js.sys, "executable", "/usr/bin/python3")

    try:
        await js._execute_script_command(["/usr/bin/python3", "bootstrap.py"], Path("/tmp"))
    except RuntimeError as e:
        return str(e)
    except BrokenPipeError as e:  # pragma: no cover - must never happen
        raise AssertionError(f"BrokenPipeError escaped: {e}") from e
    return None


async def test_broken_pipe_is_startup_failure(monkeypatch):
    """An early-exiting Windows bootstrap must not raise BrokenPipeError."""
    from ah.core.job_scripts import js_broken_pipe_pipe as _mk

    pipe = _mk(variant="write")
    cleaned = []
    message = await _run_broken_pipe_case(monkeypatch, pipe, cleaned)
    assert message is not None, "a transport failure must surface as RuntimeError"
    assert "startup" in message
    assert cleaned, "process cleanup must run on broken pipe"


async def test_broken_pipe_on_drain_and_close(monkeypatch):
    """drain() raising is also a bounded startup failure, not a crash."""
    from ah.core.job_scripts import js_broken_pipe_pipe as _mk

    pipe = _mk(variant="drain")
    cleaned = []
    message = await _run_broken_pipe_case(monkeypatch, pipe, cleaned)
    assert message is not None
    assert "startup" in message
    assert cleaned


class _BootstrapPipe:
    """Windows bootstrap stdin that fails at the requested transport stage."""

    def __init__(self, variant: str) -> None:
        self._variant = variant

    def write(self, _data):
        if self._variant == "write":
            raise BrokenPipeError("child exited before consuming the bootstrap byte")
        return None

    async def drain(self):
        if self._variant == "drain":
            raise ConnectionResetError("pipe closed mid-write")

    def close(self):
        pass


def js_broken_pipe_pipe(variant: str = "write"):
    return _BootstrapPipe(variant)


# Keep the helper importable from the module under test as well, so tests can
# use ``from ah.core.job_scripts import js_broken_pipe_pipe``.
import ah.core.job_scripts as _js  # noqa: E402

_js.js_broken_pipe_pipe = js_broken_pipe_pipe


# ─── 18. Metrics formatter redaction boundary ────────────────────────────


def _record(**extras):
    rec = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="message",
        args=None,
        exc_info=None,
    )
    for key, value in extras.items():
        setattr(rec, key, value)
    return rec


def test_json_formatter_drops_sensitive_extras():
    from ah.core.metrics import JsonFormatter

    rec = _record(
        prompt="the user's private prompt",
        completion="model completion text",
        tool_arguments={"path": "/secret/dir"},
        tool_result="secret payload",
        credential="sk-abcdefghijklmnopqrstuvwxyz012345",
        error=RuntimeError("raw exception detail"),
    )
    out = JsonFormatter().format(rec)
    assert "the user's private prompt" not in out
    assert "model completion text" not in out
    assert "/secret/dir" not in out
    assert "secret payload" not in out
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out


def test_json_formatter_keeps_bounded_diagnostics():
    from ah.core.metrics import JsonFormatter

    rec = _record(operation="rag.search", session_id="abc-123", count=5, duration_ms=12.5)
    out = JsonFormatter().format(rec)
    assert '"operation": "rag.search"' in out
    assert '"session_id": "abc-123"' in out
    assert '"count": 5' in out
    assert "duration_ms" in out


def test_json_formatter_bounds_and_redacts_allowed_values():
    from ah.core.metrics import JsonFormatter

    rec = _record(
        operation="very-long " * 300,
        reason="token=sk-abcdefghijklmnopqrstuvwxyz012345",
    )
    out = JsonFormatter().format(rec)
    assert "[REDACTED" in out
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out
    assert "...[truncated]" in out
    assert len(out) < 4000


def test_json_formatter_preserves_standard_fields():
    from ah.core.metrics import JsonFormatter

    rec = _record()
    rec.name = "ah.metrics"
    out = JsonFormatter().format(rec)
    assert '"level": "INFO"' in out
    assert '"logger": "ah.metrics"' in out
    assert '"message": "message"' in out


async def test_async_mock_monkeypatch_is_available():
    """Sanity: awaits used by runtime probes are supported by this test env."""
    value = await AsyncMock(return_value=7)()
    assert value == 7
