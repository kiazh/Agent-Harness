#!/usr/bin/env python
"""Audit finding: search_files('(a+)+$') on a 36-char line exceeds a
two-second process deadline (catastrophic backtracking; length guard is
not sufficient)."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
try:
    proc = subprocess.run(
        [sys.executable, str(HERE / "audit_redos_child.py")],
        capture_output=True,
        text=True,
        timeout=5,
    )
    print("child finished within 5s unexpectedly:", proc.stdout.strip(), proc.stderr.strip()[-200:])
    print("CLAIM NOT REPRODUCED")
    sys.exit(1)
except subprocess.TimeoutExpired:
    print("child still running after 5s on pattern '(a+)+$' against 36 'a's")
    print("CLAIM CONFIRMED (catastrophic backtracking outlives the deadline)")
    sys.exit(0)
