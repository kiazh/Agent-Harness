#!/usr/bin/env python3
"""Same-model head-to-head agent-harness runner: OpenCode vs Machina.

Runs ONE task in a fresh copy of ``--task-dir`` per harness, grades it with
pytest plus a test-file hash check, and reports machine-readable metrics.

Design notes
------------
* Stdlib only (no jq). Machina token metrics live in PostgreSQL; the query is
  delegated to the repo venv python (asyncpg lives there).
* OpenCode metrics come from its read-only sqlite DB (``~/.local/share/
  opencode/opencode.db``), never from the JSON stream, which undercounts.
* The Machina session is pre-created and granted ``full`` authority for agent
  ``harness`` before the CLI run; the CLI itself runs from inside the task
  working copy so the workspace root resolves to that directory.
* Every harness gets a pristine copy of the task dir, so runs never share
  state and the fixture on disk stays untouched.
* Secrets are never printed: only session ids, counts, and timings.

Usage
-----
    .venv/bin/python machina/research/head_to_head.py \\
        --harness both \\
        --task-dir /tmp/h2h/task1 \\
        --prompt "Make all tests in tests/test_calc.py pass. ..." \\
        --json-out /tmp/h2h/pilot.json

Prints one JSON document (report) to stdout and short human lines to stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# This runner lives in <repo>/machina/research/, so the repo root is parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
DEFAULT_OC_MODEL = "deepseek/deepseek-flash"
DEFAULT_MACHINA_AGENT = "harness"

HERMETIC_OPENCODE_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
}

# Artifacts that never count as "the harness changed the tests".
_IGNORED_PARTS = {"__pycache__", ".pytest_cache"}

_MACHINA_CREATE_SCRIPT = """
import asyncio, json, sys
sys.path.insert(0, {repo!r})
from machina.db.connection import db

async def main():
    await db.connect()
    try:
        await db.initialize_runtime_schema()
    except Exception:
        pass
    from machina.core.session import session_manager
    from machina.core.session_mode import activate_durable_mode
    session = await session_manager.create(title={title!r}, goal={goal!r})
    await activate_durable_mode(str(session.id), {agent!r}, "full")
    print(json.dumps({{"session_id": str(session.id)}}))
    await db.close()

asyncio.run(main())
"""

_MACHINA_USAGE_SCRIPT = """
import asyncio, json, sys
sys.path.insert(0, {repo!r})
from machina.db.connection import db

async def main():
    await db.connect()
    row = await db.fetchrow(
        "SELECT "
        "COALESCE(SUM(prompt_tokens), 0) AS tokens_input, "
        "COALESCE(SUM(completion_tokens), 0) AS tokens_output, "
        "COALESCE(SUM(accounted_tokens), 0) AS accounted_tokens, "
        "COUNT(*) AS requests, "
        "COUNT(*) FILTER (WHERE status = 'complete') AS completed_requests, "
        "COUNT(*) FILTER (WHERE status <> 'complete') AS incomplete_requests, "
        "string_agg(DISTINCT COALESCE(provider, '?') || ':' || COALESCE(model, '?'), ',') "
        "AS models "
        "FROM llm_usage WHERE session_id = $1::uuid",
        {sid!r},
    )
    print(json.dumps(dict(row)))
    await db.close()

