"""TDD tests for SoulSpec bug fixes (bugs 3, 4, 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from machina.soulspec.merge import SoulSpecMerger
from machina.soulspec.schema import SoulSpec

# ─── Bug 3: merge.py uses defaults as sentinels ─────────────────────────────


class TestMergeDefaultSentinels:
    """Bug 3: merge.py can't distinguish 'set to default' from 'not set'."""

    @pytest.mark.parametrize(
        "version,expected",
        [
            pytest.param("1.0.0", "1.0.0", id="explicit-default-wins"),
            pytest.param(None, "2.0.0", id="none-falls-back-to-base"),
        ],
    )
    def test_merge_version_sentinel(self, version, expected) -> None:
        """Explicit version wins; None falls back to the base version."""
        base = SoulSpec(name="Base", version="2.0.0")
        override = SoulSpec(name="Override", version=version)
        merged = SoulSpecMerger.merge(base, override)
        assert merged.version == expected

    @pytest.mark.parametrize(
        "max_iterations,expected",
        [
            pytest.param(10, 10, id="explicit-default-wins"),
            pytest.param(None, 5, id="none-falls-back-to-base"),
        ],
    )
    def test_merge_max_iterations_sentinel(self, max_iterations, expected) -> None:
        """Explicit max_iterations wins; None falls back to the base value."""
        base = SoulSpec(name="Base", config=SoulSpec.Config(max_iterations=5))
        override = SoulSpec(name="Override", config=SoulSpec.Config(max_iterations=max_iterations))
        merged = SoulSpecMerger.merge(base, override)
        assert merged.config is not None
        assert merged.config.max_iterations == expected

    @pytest.mark.parametrize(
        "context_budget,expected",
        [
            pytest.param(8000, 8000, id="explicit-default-wins"),
            pytest.param(None, 4000, id="none-falls-back-to-base"),
        ],
    )
    def test_merge_context_budget_sentinel(self, context_budget, expected) -> None:
        """Explicit context_budget wins; None falls back to the base value."""
        base = SoulSpec(name="Base", config=SoulSpec.Config(context_budget=4000))
        override = SoulSpec(name="Override", config=SoulSpec.Config(context_budget=context_budget))
        merged = SoulSpecMerger.merge(base, override)
        assert merged.config is not None
        assert merged.config.context_budget == expected


# ─── Bug 4: write_package doesn't clean stale files ─────────────────────────


class TestWritePackageStaleFiles:
    """Bug 4: write_package should remove files not in the new manifest."""

    def test_write_package_removes_stale_files(self, tmp_path: Path) -> None:
        """Files from a previous write that are not in the new manifest are removed."""
        # First write: package with an extra file
        spec1 = SoulSpec(
            name="test",
            version="1.0.0",
            persona=SoulSpec.Persona(name="Test", description="Test", system_prompt="Test prompt"),
            package_files={"SOUL.md": b"Test prompt", "extra.md": b"Extra content"},
            package_manifest={
                "specVersion": "0.5",
                "name": "test",
                "displayName": "Test",
                "version": "1.0.0",
                "description": "Test",
                "author": {"name": "Test"},
                "license": "MIT",
                "tags": [],
                "category": "general",
                "files": {"soul": "SOUL.md", "extra": "extra.md"},
            },
        )
        spec1.write_package(tmp_path)
        assert (tmp_path / "SOUL.md").exists()
        assert (tmp_path / "extra.md").exists()

        # Second write: same package without the extra file
        spec2 = SoulSpec(
            name="test",
            version="1.0.0",
            persona=SoulSpec.Persona(name="Test", description="Test", system_prompt="Test prompt"),
            package_files={"SOUL.md": b"Test prompt"},
            package_manifest={
                "specVersion": "0.5",
                "name": "test",
                "displayName": "Test",
                "version": "1.0.0",
                "description": "Test",
                "author": {"name": "Test"},
                "license": "MIT",
                "tags": [],
                "category": "general",
                "files": {"soul": "SOUL.md"},
            },
        )
        spec2.write_package(tmp_path)
        assert (tmp_path / "SOUL.md").exists()
        assert not (tmp_path / "extra.md").exists()

    def test_write_package_cleans_multiple_stale_files(self, tmp_path: Path) -> None:
        """Multiple stale files are all removed."""
        spec1 = SoulSpec(
            name="test",
            version="1.0.0",
            persona=SoulSpec.Persona(name="Test", description="Test", system_prompt="Test prompt"),
            package_files={
                "SOUL.md": b"Test prompt",
                "a.md": b"A",
                "b.md": b"B",
                "c.md": b"C",
            },
            package_manifest={
                "specVersion": "0.5",
                "name": "test",
                "displayName": "Test",
                "version": "1.0.0",
                "description": "Test",
                "author": {"name": "Test"},
                "license": "MIT",
                "tags": [],
                "category": "general",
                "files": {"soul": "SOUL.md", "a": "a.md", "b": "b.md", "c": "c.md"},
            },
        )
        spec1.write_package(tmp_path)
        assert (tmp_path / "a.md").exists()
        assert (tmp_path / "b.md").exists()
        assert (tmp_path / "c.md").exists()

        spec2 = SoulSpec(
            name="test",
            version="1.0.0",
            persona=SoulSpec.Persona(name="Test", description="Test", system_prompt="Test prompt"),
            package_files={"SOUL.md": b"Test prompt"},
            package_manifest={
                "specVersion": "0.5",
                "name": "test",
                "displayName": "Test",
                "version": "1.0.0",
                "description": "Test",
                "author": {"name": "Test"},
                "license": "MIT",
                "tags": [],
                "category": "general",
                "files": {"soul": "SOUL.md"},
            },
        )
        spec2.write_package(tmp_path)
        assert (tmp_path / "SOUL.md").exists()
        assert not (tmp_path / "a.md").exists()
        assert not (tmp_path / "b.md").exists()
        assert not (tmp_path / "c.md").exists()

    def test_write_package_preserves_unrelated_files(self, tmp_path: Path) -> None:
        """Audit: a nonempty destination without an owning manifest is not clobbered."""
        important = tmp_path / "important.txt"
        important.write_text("unrelated user data", encoding="utf-8")

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
        spec.write_package(tmp_path)

        assert important.read_text(encoding="utf-8") == "unrelated user data"
        assert (tmp_path / "SOUL.md").exists()
        assert (tmp_path / "soul.json").exists()

    def test_write_package_removes_only_manifest_owned_stale_files(self, tmp_path: Path) -> None:
        """Audit: unrelated files survive re-export over an existing package."""
        spec = SoulSpec(
            name="audit-keep",
            version="1.0.0",
            persona=SoulSpec.Persona(
                name="Audit", description="audit", system_prompt="Audit prompt"
            ),
            package_files={"SOUL.md": b"Audit prompt"},
            package_manifest={
                "specVersion": "0.5",
                "name": "audit-keep",
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
        spec.write_package(tmp_path)
        keep = tmp_path / "keep.txt"
        keep.write_text("keep me", encoding="utf-8")

        # Re-export over the existing package: only manifest-owned stale files
        # may be replaced/removed; unrelated destination files are untouched.
        spec.write_package(tmp_path)
        assert keep.read_text(encoding="utf-8") == "keep me"
        assert (tmp_path / "SOUL.md").exists()
