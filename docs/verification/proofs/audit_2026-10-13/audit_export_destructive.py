#!/usr/bin/env python
"""Audit finding: exporting a soulspec package into a nonempty directory
deletes unrelated top-level files (write_package removes every file that is
not in the new manifest, including files the package never owned).
"""
import sys
import tempfile
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

from machina.soulspec.schema import SoulSpec

work = Path(tempfile.mkdtemp(prefix="audit-export-"))
important = work / "important.txt"
important.write_text("unrelated user data\n", encoding="utf-8")

spec = SoulSpec(
    name="audit-export",
    version="1.0.0",
    persona=SoulSpec.Persona(
        name="Audit", description="audit", system_prompt="Audit prompt"
    ),
    package_files={"SOUL.md": b"Audit prompt"},
    package_manifest={
        "specVersion": "0.5",
        "name": "audit-export",
        "displayName": "Audit",
        "version": "1.0.0",
        "description": "Audit",
        "author": {"name": "Audit"},
        "license": "MIT",
        "tags": [],
        "category": "general",
        "files": {"soul": "SOUL.md"},
    },
)

print("before export:", sorted(p.name for p in work.iterdir()))
spec.write_package(work)
print("after export :", sorted(p.name for p in work.iterdir()))
print(f"important.txt survived: {important.exists()}")

ok = not important.exists()
print("CLAIM CONFIRMED (unrelated file deleted)" if ok else "CLAIM NOT REPRODUCED")
sys.exit(0 if ok else 1)
