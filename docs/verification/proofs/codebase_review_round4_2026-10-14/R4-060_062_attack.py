"""R4-060/061/062 H-ATTACK probe (supplement to the pinned pytest matrix).

Runs hostile + benign commands/paths through the real ``build_request`` /
``decide()`` and the real broker (allowed path; no DB needed) and prints a
verdict table. Exits non-zero on any mismatch.

    uv run python docs/verification/proofs/codebase_review_round4_2026-10-14/R4-060_062_attack.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from machina.core.config import config
from machina.permissions.broker import permission_broker
from machina.permissions.policy import build_request, decide
from machina.permissions.tools import action_for_tool

CASES: list[tuple[str, str, str]] = [
    # (kind, operand, expected)  kind: cmd | path
    # ── R4-060 git grep walks (must pend) ──
    ("cmd", "git grep --cached REVIEW_TOKEN", "pending"),
    ("cmd", "git grep -w REVIEW_TOKEN", "pending"),
    ("cmd", "git grep -i REVIEW_TOKEN src", "pending"),
    ("cmd", "git grep --recurse-submodules REVIEW_TOKEN", "pending"),
    ("cmd", "git grep -e REVIEW_TOKEN", "pending"),
    ("cmd", "git grep -Osh REVIEW_TOKEN README.md", "pending"),
    ("cmd", "git grep --open-files-in-pager=sh REVIEW_TOKEN README.md", "pending"),
    ("cmd", "uv run git grep REVIEW_TOKEN", "pending"),
    ("cmd", "git --no-pager grep REVIEW_TOKEN", "pending"),
    # ── R4-060 git grep plain-file reads (routine) ──
    ("cmd", "git grep -e REVIEW_TOKEN README.md", "allowed"),
    ("cmd", "git grep --max-count=5 REVIEW_TOKEN README.md", "allowed"),
    ("cmd", "git grep -m 5 REVIEW_TOKEN README.md", "allowed"),
    ("cmd", "git grep -A 2 REVIEW_TOKEN README.md", "allowed"),
    # ── R4-060 revspecs (must pend credential_access / routine) ──
    ("cmd", "git show :credentials", "pending"),
    ("cmd", "git show HEAD:deploy/../credentials", "pending"),
    ("cmd", "git show HEAD:README.md", "allowed"),
    # ── R4-060 backup families (must pend) ──
    ("path", ".env.save", "pending"),
    ("path", ".env.swp", "pending"),
    ("path", "credentials.old", "pending"),
    ("path", ".aws/credentials.bak", "pending"),
    ("path", "id_ed25519~", "pending"),
    ("path", ".env.example", "allowed"),
    ("path", "docs/.env.example", "allowed"),
    # ── R4-061 calibration (routine) ──
    ("cmd", "wc -c README.md", "allowed"),
    ("cmd", "ls -C", "allowed"),
    ("cmd", "ls -o", "allowed"),
    ("cmd", "grep -C 2 REVIEW_TOKEN README.md", "allowed"),
    ("cmd", "head -c 100 README.md", "allowed"),
    ("cmd", "diff -c a.txt b.txt", "allowed"),
    # ── R4-061 retained review (must pend) ──
    ("cmd", "git -c alias.status=whoami status", "pending"),
    ("cmd", "git -c core.pager=sh log", "pending"),
    ("cmd", "python -c print(1)", "pending"),
    ("cmd", "python3 -c0", "pending"),
    ("cmd", "find README.md -o -name a", "pending"),
    ("cmd", "find . -name a -o -name b", "pending"),
    ("cmd", "grep -C2 REVIEW_TOKEN README.md", "pending"),
    ("cmd", "head -c100 README.md", "pending"),
    # Retained over-block (documented; not part of the listed calibration set)
    ("cmd", "rg -C 2 REVIEW_TOKEN README.md", "pending"),
    ("cmd", "wc -C README.md", "pending"),
    ("cmd", "ls -c", "pending"),
    ("cmd", "git grep -c REVIEW_TOKEN README.md", "pending"),
]


def main() -> int:
    root = Path(tempfile.mkdtemp()) / "project"
    root.mkdir(parents=True)
    (root / "README.md").write_text("REVIEW_TOKEN=docs\n", encoding="utf-8")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    (root / "src").mkdir()
    config.workspace_root = str(root)
    config.execution_mode = "ask"

    failures: list[str] = []
    rows: list[tuple[str, str, str, str]] = []
    allowed_requests = []
    for kind, operand, expected in CASES:
        if kind == "cmd":
            request = build_request(
                tool="terminal", **action_for_tool("terminal", {"command": operand})
            )
        else:
            request = build_request(operation="file.read", targets=[operand])
        verdict = decide(request).verdict
        status = "ok" if verdict == expected else "MISMATCH"
        if status == "MISMATCH":
            failures.append(f"{operand}: expected {expected}, got {verdict}")
        if verdict == "allowed":
            allowed_requests.append((operand, request))
        rows.append((operand, expected, verdict, status))

    async def run_broker() -> None:
        for operand, request in allowed_requests:
            # Allowed path is DB-free: the broker must pass it without a card.
            try:
                await permission_broker.guard(request)
            except Exception as error:  # noqa: BLE001 - probe reports any raise
                failures.append(f"broker guard refused allowed case {operand!r}: {error!r}")

    asyncio.run(run_broker())

    width = max(len(operand) for _, operand, _, _ in rows)
    for operand, expected, verdict, status in rows:
        print(f"{operand:<{width}}  expected={expected:<8} actual={verdict:<8} {status}")
    print(
        f"\n{len(rows)} cases, {len(failures)} mismatches; broker-guarded allowed cases: {len(allowed_requests)}"
    )
    for failure in failures:
        print(f"MISMATCH: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
