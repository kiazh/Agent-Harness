"""FM3-RF6-F5/F6: denied-only permissions rejected; package import bounded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from machina.soulspec.schema import _NO_TOOLS_SENTINEL, SoulSpec


def _base_package(root):
    spec = SoulSpec(
        name="bounded-persona",
        persona=SoulSpec.Persona(
            name="Bounded",
            description="Bounded package for import tests",
            system_prompt="You are bounded.",
        ),
    )
    spec.write_package(root, author="Test user")
    return root


# ─── RF6-F5: denied-only permissions ───────────────────────────────────────


def test_denied_only_permissions_rejected_at_parse():
    with pytest.raises(ValueError, match="denied_tools"):
        SoulSpec.from_dict({"name": "x", "config": {"permissions": {"denied_tools": ["terminal"]}}})


def test_allowed_and_denied_permissions_still_parse():
    spec = SoulSpec.from_dict(
        {
            "name": "x",
            "config": {
                "permissions": {"allowed_tools": ["read_file"], "denied_tools": ["terminal"]}
            },
        }
    )
    assert spec.to_agent_def().tools == ["read_file"]


def test_explicit_deny_all_is_not_a_denied_only_parse_error():
    # deny_all is the explicit no-tools mechanism (and the storage round-trip
    # representation of __no_tools__), so it must keep parsing.
    spec = SoulSpec.from_dict({"name": "x", "config": {"permissions": {"deny_all": True}}})
    assert spec.to_agent_def().tools == [_NO_TOOLS_SENTINEL]


# ─── RF6-F6: per-file size/encoding caps on package import ─────────────────


def test_oversized_referenced_package_file_rejected(tmp_path):
    _base_package(tmp_path)
    manifest = json.loads((tmp_path / "soul.json").read_text(encoding="utf-8"))
    manifest["files"]["extra"] = "EXTRA.md"
    (tmp_path / "EXTRA.md").write_bytes(b"a" * (256 * 1024 + 1))
    (tmp_path / "soul.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="size"):
        SoulSpec.from_package(tmp_path)


def test_non_utf8_referenced_package_file_rejected(tmp_path):
    _base_package(tmp_path)
    manifest = json.loads((tmp_path / "soul.json").read_text(encoding="utf-8"))
    manifest["files"]["extra"] = "EXTRA.md"
    (tmp_path / "EXTRA.md").write_bytes(b"\xff\xfe\x80 binary \x00")
    (tmp_path / "soul.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="UTF-8"):
        SoulSpec.from_package(tmp_path)


def test_oversized_soul_json_rejected(tmp_path):
    _base_package(tmp_path)
    (tmp_path / "soul.json").write_bytes(b'{"padding": "' + b"a" * (256 * 1024) + b'"}')
    with pytest.raises(ValueError, match="size"):
        SoulSpec.from_package(tmp_path)


# ─── R4-091: pre-stat guards so a symlinked huge file never gets read ───────


def test_symlinked_oversized_package_file_rejected_before_read(tmp_path, monkeypatch):
    _base_package(tmp_path)
    huge = tmp_path / "HUGE.md"
    with huge.open("wb") as handle:
        handle.truncate(256 * 1024 + 1)
    link = tmp_path / "LINKED.md"
    link.symlink_to(huge)
    manifest = json.loads((tmp_path / "soul.json").read_text(encoding="utf-8"))
    manifest["files"]["linked"] = "LINKED.md"
    (tmp_path / "soul.json").write_text(json.dumps(manifest), encoding="utf-8")
    reads = []
    real_read_bytes = Path.read_bytes

    def spy(path):
        reads.append(path)
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", spy)
    with pytest.raises(ValueError, match="size"):
        SoulSpec.from_package(tmp_path)
    assert huge not in reads


def test_package_with_too_many_referenced_files_rejected(tmp_path):
    _base_package(tmp_path)
    manifest = json.loads((tmp_path / "soul.json").read_text(encoding="utf-8"))
    for index in range(65):
        name = f"doc{index:02d}.md"
        (tmp_path / name).write_text("ok", encoding="utf-8")
        manifest["files"][f"doc{index:02d}"] = name
    (tmp_path / "soul.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="too many"):
        SoulSpec.from_package(tmp_path)
