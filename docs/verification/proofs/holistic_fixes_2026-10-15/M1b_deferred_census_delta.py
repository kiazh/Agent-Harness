"""M1b deferred-import census attribution (run from repo root).

Reports the function-level (deferred) import census of ``machina/`` on the
working tree and the per-file deltas against HEAD, so the overflow over the
717 cap can be attributed. The M1b lane's files must show delta 0.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

OWNED = {
    "machina/db/connection.py",
    "machina/memory/approval.py",
    "machina/memory/store.py",
}


def count_deferred(source: str) -> int:
    tree = ast.parse(source)
    count = 0

    def walk(node: ast.AST, in_func: bool) -> None:
        nonlocal count
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)) and in_func:
                count += 1
            walk(child, in_func or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))

    walk(tree, False)
    return count


def head_source(path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"HEAD:{path}"], capture_output=True, text=True, check=False
    )
    return proc.stdout if proc.returncode == 0 else None


def main() -> None:
    root = Path(__file__).resolve().parents[4]
    total = 0
    deltas: list[tuple[str, int, int]] = []
    for path in sorted((root / "machina").rglob("*.py")):
        rel = str(path.relative_to(root))
        now = count_deferred(path.read_text(encoding="utf-8"))
        total += now
        before_source = head_source(rel)
        if before_source is None:
            deltas.append((rel, 0, now))
            continue
        before = count_deferred(before_source)
        if before != now:
            deltas.append((rel, before, now))

    print(f"tree census total: {total} (cap 717)")
    owned_delta = 0
    for rel, before, now in deltas:
        flag = "  [M1b-owned]" if rel in OWNED else ""
        print(f"  delta {now - before:+d}: {rel} ({before} -> {now}){flag}")
        if rel in OWNED:
            owned_delta += now - before
    print(f"M1b-owned files delta: {owned_delta:+d}")


if __name__ == "__main__":
    main()
