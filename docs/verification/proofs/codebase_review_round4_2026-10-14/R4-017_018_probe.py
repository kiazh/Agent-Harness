#!/usr/bin/env python
"""R4-017/R4-018 E2E probe (measurement companion to tests/test_cli_turn_e2e.py).

Runs real ``machina chat`` subprocesses against the loopback fake provider and
the ``machina_test`` database, and prints the measured SIGINT->exit latency,
exit code, released claim, and the full-mode provider request count.

Usage (from the repo root):

    MACHINA_TEST_DATABASE_URL=... .venv/bin/python \
        docs/verification/proofs/codebase_review_round4_2026-10-14/R4-017_018_probe.py
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests"))

import test_cli_turn_e2e as e2e  # noqa: E402


def sigint_scenario() -> None:
    with tempfile.TemporaryDirectory(prefix="r4-017-probe-") as raw:
        tmp = Path(raw)
        provider = e2e._FakeProvider(
            tool_name="terminal",
            tool_arguments={"command": "pytest slow_test.py", "timeout": 60},
            final_text="unused",
        )
        with provider:
            env, workspace = e2e._cli_env(tmp, provider, mode="ask")
            marker = workspace / "started.marker"
            (workspace / "slow_test.py").write_text(
                "import os, time\n"
                "from pathlib import Path\n"
                "Path(__file__).with_name('started.marker').write_text(str(os.getpid()))\n"
                "\n"
                "def test_slow_started():\n"
                "    time.sleep(60)\n",
                encoding="utf-8",
            )
            proc = e2e._spawn_chat(env)
            try:
                assert e2e._wait_for_path(marker, e2e.STARTUP_DEADLINE_S)
                slow_pid = int(marker.read_text(encoding="utf-8").strip() or 0)
                started = time.monotonic()
                proc.send_signal(signal.SIGINT)
                try:
                    output, _ = proc.communicate(timeout=e2e.SIGINT_EXIT_DEADLINE_S)
                    elapsed = time.monotonic() - started
                except Exception:
                    elapsed = time.monotonic() - started
                    e2e._kill_process_group(proc.pid)
                    output, _ = proc.communicate(timeout=5)
                    print(
                        f"R4-017: DEFERRED interrupt (no exit after "
                        f"{elapsed:.1f}s); killed. exit={proc.returncode}"
                    )
                    return
                session_id = e2e._session_id_from(output)
                state = asyncio.run(e2e._claim_state(session_id)) if session_id else None
                claim = state.get("claim_owner") if state else "session-not-parsed"
                print(
                    f"R4-017: SIGINT -> exit {proc.returncode} after {elapsed:.2f}s; "
                    f"claim_owner={claim!r}; slow_tool_pid={slow_pid}"
                )
            finally:
                if proc.poll() is None:
                    e2e._kill_process_group(proc.pid)
                    proc.wait(timeout=5)


def full_mode_scenario() -> None:
    with tempfile.TemporaryDirectory(prefix="r4-018-probe-") as raw:
        tmp = Path(raw)
        provider = e2e._FakeProvider(
            tool_name="read_file",
            tool_arguments={"path": "hello.txt"},
            final_text="HEADLESS FULL MODE COMPLETE",
        )
        with provider:
            env, workspace = e2e._cli_env(tmp, provider, mode="full")
            (workspace / "hello.txt").write_text("FULL MODE FILE CONTENT\n", encoding="utf-8")
            proc = e2e._spawn_chat(env)
            try:
                output, _ = proc.communicate(timeout=120)
            finally:
                if proc.poll() is None:
                    e2e._kill_process_group(proc.pid)
                    proc.wait(timeout=5)
            print(
                f"R4-018: exit={proc.returncode}; provider_requests="
                f"{provider.request_count()}; tool_result_reached_provider="
                f"{provider.saw_tool_result()}; grant_notice="
                f"{'FULL HOST mode active' in output}; paused="
                f"{'paused for approval' in output}"
            )


if __name__ == "__main__":
    if not os.environ.get("MACHINA_TEST_DATABASE_URL"):
        sys.exit("MACHINA_TEST_DATABASE_URL is required (machina_test only)")
    sigint_scenario()
    full_mode_scenario()
