"""No-agent jobs execute the script content approved by the real broker."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from ah.core.scheduler import Job, JobRunner


async def test_script_changed_during_approval_cannot_execute_unreviewed_content(
    monkeypatch, tmp_path
):
    from ah.core.context import context_manager
    from ah.permissions import store
    from ah.permissions.broker import clear_execution_context, set_approval_handler

    script = tmp_path / "reviewed.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('approved.txt').write_text('reviewed')\n"
        "print('approved output')\n",
        encoding="utf-8",
    )
    changed = (
        "from pathlib import Path\n"
        "Path('unreviewed.txt').write_text('changed after approval')\n"
        "print('unreviewed output')\n"
    )
    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    monkeypatch.setattr("ah.core.session_mode.get_effective_mode", lambda _sid: "ask")
    outputs = []
    approvals = []

    async def add_chunk(**kwargs):
        outputs.append(kwargs["payload"]["content"])

    async def approve(card):
        approvals.append(card["request_id"])
        await store.resolve_decision(card["request_id"], "approved", principal="tui")
        script.write_text(changed, encoding="utf-8")
        return "approved"

    monkeypatch.setattr(context_manager, "add_chunk", add_chunk)
    job = Job(
        id=uuid.uuid4(),
        name="snapshot",
        kind="interval",
        session_id=uuid.uuid4(),
        agent_name="harness",
        prompt="",
        interval_seconds=60,
        enabled=True,
        status="running",
        last_run_at=None,
        next_run_at=datetime.now(UTC),
        last_error=None,
        run_count=0,
        no_agent=True,
        script_path="reviewed.py",
    )
    set_approval_handler(approve, principal="tui")
    try:
        await JobRunner()._execute(job)
        assert not (tmp_path / "unreviewed.txt").exists()
        assert (tmp_path / "approved.txt").read_text() == "reviewed"
        assert outputs == ["approved output"]
        record = await store.get_approval(approvals[0])
        assert record["status"] == "completed"
    finally:
        set_approval_handler(None)
        clear_execution_context()
        for request_id in approvals:
            store._mem.approvals.pop(request_id, None)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "latin-1"])
async def test_python_snapshot_preserves_encoding_and_script_identity(
    monkeypatch, tmp_path, encoding
):
    from ah.core.job_scripts import run_job_script

    monkeypatch.setenv("AGENT_HARNESS_SCRIPTS_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "snapshot-secret-must-not-leak")
    (tmp_path / "sibling.py").write_text("value = 'local import'\n", encoding="utf-8")
    target = tmp_path / "identity.py"
    source = (
        f"# coding: {encoding}\n"
        "import os, sys, pickle, __main__\nfrom pathlib import Path\nimport sibling\n"
        "def script_function(): return 'function'\n"
        "assert __main__.script_function is script_function\n"
        "assert pickle.loads(pickle.dumps(script_function)) is script_function\n"
        "assert Path(__file__).resolve() == Path(sys.argv[0]).resolve()\n"
        "assert Path(__file__).parent.resolve() == Path.cwd()\n"
        "assert len(sys.argv) == 1\n"
        "assert not os.getenv('OPENROUTER_API_KEY')\n"
        "print('café', Path(__file__).name, sibling.value)\n"
    ).encode(encoding)
    target.write_bytes(source)

    output = await run_job_script(
        "identity.py", approved_content=source, approved_path=target.resolve()
    )

    assert output == "café identity.py local import"
