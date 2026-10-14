#!/usr/bin/env python
"""Audit finding (lower): GNU find output-writing flags accepted as routine.

Policy acceptance only on this macOS host: BSD find lacks -fprint, so the
run documents that the write itself cannot execute here.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

work = Path(tempfile.mkdtemp(prefix="audit-find-"))
(work / "sample.txt").write_text("hello\n", encoding="utf-8")

from machina.core.config import config

config.workspace_root = str(work)

from machina.permissions.policy import build_request, decide

req = build_request(
    operation="process.exec",
    argv=["find", ".", "-name", "*.txt", "-fprint", "captured.txt"],
    cwd=".",
    mode="ask",
    backend="host",
    tool="terminal",
    session_id="00000000-0000-0000-0000-0000000000bb",
)
d = decide(req, [])
print(f"find . -name '*.txt' -fprint captured.txt -> {d.verdict} ({d.reason})")

# Control: -delete is classified destructive.
req_del = build_request(
    operation="process.exec", argv=["find", ".", "-delete"], cwd=".", mode="ask",
    backend="host", tool="terminal", session_id=req.session_id,
)
d_del = decide(req_del, [])
print(f"find . -delete -> {d_del.verdict} ({d_del.reason})")

proc = subprocess.run(
    ["find", ".", "-name", "*.txt", "-fprint", "captured.txt"],
    cwd=work, capture_output=True, text=True,
)
print("local find execution:", proc.returncode, (proc.stderr or proc.stdout).strip()[:120])
print("captured.txt created:", (work / "captured.txt").exists())

accepted = d.verdict == "allowed" and d_del.verdict == "pending"
print("CLAIM CONFIRMED at policy layer (write flag allowed; -delete asks)"
      if accepted else "CLAIM NOT REPRODUCED")
sys.exit(0 if accepted else 1)
