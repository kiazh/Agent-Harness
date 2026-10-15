"""Holistic review 2026-10-15, lane L4 — permission/tool hardening (H14, H18).

H14 (P1): recursive dereferencing copy bypasses containment and credential
checks.  Top-level operand checks never classified the tree a ``cp -R``
actually reaches; with ``-L`` that tree includes symlink targets, so a
workspace ``source/data`` link to an outside private file was copied into the
workspace as a regular file that later permitted reads expose.  No pathname
race is needed.

H18 (P2): ``search_files`` validated each candidate path, then passed the
original pathname to the isolated matcher, which re-opened it with a plain
``open()``.  A controlled swap of the file (or an ancestor) after validation
redirected that later read outside the workspace.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from machina.core.config import config
from machina.permissions.policy import build_request, decide
from machina.permissions.tools import action_for_tool


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(config, "workspace_root", str(root))
    monkeypatch.setattr(config, "execution_mode", "ask")
    return root


def _terminal_request(command: str):
    return build_request(tool="terminal", **action_for_tool("terminal", {"command": command}))


# ─── H14: recursive dereferencing copy ─────────────────────────────────────


def _outside_secret(workspace) -> tuple[str, object]:
    """Create an outside private file and a workspace link reaching it."""
    outside = workspace.parent / "outside-private"
    outside.mkdir(exist_ok=True)
    secret = outside / "private.txt"
    secret.write_text("OUTSIDE-PRIVATE-CONTENT\n", encoding="utf-8")
    source = workspace / "source"
    source.mkdir(exist_ok=True)
    (source / "notes.txt").write_text("public notes\n", encoding="utf-8")
    (source / "data").symlink_to(secret)
    return "OUTSIDE-PRIVATE-CONTENT", secret


@pytest.mark.parametrize(
    "command",
    [
        "cp -RL source clone",
        "cp -R --dereference source clone",
        "uv run cp -RL source clone",
        "cp -RL source/sub clone",
    ],
)
def test_recursive_dereferencing_copy_of_outside_file_requires_review(workspace, command):
    """A dereferenced descendant resolving outside the workspace gates review."""
    _outside_secret(workspace)
    nested = workspace / "source" / "sub"
    nested.mkdir()
    (nested / "deep").symlink_to(workspace.parent / "outside-private" / "private.txt")

    decision = decide(_terminal_request(command))
    assert decision.verdict == "pending", (command, decision.reason)
    assert "opaque_execution_review" in decision.reason, (command, decision.reason)


def test_recursive_dereferencing_copy_of_credential_target_requires_review(workspace):
    """A dereferenced descendant holding credential material gates exactly
    like a direct credential read, inside or outside the workspace."""
    (workspace / ".env").write_text("FAKE_SECRET=1\n", encoding="utf-8")
    source = workspace / "source"
    source.mkdir()
    (source / "config").symlink_to(workspace / ".env")

    inside = decide(_terminal_request("cp -RL source clone"))
    assert inside.verdict == "pending"
    assert "credential_access" in inside.reason

    outside = workspace.parent / "id_rsa"
    outside.write_text("FAKE PRIVATE KEY\n", encoding="utf-8")
    (source / "config").unlink()
    (source / "config").symlink_to(outside)
    leaked = decide(_terminal_request("cp -RL source clone"))
    assert leaked.verdict == "pending"
    assert "credential_access" in leaked.reason
    assert "opaque_execution_review" in leaked.reason


def test_recursive_dereferencing_copy_of_outside_directory_requires_review(workspace):
    """A link to an outside directory (no protected/sensitive names inside)
    still escapes containment when dereferenced."""
    outside = workspace.parent / "outside-tree" / "stuff"
    outside.mkdir(parents=True)
    (outside / "plain.txt").write_text("outside bytes\n", encoding="utf-8")
    source = workspace / "source"
    source.mkdir()
    (source / "tree").symlink_to(outside, target_is_directory=True)

    decision = decide(_terminal_request("cp -RL source clone"))
    assert decision.verdict == "pending"
    assert "opaque_execution_review" in decision.reason


def test_recursive_dereferencing_copy_of_sensitive_pair_through_link_requires_review(
    workspace,
):
    """A tree also walked literally is classified again through a link: the
    link renames the ancestry, so ``etc/passwd`` under an innocuous link name
    is no longer sensitive by path and would leak as ``clone/data/passwd``."""
    source = workspace / "source"
    (source / "etc").mkdir(parents=True)
    (source / "etc" / "passwd").write_text("root:x:0:0\n", encoding="utf-8")
    (source / "data").symlink_to(source / "etc", target_is_directory=True)

    decision = decide(_terminal_request("cp -RL source clone"))
    assert decision.verdict == "pending"
    assert "credential_access" in decision.reason


def test_dereferencing_copy_would_materialize_outside_bytes(workspace):
    """The effect H14's gate must prevent: ``cp -L`` materializes the outside
    target as a regular workspace file (demonstrated directly with cp; the
    policy tests above pin that this command now requires review)."""
    _outside_secret(workspace)
    subprocess.run(["cp", "-RL", "source", "clone"], cwd=workspace, check=True)
    copied = workspace / "clone" / "data"
    assert not copied.is_symlink()
    assert copied.read_text(encoding="utf-8") == "OUTSIDE-PRIVATE-CONTENT\n"


def test_recursive_copy_without_dereference_stays_routine(workspace):
    """Without ``-L`` the link is copied as a link; reads through it are
    rejected at read time, so the copy stays routine (no over-blocking)."""
    _outside_secret(workspace)
    for command in ("cp -R source clone", "cp -RP source clone", "cp -Ra source clone"):
        assert decide(_terminal_request(command)).verdict == "allowed", command


def test_recursive_dereferencing_copy_with_inside_links_stays_routine(workspace):
    """Ordinary in-workspace dereferences are not review-worthy."""
    source = workspace / "source"
    source.mkdir()
    (source / "notes.txt").write_text("hello\n", encoding="utf-8")
    (source / "alias.txt").symlink_to(source / "notes.txt")
    subdir = source / "sub"
    subdir.mkdir()
    (subdir / "more.txt").write_text("more\n", encoding="utf-8")
    (source / "linked-dir").symlink_to(subdir, target_is_directory=True)

    assert decide(_terminal_request("cp -RL source clone")).verdict == "allowed"
    assert decide(_terminal_request("cp -RL source/. clone")).verdict == "allowed"


# ─── H18: search_files read boundary ───────────────────────────────────────


async def test_search_files_ignores_a_swapped_symlink(tmp_path, monkeypatch):
    """The matcher re-opens by pathname; a swap after validation must not
    redirect the read to the outside file that the swap now targets."""
    from machina.tools import builtins

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(config, "workspace_root", str(root))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("OUTSIDE-PRIVATE-CONTENT\n", encoding="utf-8")
    (root / "notes.txt").write_text("public notes\n", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to(root / "notes.txt")  # inside target at validation time

    real_create = asyncio.create_subprocess_exec
    swapped = False

    async def create_with_swap(*args, **kwargs):
        # Controlled swap at the validation/read boundary: the matcher is
        # about to re-open `link.txt` by pathname.
        nonlocal swapped
        if not swapped:
            swapped = True
            link.unlink()
            link.symlink_to(outside / "private.txt")
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_with_swap)
    # Search a substring of the outside line so the no-match report cannot be
    # confused with the leaked content line itself.
    result = await builtins.search_files("PRIVATE-CONTENT", ".")

    assert swapped, "the controlled swap never ran"
    # The swap really redirects a plain pathname open...
    assert (root / "link.txt").read_text(encoding="utf-8").startswith("OUTSIDE-PRIVATE")
    # ...but the search must not expose the outside bytes.
    assert "OUTSIDE-PRIVATE-CONTENT" not in result


async def test_search_files_still_follows_in_workspace_symlinks(tmp_path, monkeypatch):
    """Pinning the safe behavior: an ordinary in-workspace symlink is still
    searched (no false negatives from the identity check)."""
    from machina.tools import builtins

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(config, "workspace_root", str(root))
    (root / "real.txt").write_text("needle here\n", encoding="utf-8")
    (root / "link.txt").symlink_to(root / "real.txt")

    result = await builtins.search_files("needle", ".")
    assert "link.txt:1: needle here" in result
