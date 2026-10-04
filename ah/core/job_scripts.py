"""Bounded script execution for explicitly configured no-agent jobs."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
from pathlib import Path

from ah.memory.redaction import redact_secrets

SCRIPT_TIMEOUT_SECONDS = 30.0
MAX_SCRIPT_OUTPUT_BYTES = 64 * 1024
_WINDOWS_BOOTSTRAP = (
    "import subprocess,sys; "
    "sys.stdin.buffer.read(1); "
    "raise SystemExit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))"
)


def resolve_script_path(script_path: str) -> Path:
    """Resolve a user-managed script inside the configured scripts directory."""
    root = Path(
        os.environ.get("AGENT_HARNESS_SCRIPTS_DIR") or Path.home() / ".agent-harness" / "scripts"
    ).resolve()
    candidate = Path(script_path)
    if not script_path or candidate.is_absolute():
        raise ValueError("script must be relative to the scripts directory")
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        raise ValueError("script must stay inside the scripts directory")
    if target.suffix.lower() not in {".py", ".sh", ".bash"}:
        raise ValueError("script extension must be .py, .sh, or .bash")
    if not target.is_file():
        raise ValueError("script does not exist in the scripts directory")
    return target


async def _read_capped(stream: asyncio.StreamReader) -> bytes:
    chunks = []
    size = 0
    while data := await stream.read(4096):
        size += len(data)
        if size > MAX_SCRIPT_OUTPUT_BYTES:
            raise ValueError("script output exceeded 64 KiB")
        chunks.append(data)
    return b"".join(chunks)


async def _terminate_tree(process: asyncio.subprocess.Process, job_handle: int | None) -> None:
    """Stop every remaining script descendant, including after a successful run."""
    if os.name == "nt":
        if job_handle is not None:
            from ah.core.windows_job import close_script_job

            close_script_job(job_handle)
        elif process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def run_job_script(script_path: str) -> str:
    """Run a Python or bash script with a time limit and no provider secrets."""
    target = resolve_script_path(script_path)
    if target.suffix.lower() == ".py":
        command = [sys.executable, str(target)]
    else:
        bash = shutil.which("bash")
        if bash is None:
            raise ValueError("bash is required for shell scripts")
        command = [bash, str(target)]
    environment = {
        key: value
        for key in ("PATH", "SystemRoot", "WINDIR", "TMP", "TEMP", "TMPDIR")
        if (value := os.environ.get(key)) is not None
    }
    windows = os.name == "nt"
    process_options = {} if windows else {"start_new_session": True}
    process = await asyncio.create_subprocess_exec(
        *([sys.executable, "-c", _WINDOWS_BOOTSTRAP, *command] if windows else command),
        cwd=str(target.parent),
        env=environment,
        stdin=asyncio.subprocess.PIPE if windows else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_options,
    )
    job_handle = None
    try:
        if windows:
            from ah.core.windows_job import create_script_job

            job_handle = create_script_job(process.pid)
            process.stdin.write(b"\x00")
            await process.stdin.drain()
            process.stdin.close()
        stdout, stderr, _ = await asyncio.wait_for(
            asyncio.gather(
                _read_capped(process.stdout),
                _read_capped(process.stderr),
                process.wait(),
            ),
            timeout=SCRIPT_TIMEOUT_SECONDS,
        )
    finally:
        cleanup = asyncio.create_task(_terminate_tree(process, job_handle))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
    if process.returncode:
        detail = redact_secrets(stderr.decode("utf-8", errors="replace")[:500]).text
        raise RuntimeError(f"script exited {process.returncode}: {detail}")
    return stdout.decode("utf-8", errors="replace").strip()
