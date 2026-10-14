# Review evidence — 2026-10-14

Companion report: [whole-codebase review](../../CODEBASE_REVIEW_2026-10-14.md).

These are reproductions, not fixes or a replacement for the repository test suite. They intentionally print the observed defects. Use only synthetic credentials, temporary workspaces, and the dedicated test database. Python fixture scripts use mocked providers/transports; they do not make paid model calls. `memory_pool.py` opens only `machina_test` and writes no rows; `scheduler.py` creates and deletes one temporary session there. Avoid running those alongside DB suites that clean test tables.

Run from the repository root with an isolated configuration. The proof files preserve the exact scripts used during review; some include this checkout's absolute path. For example:

```sh
PYTHONPATH=. .venv/bin/python docs/verification/proofs/codebase_review_2026-10-14/security.py
PYTHONPATH=. .venv/bin/python docs/verification/proofs/codebase_review_2026-10-14/rag_pem.py
PYTHONPATH=. .venv/bin/python docs/verification/proofs/codebase_review_2026-10-14/memory_watermark.py
PYTHONPATH=. .venv/bin/python docs/verification/proofs/codebase_review_2026-10-14/memory_and_skill.py
PYTHONPATH=. .venv/bin/python docs/verification/proofs/codebase_review_2026-10-14/memory_pool.py
PYTHONPATH=. .venv/bin/python docs/verification/proofs/codebase_review_2026-10-14/cli.py
PYTHONPATH=. .venv/bin/python docs/verification/proofs/codebase_review_2026-10-14/providers.py
PYTHONPATH=. .venv/bin/python docs/verification/proofs/codebase_review_2026-10-14/scheduler.py
PYTHONPATH=. .venv/bin/python docs/verification/proofs/codebase_review_2026-10-14/turn_release.py
```

The UI reproduction imports the real `ui/src` and installed UI dependency using the existing fake TUI/client pattern. To execute the saved copy without changing source, create a scratch directory with `src` and `node_modules` symlinks pointing to this checkout's `ui/src` and `ui/node_modules`, put `ui_approval_navigation.ts` in its `test` directory, and run it with the project's supported Node version. The review executed it that way and observed one awaiting-approval card before navigation, then zero cards, `working`, and an in-flight turn after returning.

Logs contain current test/check results. `lock_check.log` is an expected failing check proving the stale lock. `bandit.json` is the scanner output, which contains no medium/high findings despite the manually reproduced policy failures. Model/provider compatibility and timing qualifications are stated in the report.
