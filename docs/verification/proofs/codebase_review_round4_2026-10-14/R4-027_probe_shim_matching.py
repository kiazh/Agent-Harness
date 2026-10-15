"""R4-027 adversarial probe: whole-path-segment matching for _shim_points_at.

Usage:
  python probe_shim_matching.py unfixed <path-to-extracted-function.py>
  python probe_shim_matching.py live     # imports machina.cli from the worktree

Exit code 0 when every expectation holds; 1 otherwise (prints per-case table).
"""

from __future__ import annotations

import sys
from pathlib import Path

CASES: list[tuple[str, str, str, bool]] = [
    # label, shim_text, app_root, expected
    ("sibling machina-old", 'exec "/Users/x/machina-old/.venv/bin/machina" "$@"\n', "/Users/x/machina", False),
    ("sibling machina2", 'exec "/Users/x/machina2/.venv/bin/machina" "$@"\n', "/Users/x/machina", False),
    ("sibling machinaX", 'exec "/Users/x/machinaX/.venv/bin/machina" "$@"\n', "/Users/x/machina", False),
    ("sibling machina_old", 'exec "/Users/x/machina_old/.venv/bin/machina" "$@"\n', "/Users/x/machina", False),
    ("sibling machina.bak", 'exec "/Users/x/machina.bak/.venv/bin/machina" "$@"\n', "/Users/x/machina", False),
    ("exact path (bare)", "/Users/x/machina", "/Users/x/machina", True),
    ("exact path (quoted exec)", 'exec "/Users/x/machina" "$@"\n', "/Users/x/machina", True),
    ("path inside root", 'exec "/Users/x/machina/.venv/bin/machina" "$@"\n', "/Users/x/machina", True),
    ("containing dir (cd &&)", "cd /Users/x/machina && .venv/bin/machina chat\n", "/Users/x/machina", True),
    ("case-insensitive pos", 'exec "/USERS/X/MACHINA/.venv/bin/machina" "$@"\n', "/users/x/machina", True),
    ("case-insensitive neg", 'exec "/USERS/X/MACHINA2/.venv/bin/machina" "$@"\n', "/users/x/machina", False),
    ("trailing sep root pos", 'exec "/Users/x/machina/.venv/bin/machina" "$@"\n', "/Users/x/machina/", True),
    ("trailing sep root neg", 'exec "/Users/x/machina-old/.venv/bin/machina" "$@"\n', "/Users/x/machina/", False),
    ("relative prefix before", 'exec "old/x/machina/.venv/bin/machina" "$@"\n', "/x/machina", False),
    ("old existing pin pos", 'exec "/APP/.venv/bin/machina" "$@"\n', "/app", True),
    ("old existing pin neg", 'exec "/other/.venv/bin/machina" "$@"\n', "/app", False),
    (
        "windows path inside root",
        'exec "C:\\x\\machina\\.venv\\Scripts\\machina.exe" "$@"\n',
        r"C:\x\machina",
        True,
    ),
    (
        "windows sibling neg",
        'exec "C:\\x\\machina-old\\.venv\\Scripts\\machina.exe" "$@"\n',
        r"C:\x\machina",
        False,
    ),
    (
        "rejected first, valid later",
        'echo "/Users/x/machina-old is elsewhere"\nexec "/Users/x/machina/.venv/bin/machina" "$@"\n',
        "/Users/x/machina",
        True,
    ),
]


def load_source(which: str):
    if which == "unfixed":
        text = Path(sys.argv[2]).read_text(encoding="utf-8")
        ns: dict = {}
        exec(text, ns)  # noqa: S102 - trusted local extraction of HEAD
        return ns["_shim_points_at"]
    from machina.cli import _shim_points_at

    return _shim_points_at


def main() -> int:
    which = sys.argv[1]
    func = load_source(which)
    failures = 0
    print(f"probe source: {which}")
    for label, shim, root, expected in CASES:
        got = func(shim, root)
        ok = got is expected
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {label}: got={got} expected={expected}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed; {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
