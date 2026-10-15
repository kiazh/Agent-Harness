"""RED probe: the repo-join evidence tests are sensitive to the fix.

Replaces ``machina.tools.repo._run_worker_joined`` with the pre-fix behavior
(bare ``asyncio.to_thread``) in this process only, then runs the same
cancelled-install scenario as
``tests/test_final_review_2026_10_15_fix_batch3.py::test_repo_activate_joins_install_worker_on_cancel``.

Expected pre-fix output: ``finished set after cancel = False`` — the cancelled
await returns before the worker's effects complete, i.e. the worker is
detached. Post-fix the pytest test asserts ``finished.is_set()`` is True.
"""

from __future__ import annotations

import asyncio
import threading

import machina.tools.repo as repo_tools


async def main() -> None:
    # Pre-fix behavior: an unshielded worker whose await can be cancelled.
    repo_tools._run_worker_joined = lambda op: asyncio.to_thread(op)

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    repo_tools.repo_install.load_staged_plan = lambda digest: ({}, {})
    repo_tools.skills_dir_for_install = lambda: None

    def gated_activate(detail, staged, skills_dir):
        entered.set()
        release.wait(timeout=5)
        finished.set()
        return ["alpha"]

    repo_tools.repo_install.activate_plan = gated_activate

    task = asyncio.create_task(repo_tools.repo_activate("d" * 64))
    assert await asyncio.to_thread(entered.wait, 5), "install worker never entered"
    timer = threading.Timer(0.2, release.set)
    timer.daemon = True
    timer.start()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    print("pre-fix (bare to_thread): finished set after cancel =", finished.is_set())
    timer.join(timeout=5)


if __name__ == "__main__":
    asyncio.run(main())
