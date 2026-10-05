"""TDD tests for SoulSpec bug fixes (bugs 3, 4, 5)."""
from __future__ import annotations

from pathlib import Path

from ah.soulspec.merge import SoulSpecMerger
from ah.soulspec.schema import SoulSpec

# ─── Bug 3: merge.py uses defaults as sentinels ─────────────────────────────


class TestMergeDefaultSentinels:
    """Bug 3: merge.py can't distinguish 'set to default' from 'not set'."""

    def test_merge_version_explicit_default(self) -> None:
        """Override with version='1.0.0' should use '1.0.0', not base's version."""
        base = SoulSpec(name="Base", version="2.0.0")
        override = SoulSpec(name="Override", version="1.0.0")
        merged = SoulSpecMerger.merge(base, override)
        assert merged.version == "1.0.0"

    def test_merge_max_iterations_explicit_default(self) -> None:
        """Override with max_iterations=10 should use 10, not base's value."""
        base = SoulSpec(name="Base", config=SoulSpec.Config(max_iterations=5))
        override = SoulSpec(name="Override", config=SoulSpec.Config(max_iterations=10))
        merged = SoulSpecMerger.merge(base, override)
        assert merged.config is not None
        assert merged.config.max_iterations == 10

    def test_merge_context_budget_explicit_default(self) -> None:
        """Override with context_budget=8000 should use 8000, not base's value."""
        base = SoulSpec(name="Base", config=SoulSpec.Config(context_budget=4000))
        override = SoulSpec(name="Override", config=SoulSpec.Config(context_budget=8000))
        merged = SoulSpecMerger.merge(base, override)
        assert merged.config is not None
        assert merged.config.context_budget == 8000

    def test_merge_version_none_uses_base(self) -> None:
        """Override with version=None should use base's version."""
        base = SoulSpec(name="Base", version="2.0.0")
        override = SoulSpec(name="Override", version=None)
        merged = SoulSpecMerger.merge(base, override)
        assert merged.version == "2.0.0"

    def test_merge_max_iterations_none_uses_base(self) -> None:
        """Override with max_iterations=None should use base's value."""
        base = SoulSpec(name="Base", config=SoulSpec.Config(max_iterations=5))
        override = SoulSpec(name="Override", config=SoulSpec.Config(max_iterations=None))
        merged = SoulSpecMerger.merge(base, override)
        assert merged.config is not None
        assert merged.config.max_iterations == 5

    def test_merge_context_budget_none_uses_base(self) -> None:
        """Override with context_budget=None should use base's value."""
        base = SoulSpec(name="Base", config=SoulSpec.Config(context_budget=4000))
        override = SoulSpec(name="Override", config=SoulSpec.Config(context_budget=None))
        merged = SoulSpecMerger.merge(base, override)
        assert merged.config is not None
        assert merged.config.context_budget == 4000


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
