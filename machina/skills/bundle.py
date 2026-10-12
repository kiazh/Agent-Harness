"""Pinned, offline ECC distribution; external skill text never grants authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

from machina.skills.registry import Skill

PIN = "4eb71d92a39cab44ad40ac9d8a6a5ccb4029d6c2"
MANIFEST_SHA256 = "01b3ca9e35a5e9d4b2cc9a80a0c27eae4d6d07d241312aa9015a26ff0e28ed36"


class ECCBundle:
    """Read only hash-listed resources within the packaged source tree."""

    def __init__(self, root: Path | None = None) -> None:
        # Independent of MACHINA_SKILLS_DIR: local overrides cannot remove defaults.
        self.root = root or Path(__file__).resolve().parents[2] / "skills" / "ecc"
        payload = (self.root / "manifest.json").read_bytes()
        if hashlib.sha256(payload).hexdigest() != MANIFEST_SHA256:
            raise ValueError("ECC manifest integrity mismatch")
        self.manifest = json.loads(payload)
        if self.manifest["revision"] != PIN:
            raise ValueError("Unexpected ECC revision")
        self._content: dict[str, tuple[int, int, str]] = {}

    def read_resource(self, relative: str) -> str:
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or relative not in self.manifest["files"]:
            raise ValueError("Resource must be a manifest-listed ECC path")
        target = self.root / relative
        resolved = target.resolve()
        if not resolved.is_relative_to(self.root.resolve()) or target.is_symlink():
            raise ValueError("Resource escapes ECC bundle")
        stat = target.stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        cached = self._content.get(relative)
        if cached and cached[:2] == fingerprint:
            return cached[2]
        payload = target.read_bytes()
        if hashlib.sha256(payload).hexdigest() != self.manifest["files"][relative]:
            raise ValueError(f"ECC resource integrity mismatch: {relative}")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "Binary resource: use its packaged file path with an authorized tool"
            ) from exc
        self._content[relative] = (*fingerprint, text)
        return text

    def skills(self) -> list[Skill]:
        return [
            Skill(
                name=entry["name"],
                description=entry["description"],
                triggers=[],
                content="",
                file_path=str(self.root / entry["path"]),
                source=self.manifest["source"] + "/tree/" + PIN,
                source_type="builtin-ecc",
                metadata={"bundle_path": entry["path"], "revision": PIN},
            )
            for entry in self.manifest["skills"]
        ]

    def instructions(self, skill: Skill) -> str:
        return self.read_resource(skill.metadata["bundle_path"]).split("---", 2)[2].strip()

    def resource_paths(self, skill: Skill) -> list[str]:
        prefix = str(PurePosixPath(skill.metadata["bundle_path"]).parent) + "/"
        return [
            p
            for p in self.manifest["files"]
            if p.startswith(prefix) and not p.endswith("/SKILL.md")
        ]
