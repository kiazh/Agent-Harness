"""Bounded script execution for explicitly configured no-agent jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ah.memory.redaction import redact_secrets

__all__ = ["resolve_script_path", "review_script", "run_job_script"]

SCRIPT_TIMEOUT_SECONDS = 30.0
MAX_SCRIPT_OUTPUT_BYTES = 64 * 1024
_WINDOWS_BOOTSTRAP = (
    "import subprocess,sys; "
    "sys.stdin.buffer.read(1); "
    "raise SystemExit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))"
)
_PYTHON_SNAPSHOT_BOOTSTRAP = (
    "import os,sys\n"
    "target,snapshot=sys.argv[1:3]\n"
    "sys.argv=[target]\n"
    "sys.path[0]=os.path.dirname(target)\n"
    "with open(snapshot,'rb') as source:\n"
    "    code=compile(source.read(),target,'exec')\n"
    "def run(code,namespace,target):\n"
    "    namespace.clear()\n"
    "    namespace.update(__name__='__main__',__file__=target,__package__=None,"
    "__spec__=None,__cached__=None,__doc__=None)\n"
    "    exec(code,namespace)\n"
    "run(code,vars(sys.modules['__main__']),target)\n"
)
_BASH_SNAPSHOT_BOOTSTRAP = 'snapshot=$1; shift; source "$snapshot"'


@dataclass(frozen=True)
class ScriptReview:
    target: Path
    content: bytes
    dependencies: tuple[tuple[str, str], ...]
    policy_digest: str

    @property
    def approval_content(self) -> str:
        return (
            self.content.decode("latin-1")
            + "\x00policy:"
            + self.policy_digest
            + "\x00deps:"
            + json.dumps(self.dependencies)
        )


def scripts_root() -> Path:
    return Path(
        os.environ.get("AGENT_HARNESS_SCRIPTS_DIR") or Path.home() / ".agent-harness" / "scripts"
    ).resolve()


def _read_admin_file(target: Path, root: Path, limit: int) -> bytes:
    """Bounded descriptor read; reject symlinks, hard links, and swapped paths."""
    import stat

    if not target.is_relative_to(root):
        raise ValueError("script dependency escapes administrator directory")
    for part in (target, *target.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError("administrator script files must not be symlinks")
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened, current = os.fstat(fd), target.lstat()
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError("administrator script files must be regular files without hard links")
        if (opened.st_dev, opened.st_ino) != (
            current.st_dev,
            current.st_ino,
        ) or not target.resolve().is_relative_to(root):
            raise ValueError("administrator script file changed during access")
        if os.name != "nt" and opened.st_mode & 0o022:
            raise ValueError("administrator script files must not be group/world writable")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError("administrator script file exceeds review bound")
        return content
    finally:
        os.close(fd)


def _dependency_list(target: Path, root: Path) -> tuple[str, ...]:
    from ah.permissions.policy import workspace_root

    if root.is_relative_to(workspace_root()):
        raise ValueError("administrator scripts directory must be outside the agent workspace")
    try:
        policy = json.loads(_read_admin_file(root / ".script-policy.json", root, 65_536))
    except (OSError, ValueError) as error:
        raise ValueError("administrator script allowlist unavailable or invalid") from error
    if (
        not isinstance(policy, dict)
        or policy.get("version") != 1
        or not isinstance(policy.get("scripts"), dict)
    ):
        raise ValueError("invalid administrator script allowlist")
    entries = policy["scripts"]
    name = target.relative_to(root).as_posix()
    if len(entries) > 256 or name not in entries:
        raise ValueError("script is not in the administrator allowlist")
    dependencies = entries[name]
    if (
        not isinstance(dependencies, list)
        or len(dependencies) > 32
        or any(not isinstance(item, str) or not item or len(item) > 500 for item in dependencies)
    ):
        raise ValueError("invalid administrator dependency allowlist")
    return tuple(sorted(set(dependencies)))


def review_script(script_path: str) -> ScriptReview:
    """Pin the entry and hash administrator-declared dependencies.

    This is a trusted dependency boundary, not automatic dependency discovery.
    Dynamic imports, runtime packages, interpreters, and undeclared resources
    remain administrator-trusted; only listed local dependencies are hashed.
    """
    target = resolve_script_path(script_path)
    root = scripts_root()
    names = _dependency_list(target, root)
    dependencies = []
    for name in names:
        path = Path(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or any(part.startswith(".") for part in path.parts)
        ):
            raise ValueError("invalid administrator dependency path")
        content = _read_admin_file(root / path, root, 2_000_000)
        dependencies.append((name, hashlib.sha256(content).hexdigest()))
    policy_digest = hashlib.sha256(
        json.dumps(
            {"entry": target.relative_to(root).as_posix(), "dependencies": names}, sort_keys=True
        ).encode()
    ).hexdigest()
    return ScriptReview(
        target, _read_admin_file(target, root, 200_000), tuple(dependencies), policy_digest
    )


def resolve_script_path(script_path: str) -> Path:
    """Resolve a user-managed script inside the configured scripts directory."""
    root = scripts_root()
    candidate = Path(script_path)
    if not script_path or candidate.is_absolute():
        raise ValueError("script must be relative to the scripts directory")
    raw = root / candidate
    for part in (raw, *raw.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError("script must not traverse symlinks")
    # Reject symlinks outright to close TOCTOU swaps outside the scripts dir.
    try:
        if raw.is_symlink():
            raise ValueError("script must not be a symlink")
    except OSError:
        raise ValueError("script must stay inside the scripts directory") from None
    target = raw.resolve()
    if not target.is_relative_to(root):
        raise ValueError("script must stay inside the scripts directory")
    if target.suffix.lower() not in {".py", ".sh", ".bash"}:
        raise ValueError("script extension must be .py, .sh, or .bash")
    if not target.is_file():
        raise ValueError("script does not exist in the scripts directory")
    # O_NOFOLLOW final check where supported: reject symlink races.
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        try:
            fd = os.open(target, os.O_RDONLY | nofollow)
        except OSError as e:
            raise ValueError("script must not be a symlink") from e
        else:
            os.close(fd)
    elif target.is_symlink():
        raise ValueError("script must not be a symlink")
    _dependency_list(target, root)
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


async def run_job_script(
    script_path: str,
    *,
    approved_content: bytes | None = None,
    approved_path: Path | None = None,
    approved_review: ScriptReview | None = None,
) -> str:
    """Execute an allowlisted administrator script using a private entry snapshot."""
    current = review_script(script_path)
    target = current.target
    if approved_review is not None and (
        current.dependencies != approved_review.dependencies
        or current.policy_digest != approved_review.policy_digest
    ):
        raise ValueError("script dependencies changed after approval; fresh approval required")
    if approved_content is None:
        approved_content = current.content
    elif approved_path is None or target != approved_path:
        raise ValueError("script path changed after approval")
    if approved_review is not None and approved_content != approved_review.content:
        raise ValueError("approved entry content does not match reviewed snapshot")
    if len(approved_content) > 200_000:
        raise ValueError("script exceeds 200,000 byte review bound")
    from ah.core.provider import audit_log

    audit_log(
        "job_script.dependencies",
        entry=target.name,
        policy_digest=current.policy_digest,
        dependencies=dict(current.dependencies),
    )
    with tempfile.TemporaryDirectory(prefix="ah-job-script-") as snapshot_dir:
        snapshot = Path(snapshot_dir) / target.name
        snapshot.write_bytes(approved_content)
        if target.suffix.lower() == ".py":
            command = [sys.executable, "-c", _PYTHON_SNAPSHOT_BOOTSTRAP, str(target), str(snapshot)]
        else:
            bash = shutil.which("bash", path="/usr/bin:/bin")
            if bash is None:
                raise ValueError("bash is required for shell scripts")
            command = [bash, "-c", _BASH_SNAPSHOT_BOOTSTRAP, str(target), str(snapshot)]
        return await _execute_script_command(command, target.parent)


async def _execute_script_command(command: list[str], cwd: Path) -> str:
    """Execute a prepared script command with bounded process-tree ownership."""
    environment = {
        key: value
        for key in ("PATH", "SystemRoot", "WINDIR", "TMP", "TEMP", "TMPDIR")
        if (value := os.environ.get(key)) is not None
    }
    # Python pipes default to the Windows locale encoding, while this runner
    # decodes output as UTF-8. Fix the encoding without inheriting user secrets.
    environment["PYTHONIOENCODING"] = "utf-8"
    if os.name != "nt":
        environment["PATH"] = "/usr/bin:/bin"
    windows = os.name == "nt"
    process_options = {} if windows else {"start_new_session": True}
    process = await asyncio.create_subprocess_exec(
        *([sys.executable, "-c", _WINDOWS_BOOTSTRAP, *command] if windows else command),
        cwd=str(cwd),
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
    return redact_secrets(stdout.decode("utf-8", errors="replace").strip()).text
