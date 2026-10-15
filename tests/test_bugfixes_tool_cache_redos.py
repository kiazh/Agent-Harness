"""Regression pins for three audited tool-layer defects (audit 2026-10-13).

1. The tool result cache was keyed by ``(tool, args)`` only, so a registry
   read in workspace B returned workspace A's cached content. Filesystem
   reads are now excluded from caching entirely (no freshness contract) and
   the remaining entries are scoped to the effective execution context.
2. Cache eligibility came from tool-name prefixes and ignored a declared
   ``effects=('exec',)``, so a mutating custom tool ran once and was served
   from cache afterwards. Eligibility now derives from ``Tool.effects``.
3. ``search_files('(a+)+$')`` ran catastrophic regex backtracking in a thread;
   an asyncio timeout cannot interrupt a thread, so the call outlived every
   deadline. Matching now runs in an isolated subprocess that is killed at
   the deadline and reported as a bounded ToolError.
"""

from __future__ import annotations

import time

import pytest

import machina.tools.builtins  # noqa: F401  (registers search_files)
from machina.tools.base import registry


@pytest.mark.asyncio
async def test_tool_cache_reads_are_workspace_scoped(tmp_path, monkeypatch):
    """A read in workspace B must never return workspace A's cached content."""
    from machina.core.config import config

    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    (workspace_a / "report.txt").write_text("workspace-a-content\n", encoding="utf-8")
    (workspace_b / "report.txt").write_text("workspace-b-content\n", encoding="utf-8")

    import machina.tools.file  # noqa: F401  (registers read_file)

    monkeypatch.setattr(config, "workspace_root", str(workspace_a))
    assert "workspace-a-content" in await registry.execute("read_file", path="report.txt")

    monkeypatch.setattr(config, "workspace_root", str(workspace_b))
    assert "workspace-b-content" in await registry.execute("read_file", path="report.txt")
    # Filesystem reads carry no freshness contract: the registry never caches,
    # so a re-read in either workspace always executes against that workspace.
    monkeypatch.setattr(config, "workspace_root", str(workspace_a))
    assert "workspace-a-content" in await registry.execute("read_file", path="report.txt")


@pytest.mark.asyncio
async def test_declared_exec_effect_tool_is_never_cache_hit():
    """effects=('exec',) must win over the name heuristics: two runs, not one."""
    executions: list[str] = []

    @registry.register(
        name="bugfixes_exec_effects_probe",
        description="test-only tool declaring exec effects",
        parameters={
            "type": "object",
            "properties": {"argument": {"type": "string"}},
            "required": ["argument"],
        },
        effects=("exec",),
    )
    def probe(argument: str) -> str:
        executions.append(argument)
        return f"run #{len(executions)}: {argument}"

    try:
        first = await registry.execute("bugfixes_exec_effects_probe", argument="same")
        second = await registry.execute("bugfixes_exec_effects_probe", argument="same")
    finally:
        registry._tools.pop("bugfixes_exec_effects_probe", None)

    assert first == "run #1: same"
    assert second == "run #2: same"
    assert executions == ["same", "same"]


@pytest.mark.asyncio
async def test_effect_free_tools_execute_fresh_every_call(tmp_path, monkeypatch):
    """R4-096: the registry keeps no result cache, so identical calls re-run.

    (Previously this test pinned a scoped TTL cache hit for effect-free
    tools; the cache was unreachable for every shipped tool — all declare
    broker effects — and has been removed. The real seam,
    ``registry.execute``, now runs the tool on every invocation in every
    execution context.)
    """
    from machina.core.config import config

    calls: list[str] = []

    @registry.register(
        name="bugfixes_scoped_cache_probe",
        description="test-only effect-free tool",
        parameters={
            "type": "object",
            "properties": {"tag": {"type": "string"}},
            "required": ["tag"],
        },
    )
    def probe(tag: str) -> str:
        calls.append(tag)
        return f"{tag}:{config.get('workspace_root')}"

    try:
        monkeypatch.setattr(config, "workspace_root", str(tmp_path / "a"))
        first = await registry.execute("bugfixes_scoped_cache_probe", tag="x")
        repeat = await registry.execute("bugfixes_scoped_cache_probe", tag="x")
        assert repeat == first
        assert len(calls) == 2  # no cache: every call executes

        monkeypatch.setattr(config, "workspace_root", str(tmp_path / "b"))
        other = await registry.execute("bugfixes_scoped_cache_probe", tag="x")
        assert other != first  # new context: executed, never a cross-context hit
        assert len(calls) == 3
    finally:
        registry._tools.pop("bugfixes_scoped_cache_probe", None)


@pytest.mark.asyncio
async def test_search_files_backtracking_is_killed_at_the_deadline(tmp_path, monkeypatch):
    """A catastrophic pattern fails fast with the bounded error, not later."""
    from machina.core.config import config
    from machina.core.exceptions import ToolError
    from machina.tools import builtins

    monkeypatch.setattr(config, "workspace_root", str(tmp_path))
    monkeypatch.setattr(builtins, "_SEARCH_DEADLINE_SECONDS", 0.5)
    (tmp_path / "evil.txt").write_text("a" * 36 + "!\n", encoding="utf-8")

    started = time.monotonic()
    with pytest.raises(ToolError, match="timed out"):
        await registry.execute("search_files", pattern="(a+)+$", path=str(tmp_path))
    # The isolated matcher is killed, so the call cannot outlive the deadline
    # by more than process/startup overhead.
    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
async def test_search_files_report_shape_is_unchanged(tmp_path, monkeypatch):
    """The isolated matcher preserves match and no-match report lines."""
    from machina.core.config import config

    monkeypatch.setattr(config, "workspace_root", str(tmp_path))
    (tmp_path / "hit.txt").write_text("alpha\nneedle here\n", encoding="utf-8")

    found = await registry.execute("search_files", pattern="needle", path=str(tmp_path))
    assert found.startswith("Found 1 matches:")
    assert "hit.txt:2: needle here" in found

    missing = await registry.execute("search_files", pattern="absent", path=str(tmp_path))
    assert missing == f"No matches for 'absent' in {tmp_path}"
