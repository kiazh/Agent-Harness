"""LANE F ratchet scan: deferred-import and line-count deltas for the LANE F files.

Reports, against HEAD, for the files this lane owns/touched:
  * deferred (function-level) import count — feeds DEFERRED_IMPORT_CAP (717);
  * total line count — feeds the god-file ceilings (no LANE F file is pinned);
  * the tree-wide deferred census (for context against the cap).
Run from the repo root:  .venv/bin/python docs/verification/proofs/holistic_fixes_2026-10-15/F_ratchet_scan.py
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
LANE_F_FILES = (
    "machina/core/firecrawl_lifecycle.py",
    "machina/cli/launcher.py",
    "machina/core/config.py",
    "tests/test_firecrawl_lifecycle.py",
)


def deferred_count(source: str) -> int:
    count = 0

    def walk(node: ast.AST, in_func: bool) -> None:
        nonlocal count
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)) and in_func:
                count += 1
            walk(child, in_func or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))

    walk(ast.parse(source), False)
    return count


def line_count(source: str) -> int:
    return source.count("\n") + (0 if source.endswith("\n") else 1)


def head_source(rel: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"HEAD:{rel}"], capture_output=True, text=True, check=False
    )
    return proc.stdout if proc.returncode == 0 else None


def main() -> None:
    print(f"{'file':45} {'deferred':>16} {'lines':>14}")
    for rel in LANE_F_FILES:
        now_source = (ROOT / rel).read_text(encoding="utf-8")
        now_deferred, now_lines = deferred_count(now_source), line_count(now_source)
        before_source = head_source(rel)
        if before_source is None:
            print(f"{rel:45} {now_deferred:>7} (new) {now_lines:>7} (new)")
            continue
        before_deferred, before_lines = deferred_count(before_source), line_count(before_source)
        print(
            f"{rel:45} {now_deferred:>7} ({now_deferred - before_deferred:+d}) "
            f"{now_lines:>7} ({now_lines - before_lines:+d})"
        )
    total = sum(
        deferred_count(p.read_text(encoding="utf-8")) for p in (ROOT / "machina").rglob("*.py")
    )
    print(f"\ntree deferred census: {total} (cap 717; overflow owned by other lanes)")


if __name__ == "__main__":
    main()