asyncio.run(main())
"""


def log(message: str) -> None:
    print(f"[head_to_head] {message}", file=sys.stderr, flush=True)


def child_env() -> dict[str, str]:
    """Inherited env minus OpenCode harness markers; bytecode writes disabled."""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("OPENCODE") or key == "AI_AGENT":
            env.pop(key, None)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def _kill_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_cmd(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 240.0,
) -> dict:
    """Run a subprocess with a hard timeout; kills the whole process group.

    ``PWD`` is forced to match ``cwd``: OpenCode resolves the session project
    from the inherited ``PWD`` env var, not from the process cwd, so a stale
    value leaks the caller's project into the run.
    """
    start = time.monotonic()
    env = dict(env) if env is not None else dict(os.environ)
    env["PWD"] = str(cwd)
    proc = subprocess.Popen(
        [str(part) for part in cmd],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            _kill_group(proc, signal.SIGKILL)
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                # A detached grandchild can hold the pipes open forever; the
                # harness cap (timeout + ~5s worst case) must not be defeated
                # by a runner that never returns. Close our ends and move on.
                for stream in (proc.stdout, proc.stderr):
                    try:
                        stream.close()
                    except Exception:
                        pass
                stdout, stderr = "", ""
    return {
        "cmd": [str(part) for part in cmd],
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "wall_seconds": round(time.monotonic() - start, 3),
        "stdout": stdout,
        "stderr": stderr,
    }


def save_log(log_path: Path, raw: dict) -> None:
    """Persist raw subprocess output for audit/debugging."""
    log_path.write_text(
        "\n".join(
            [
                "===== CMD =====",
                " ".join(raw["cmd"]),
                f"exit_code={raw['exit_code']} timed_out={raw['timed_out']} "
                f"wall_seconds={raw['wall_seconds']}",
                "===== STDOUT =====",
                raw["stdout"],
                "===== STDERR =====",
                raw["stderr"],
            ]
        ),
        encoding="utf-8",
    )


def hash_tree(root: Path) -> dict[str, str]:
    """sha256 of every source file under ``root`` (pycache/pytest caches ignored)."""
    digests: dict[str, str] = {}
    if not root.exists():
        return digests
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in rel.parts) or path.suffix == ".pyc":
            continue
        digests[str(rel)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def git_capture(repo: Path) -> dict:
    """Per-run provenance: short HEAD plus sha256 of ``git status --short``.

    Recorded for both the harness-under-test tree (the machina repo) and the
    task fixture copy, so a run can be tied to an exact revision + dirty set.
    """
    try:
        head = run_cmd(["git", "rev-parse", "--short", "HEAD"], cwd=repo, timeout=30)
        status = run_cmd(["git", "status", "--short"], cwd=repo, timeout=30)
        status_text = status["stdout"] if status["exit_code"] == 0 else ""
        return {
            "git_head": head["stdout"].strip() if head["exit_code"] == 0 else None,
            "status_short": [line for line in status_text.splitlines() if line.strip()],
            "status_sha256_16": hashlib.sha256(status_text.encode("utf-8")).hexdigest()[:16],
        }
    except Exception as error:  # provenance must never kill a run
        return {"git_capture_error": f"{type(error).__name__}: {error}"}


# ---------------------------------------------------------------------------
# Machina
# ---------------------------------------------------------------------------


def machina_create_session(venv_python: Path, title: str, goal: str, agent_id: str) -> str:
    script = _MACHINA_CREATE_SCRIPT.format(
        repo=str(REPO_ROOT), title=title, goal=goal, agent=agent_id
    )
    result = run_cmd([str(venv_python), "-c", script], cwd=REPO_ROOT, env=child_env(), timeout=120)
    if result["exit_code"] != 0:
        raise RuntimeError(
            f"machina session setup failed (rc={result['exit_code']}, timed_out={result['timed_out']})"
        )
    for line in reversed(result["stdout"].splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("session_id"):
            return str(payload["session_id"])
    raise RuntimeError("machina session setup produced no session id")


def machina_usage(venv_python: Path, session_id: str, retries: int = 3) -> dict:
    script = _MACHINA_USAGE_SCRIPT.format(repo=str(REPO_ROOT), sid=session_id)
    for attempt in range(retries):
        result = run_cmd(
            [str(venv_python), "-c", script], cwd=REPO_ROOT, env=child_env(), timeout=60
        )
        if result["exit_code"] == 0:
            for line in reversed(result["stdout"].splitlines()):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        if attempt < retries - 1:
            time.sleep(1)
    log("machina usage query unavailable (llm_usage read failed)")
    return {}


def machina_audit(stderr: str) -> dict:
    """Last ``agent_run_complete`` audit record (stderr ``[AUDIT] {json}`` line)."""
    found: dict = {}
    for line in stderr.splitlines():
        if "[AUDIT]" not in line:
            continue
        brace = line.find("{")
        if brace < 0:
            continue
        try:
            payload = json.loads(line[brace:])
        except json.JSONDecodeError:
            continue
        if payload.get("event") == "agent_run_complete":
            found = payload
    return found


def run_machina(
    run_dir: Path,
    prompt: str,
    timeout: float,
    venv_python: Path,
    model: str | None,
    agent_id: str,
    title: str,
) -> dict:
    prep_start = time.monotonic()
    session_id = machina_create_session(venv_python, title, goal=prompt[:100], agent_id=agent_id)
    prep_seconds = round(time.monotonic() - prep_start, 3)

    machina_bin = venv_python.parent / "machina"
    cmd = [str(machina_bin), "chat", "--session", session_id]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)

    result = run_cmd(cmd, cwd=run_dir, env=child_env(), timeout=timeout)
    result["session_id"] = session_id
    result["prep_seconds"] = prep_seconds
    return result


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------


def opencode_session_id(stdout: str) -> str | None:
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = payload.get("sessionID")
        if session_id:
            return str(session_id)
    return None


def opencode_usage(db_path: Path, session_id: str, retries: int = 5) -> dict | None:
    """Read-only totals from OpenCode's sqlite DB; tool items parsed in Python."""
    if not db_path.exists():
        log(f"opencode db not found: {db_path}")
        return None
    last_error = "no attempt"
    for attempt in range(retries):
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
            try:
                row = connection.execute(
                    "SELECT tokens_input, tokens_output, tokens_reasoning, "
                    "tokens_cache_read, tokens_cache_write, cost, directory, model, agent "
                    "FROM session_v2 WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    last_error = "session row not visible yet"
                else:
                    tool_calls = 0
                    for (data,) in connection.execute(
                        "SELECT data FROM session_message "
                        "WHERE session_id = ? AND type = 'assistant'",
                        (session_id,),
                    ):
                        try:
                            payload = json.loads(data)
                        except (TypeError, json.JSONDecodeError):
                            continue
                        for item in payload.get("content") or []:
                            if isinstance(item, dict) and item.get("type") == "tool":
                                tool_calls += 1
                    return {
                        "tokens_input": row[0] or 0,
                        "tokens_output": row[1] or 0,
                        "tokens_reasoning": row[2] or 0,
                        "tokens_cache_read": row[3] or 0,
                        "tokens_cache_write": row[4] or 0,
                        "cost_usd": row[5],
                        "session_directory": row[6],
                        "model": row[7],
                        "agent": row[8],
                        "tool_calls": tool_calls,
                    }
            finally:
                connection.close()
        except sqlite3.Error as error:
            last_error = str(error)
        if attempt < retries - 1:
            time.sleep(2)
    log(f"opencode usage unavailable: {last_error}")
    return None


def run_opencode(
    run_dir: Path,
    prompt: str,
    model: str,
    agent: str,
    title: str,
    timeout: float,
) -> dict:
    # Project-local config grants hermetic auto-approval for the run dir.
    (run_dir / "opencode.json").write_text(
        json.dumps(HERMETIC_OPENCODE_CONFIG, indent=2) + "\n", encoding="utf-8"
    )
    cmd = [
        "opencode",
        "run",
        "--standalone",
        "--auto",
        "--format",
        "json",
        "--model",
        model,
        "--agent",
        agent,
        "--title",
        title,
        prompt,
    ]
    return run_cmd(cmd, cwd=run_dir, env=child_env(), timeout=timeout)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

_PYTEST_COUNT_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")


def grade(run_dir: Path, venv_python: Path, test_path: str, timeout: float = 120.0) -> dict:
    result = run_cmd(
        [str(venv_python), "-m", "pytest", test_path, "-q"],
        cwd=run_dir,
        env=child_env(),
        timeout=timeout,
    )
    text = result["stdout"] + "\n" + result["stderr"]
    counts: dict[str, int] = {}
    for value, label in _PYTEST_COUNT_RE.findall(text):
        label = "error" if label.startswith("error") else label
        counts[label] = counts.get(label, 0) + int(value)
    summary = ""
    for line in text.splitlines():
        if _PYTEST_COUNT_RE.search(line):
            summary = line.strip()
    return {
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "passed": counts.get("passed", 0),
        "failed": counts.get("failed", 0),
        "errors": counts.get("error", 0),
        "skipped": counts.get("skipped", 0),
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_one(harness: str, args: argparse.Namespace, task_dir: Path, venv_python: Path) -> dict:
    runs_dir = (
        Path(args.runs_dir).expanduser().resolve() if args.runs_dir else (task_dir.parent / "runs")
    )
    run_dir = runs_dir / task_dir.name / harness
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        task_dir,
        run_dir,
        symlinks=True,
        ignore=shutil.ignore_patterns(*_IGNORED_PARTS),
    )

    tests_dir = (run_dir / args.test_path).parent
    hashes_before = hash_tree(tests_dir)
    test_key = Path(args.test_path).name
    title = f"h2h:{task_dir.name}:{harness}"

    result: dict = {"harness": harness, "run_dir": str(run_dir)}

    try:
        if harness == "opencode":
            raw = run_opencode(
                run_dir, args.prompt, args.model, args.opencode_agent, title, args.timeout
            )
            log_path = run_dir.parent / f"{harness}.log"
            save_log(log_path, raw)
            session_id = opencode_session_id(raw["stdout"])
            usage = (
                opencode_usage(Path(args.opencode_db).expanduser(), session_id)
                if session_id
                else None
            )
            result.update(
                {
                    "session_id": session_id,
                    "wall_seconds": raw["wall_seconds"],
                    "harness_exit_code": raw["exit_code"],
                    "timed_out": raw["timed_out"],
                    "log_path": str(log_path),
                }
            )
            if usage:
                result.update(usage)
                result["total_tokens"] = (
                    usage["tokens_input"] + usage["tokens_output"] + usage["tokens_reasoning"]
                )
                session_directory = usage.get("session_directory")
                if session_directory and os.path.realpath(session_directory) != os.path.realpath(
                    run_dir
                ):
                    log(
                        "WARNING: opencode resolved its session dir to "
                        f"{session_directory} instead of {run_dir}"
                    )
                    result["session_dir_mismatch"] = True
            else:
                result.update(
                    {
                        "tokens_input": 0,
                        "tokens_output": 0,
                        "total_tokens": 0,
                        "tool_calls": 0,
                        "cost_usd": None,
                    }
                )
        elif harness == "machina":
            raw = run_machina(
                run_dir,
                args.prompt,
                args.timeout,
                venv_python,
                args.machina_model,
                args.machina_agent,
                title,
            )
            audit = machina_audit(raw["stderr"])
            log_path = run_dir.parent / f"{harness}.log"
            save_log(log_path, raw)
            usage = machina_usage(venv_python, raw["session_id"])
            tokens_input = int(usage.get("tokens_input", 0) or 0)
            tokens_output = int(usage.get("tokens_output", 0) or 0)
            total = tokens_input + tokens_output
            if total <= 0:
                total = int(usage.get("accounted_tokens", 0) or 0)
            result.update(
                {
                    "session_id": raw["session_id"],
                    "wall_seconds": raw["wall_seconds"],
                    "prep_seconds": raw["prep_seconds"],
                    "harness_exit_code": raw["exit_code"],
                    "timed_out": raw["timed_out"],
                    "tokens_input": tokens_input,
                    "tokens_output": tokens_output,
                    "total_tokens": total,
                    "accounted_tokens": int(usage.get("accounted_tokens", 0) or 0),
                    "llm_requests": int(usage.get("requests", 0) or 0),
                    "llm_incomplete_requests": int(usage.get("incomplete_requests", 0) or 0),
                    "tool_calls": int(audit.get("tool_calls_count", 0) or 0),
                    "iterations": audit.get("iterations"),
                    "audit_total_tokens": audit.get("total_tokens"),
                    "models": usage.get("models"),
                    "cost_usd": None,
                    "log_path": str(log_path),
                }
            )
        else:  # pragma: no cover - argparse choices prevent this
            raise ValueError(f"unknown harness {harness!r}")
    except Exception as error:  # report, do not crash the run pair
        result["error"] = f"{type(error).__name__}: {error}"
        result.setdefault("timed_out", False)

    # Per-run provenance: harness tree revision + fixture copy revision.
    result["harness_repo"] = git_capture(REPO_ROOT)
    result["task_repo"] = git_capture(run_dir)

    grade_result = grade(run_dir, venv_python, args.test_path)
    hashes_after = hash_tree(tests_dir)
    unchanged = hashes_before == hashes_after
    result["tests_passed"] = grade_result["passed"]
    result["tests_failed"] = grade_result["failed"] + grade_result["errors"]
    result["grade_exit_code"] = grade_result["exit_code"]
    result["pytest_summary"] = grade_result["summary"]
    result["test_file_sha256_before"] = hashes_before.get(test_key)
    result["test_file_sha256_after"] = hashes_after.get(test_key)
    result["test_files_unchanged"] = unchanged
    if not unchanged:
        result["test_files_diff"] = {
            "changed": sorted(
                name
                for name in set(hashes_before) & set(hashes_after)
                if hashes_before[name] != hashes_after[name]
            ),
            "added": sorted(set(hashes_after) - set(hashes_before)),
            "removed": sorted(set(hashes_before) - set(hashes_after)),
        }
    result["success"] = bool(
        grade_result["exit_code"] == 0
        and grade_result["passed"] > 0
        and unchanged
        and not result.get("timed_out")
    )
    return result


def summary_line(result: dict) -> str:
    return (
        f"{result['harness']}: success={result.get('success')} "
        f"wall={result.get('wall_seconds')}s "
        f"pytest={result.get('tests_passed')} passed/"
        f"{result.get('tests_failed')} failed "
        f"tokens={result.get('tokens_input')}/{result.get('tokens_output')}/"
        f"{result.get('total_tokens')} "
        f"tools={result.get('tool_calls')} cost={result.get('cost_usd')} "
        f"tests_intact={result.get('test_files_unchanged')} "
        f"session={result.get('session_id')}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Head-to-head same-model harness runner (OpenCode vs Machina)."
    )
    parser.add_argument("--harness", choices=("opencode", "machina", "both"), default="both")
    parser.add_argument("--task-dir", required=True, help="pristine task fixture directory")
    parser.add_argument("--prompt", required=True, help="identical prompt for both harnesses")
    parser.add_argument("--model", default=DEFAULT_OC_MODEL, help="OpenCode model")
    parser.add_argument("--machina-model", default=None, help="Machina model override")
    parser.add_argument("--machina-agent", default=DEFAULT_MACHINA_AGENT, help="Machina agent id")
    parser.add_argument("--opencode-agent", default="build", help="OpenCode agent")
    parser.add_argument("--runs-dir", default=None, help="where run copies live")
    parser.add_argument("--json-out", default=None, help="optional report path")
    parser.add_argument("--timeout", type=float, default=240.0, help="per-harness timeout (s)")
    parser.add_argument("--test-path", default="tests/test_calc.py", help="pytest target")
    parser.add_argument(
        "--venv", default=str(REPO_ROOT / ".venv"), help="repo venv (python + machina CLI)"
    )
    parser.add_argument("--opencode-db", default=str(DEFAULT_OPENCODE_DB))
    args = parser.parse_args(argv)

    task_dir = Path(args.task_dir).expanduser().resolve()
    if not task_dir.is_dir():
        parser.error(f"task dir not found: {task_dir}")
    if not (task_dir / args.test_path).is_file():
        parser.error(f"test path not found in task dir: {args.test_path}")

    venv = Path(args.venv).expanduser().resolve()
    venv_python = venv / "bin" / "python"
    if not venv_python.is_file():
        parser.error(f"venv python not found: {venv_python}")

    runs_dir = (
        Path(args.runs_dir).expanduser().resolve() if args.runs_dir else (task_dir.parent / "runs")
    )
    if runs_dir == task_dir or task_dir in runs_dir.parents:
        parser.error("--runs-dir must not be inside --task-dir")

    harnesses = ["opencode", "machina"] if args.harness == "both" else [args.harness]
    if "opencode" in harnesses and shutil.which("opencode") is None:
        parser.error("opencode executable not found on PATH")

    report: dict = {
        "task_dir": str(task_dir),
        "prompt": args.prompt,
        "model_opencode": args.model,
        "model_machina": args.machina_model,
        "timeout_seconds": args.timeout,
        "runs_dir": str(runs_dir),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "results": {},
    }
    for harness in harnesses:
        log(f"running {harness} on {task_dir.name} (timeout {args.timeout:g}s)")
        result = run_one(harness, args, task_dir, venv_python)
        report["results"][harness] = result
        log(summary_line(result))

    document = json.dumps(report, indent=2, ensure_ascii=False)
    print(document)
    if args.json_out:
        out_path = Path(args.json_out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(document + "\n", encoding="utf-8")
        log(f"report written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
