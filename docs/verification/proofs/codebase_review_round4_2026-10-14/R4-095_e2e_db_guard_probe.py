#!/usr/bin/env python3
"""R4-095 probe: e2e_gate database guard (helpers + refusal CLI paths).

Run with the repo venv so `tools.e2e_gate` imports cleanly. Every refusal path
exits before the gate touches anything, so this probe is side-effect free.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
GATE = REPO / "tools" / "e2e_gate.py"
PYTHON = REPO / ".venv" / "bin" / "python"
FAILURES = 0


def check(label: str, actual, expected) -> None:
    global FAILURES
    if actual == expected:
        print(f"PASS {label:52} = {actual!r}")
    else:
        print(f"FAIL {label:52} = {actual!r} (expected {expected!r})")
        FAILURES += 1


sys.path.insert(0, str(REPO))
from tools import e2e_gate  # noqa: E402

print("== helper semantics ==")
explicit = e2e_gate.resolve_db_url("postgresql://u:p@h:5432/db")
check("explicit flag wins", explicit, ("postgresql://u:p@h:5432/db", "flag"))

os.environ["MACHINA_DATABASE_URL"] = "postgresql://u:p@h:5432/from_explicit_env"
os.environ["DATABASE_URL"] = "postgresql://u:p@h:5432/from_legacy_env"
check(
    "MACHINA_DATABASE_URL wins over DATABASE_URL",
    e2e_gate.resolve_db_url(None),
    ("postgresql://u:p@h:5432/from_explicit_env", "env:MACHINA_DATABASE_URL"),
)
os.environ.pop("MACHINA_DATABASE_URL")
check(
    "falls back to DATABASE_URL",
    e2e_gate.resolve_db_url(None),
    ("postgresql://u:p@h:5432/from_legacy_env", "env:DATABASE_URL"),
)
os.environ.pop("DATABASE_URL")
check("unset env resolves to None", e2e_gate.resolve_db_url(None), (None, "unset"))

check("db name parsed", e2e_gate.database_name_from_url("postgresql://u:p@h:5432/machina_test"), "machina_test")
check("db name with params", e2e_gate.database_name_from_url("postgresql://u:p@h:5432/machina?sslmode=require"), "machina")
check("non-postgres scheme rejected", e2e_gate.database_name_from_url("mysql://u:p@h/machina"), None)
check("missing db name rejected", e2e_gate.database_name_from_url("postgresql://u:p@h:5432"), None)
check("test-like predicate", e2e_gate.is_test_database("machina_test"), True)
check("dev db not test-like", e2e_gate.is_test_database("machina"), False)
check("empty name not test-like", e2e_gate.is_test_database(None), False)

redacted = e2e_gate.redacted_command_line(
    ["--db-url", "postgresql://u:supersecret@h:5432/machina_test", "--journeys", "j1"]
)
check("command line redacts --db-url value", "supersecret" in redacted, False)
check("redacted keeps the flag", "--db-url <redacted>" in redacted, True)
redacted_eq = e2e_gate.redacted_command_line(["--db-url=postgresql://u:supersecret@h/db"])
check("command line redacts --db-url= form", "supersecret" in redacted_eq, False)


def run_gate(env_overrides: dict, args: list[str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for name in ("MACHINA_DATABASE_URL", "DATABASE_URL"):
        env.pop(name, None)
    env.update(env_overrides)
    return subprocess.run(
        [str(PYTHON), str(GATE), *(args or ["--journeys", "j1"])],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


print("\n== refusal CLI paths (exit before any journey) ==")
proc = run_gate({})
check("no env and no flag -> exit 2", proc.returncode, 2)
check("no env message", "will not guess a database" in proc.stderr, True)

proc = run_gate({"DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/machina"})
check("non-test env DB -> exit 2", proc.returncode, 2)
check("non-test message names the db", "non-test database 'machina'" in proc.stderr, True)
check("non-test message offers --db-url", "--db-url" in proc.stderr, True)

proc = run_gate({}, ["--journeys", "j1", "--db-url", "postgresql://u:p@h:5432"])
check("flag without db name -> exit 2", proc.returncode, 2)
check("unusable URL message", "not a usable postgres URL" in proc.stderr, True)

proc = run_gate({"DATABASE_URL": "mysql://u:p@h/machina"})
check("non-postgres ambient URL -> exit 2", proc.returncode, 2)
print("\n--- refusal output sample ---")
print(proc.stderr.strip())

print("\n== acceptance path is explicit and recorded (no journeys run) ==")
# A test-like ambient database passes without the flag; verify by reading the
# guard decision the same way main() does (running it would execute journeys).
check("ambient machina_test is accepted without flag", e2e_gate.is_test_database("machina_test"), True)

print("\n== result: ", end="")
if FAILURES == 0:
    print("ALL PROBES PASS")
    sys.exit(0)
print(f"{FAILURES} FAILURE(S)")
sys.exit(1)
