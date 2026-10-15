"""R4-090 tools cluster — adversarial checks (RED-first).

Per-bullet surface:

- ``read_file`` applies offset/limit BEFORE the byte gate so large files can
  be paged; truncation at ``limit`` reports the total line count and the next
  offset instead of truncating silently.
- ``write_file`` is atomic (same-directory temp + fsync + replace): an
  ENOSPC failure injected mid-write cannot destroy the original file and
  leaves no partial temp behind; an existing file keeps its mode.
- ``harness_status._persona_summary`` distinguishes a persona-resolution
  FAILURE from "no agent definition" with a typed marker, and never leaks the
  exception text.
- Every schema sent to the model declares ``additionalProperties`` and the
  runtime validator agrees with the declaration (no drift).
- The five tools that used to ship without ``parameters`` declare real
  parameter schemas with units/ranges/defaults.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import uuid
from unittest.mock import AsyncMock

import pytest

from machina.core.exceptions import ToolError, ValidationError

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_ARG_SAMPLE = {
    "string": "x",
    "integer": 1,
    "number": 1.0,
    "boolean": True,
    "array": [],
    "object": {},
}


def _required_args(schema: dict) -> dict:
    """Minimal valid argument dict for *schema* (required parameters only)."""
    args = {}
    for name in schema.get("required", []):
        prop = schema.get("properties", {}).get(name, {})
        args[name] = _ARG_SAMPLE.get(prop.get("type", "string"), "x")
    return args


def _file_module(tmp_path, monkeypatch):
    import machina.tools.file as fmod

    monkeypatch.setattr(fmod, "_BASE_DIR", tmp_path.resolve())
    return fmod


# ---------------------------------------------------------------------------
# (a) read_file: paging before the size gate + truncation marker
# ---------------------------------------------------------------------------


async def test_read_file_pages_a_file_larger_than_max_size(tmp_path, monkeypatch):
    """A file over max_size is still readable a window at a time."""
    fmod = _file_module(tmp_path, monkeypatch)
    lines = [f"line-{i:05d} " + "x" * 240 for i in range(1, 401)]
    target = tmp_path / "large.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert target.stat().st_size > 100_000  # far beyond the page budget

    # A window deep in the file (300 * ~250B >> max_size) must be reachable.
    page = await fmod.read_file("large.txt", offset=300, limit=5, max_size=4096)

    assert page.splitlines()[:5] == lines[299:304]
    assert "of 400" in page, "truncation marker must report the total line count"
    assert "offset=305" in page, "marker must point at the next page"

    # The byte gate still refuses a window that is itself oversized.
    with pytest.raises(ToolError, match="too large|exceeds|max_size|size limit"):
        await fmod.read_file("large.txt", limit=400, max_size=4096)


async def test_read_file_marker_reports_truncation_and_last_page_is_clean(tmp_path, monkeypatch):
    """Truncation at limit is explicit; a page that reaches EOF is unmarked."""
    fmod = _file_module(tmp_path, monkeypatch)
    (tmp_path / "notes.txt").write_text(
        "\n".join(f"line-{i}" for i in range(1, 11)) + "\n", encoding="utf-8"
    )

    page = await fmod.read_file("notes.txt", limit=4)
    assert "line-1\n" in page and "line-4\n" in page
    assert "line-5" not in page
    assert "of 10" in page
    assert "offset=5" in page

    middle = await fmod.read_file("notes.txt", offset=5, limit=4)
    assert middle.startswith("line-5")
    assert "line-8" in middle
    assert "of 10" in middle and "offset=9" in middle

    last = await fmod.read_file("notes.txt", offset=9, limit=4)
    assert last.startswith("line-9")
    assert "line-10" in last
    assert "offset=" not in last, "nothing follows the final page"

    complete = await fmod.read_file("notes.txt", limit=50)
    assert complete == "\n".join(f"line-{i}" for i in range(1, 11)) + "\n"


async def test_read_file_small_file_result_is_unchanged(tmp_path, monkeypatch):
    """Files that fit entirely in one page carry no marker."""
    fmod = _file_module(tmp_path, monkeypatch)
    (tmp_path / "small.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    assert await fmod.read_file("small.txt") == "alpha\nbeta\n"
    assert await fmod.read_file("small.txt", offset=2) == "beta\n"


async def test_read_file_pages_past_a_giant_line_without_buffering_it(tmp_path, monkeypatch):
    """Skipping an out-of-window giant line stays piece-bounded."""
    fmod = _file_module(tmp_path, monkeypatch)
    target = tmp_path / "giant.txt"
    target.write_text("a" * (2 * 1024 * 1024) + "\ntarget-line\n", encoding="utf-8")

    page = await fmod.read_file("giant.txt", offset=2, limit=5, max_size=4096)

    assert page == "target-line\n"


# ---------------------------------------------------------------------------
# (b) write_file: atomic temp + fsync + replace
# ---------------------------------------------------------------------------


async def test_write_file_enospc_mid_write_preserves_original(tmp_path, monkeypatch):
    """An injected ENOSPC during the write cannot destroy the original."""
    fmod = _file_module(tmp_path, monkeypatch)
    workdir = tmp_path / "data"
    workdir.mkdir()
    target = workdir / "report.txt"
    target.write_text("ORIGINAL", encoding="utf-8")

    observed = []
    real_fdopen = os.fdopen

    class _PartialFailure:
        def __init__(self, handle):
            self._handle = handle

        def write(self, data):
            observed.append(target.read_text(encoding="utf-8"))
            self._handle.write(data[:8])
            self._handle.flush()
            raise OSError(errno.ENOSPC, "No space left on device")

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._handle.close()
            return False

        def fileno(self):
            return self._handle.fileno()

    def failing_fdopen(fd, *args, **kwargs):
        return _PartialFailure(real_fdopen(fd, *args, **kwargs))

    monkeypatch.setattr(os, "fdopen", failing_fdopen)
    with pytest.raises(ToolError):
        await fmod.write_file("data/report.txt", "x" * 4096)

    assert observed == ["ORIGINAL"], "the original was already damaged mid-write"
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    assert [p.name for p in workdir.iterdir()] == ["report.txt"], "temp file leaked"


async def test_write_file_fsync_failure_leaves_original_and_no_temp(tmp_path, monkeypatch):
    """A failure after the write but before replace is still non-destructive."""
    fmod = _file_module(tmp_path, monkeypatch)
    target = tmp_path / "report.txt"
    target.write_text("ORIGINAL", encoding="utf-8")

    def failing_fsync(_fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(ToolError):
        await fmod.write_file("report.txt", "y" * 512)

    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    assert [p.name for p in tmp_path.iterdir()] == ["report.txt"], "temp file leaked"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
async def test_write_file_rewrite_preserves_existing_mode(tmp_path, monkeypatch):
    """Replacing a file keeps its permission bits (old behavior preserved)."""
    fmod = _file_module(tmp_path, monkeypatch)
    target = tmp_path / "script.sh"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)

    await fmod.write_file("script.sh", "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


async def test_write_file_success_and_nested_creation_still_work(tmp_path, monkeypatch):
    fmod = _file_module(tmp_path, monkeypatch)

    result = await fmod.write_file("a/b/new.txt", "hello")
    assert "Successfully wrote" in result
    assert (tmp_path / "a/b/new.txt").read_text(encoding="utf-8") == "hello"

    await fmod.write_file("a/b/new.txt", "world")
    assert (tmp_path / "a/b/new.txt").read_text(encoding="utf-8") == "world"


# ---------------------------------------------------------------------------
# (c) harness_status: persona failure is a typed state
# ---------------------------------------------------------------------------


async def test_persona_summary_separates_absent_from_failure(monkeypatch):
    from machina.core.agent_def import agent_registry
    from machina.tools import harness

    async def missing(_name):
        return None

    monkeypatch.setattr(agent_registry, "get", missing)
    absent = await harness._persona_summary("ghost", {})

    async def broken(_name):
        raise RuntimeError("soulspec store offline")

    monkeypatch.setattr(agent_registry, "get", broken)
    failed = await harness._persona_summary("ghost", {})

    assert absent["available"] is False and failed["available"] is False
    assert absent["reason"] == "no_agent_definition"
    assert failed["reason"] == "persona_resolution_failed"
    assert absent != failed
    assert "soulspec store offline" not in json.dumps(failed)


async def test_harness_status_surfaces_persona_resolution_failure(monkeypatch):
    from machina.core.agent_def import agent_registry
    from machina.core.models import Session
    from machina.tools import harness

    sid = uuid.uuid4()
    owner = "persona-failure-owner"
    monkeypatch.setattr(harness, "active_agent_scope", AsyncMock(return_value=(owner, sid)))
    monkeypatch.setattr(
        harness.session_manager,
        "get",
        AsyncMock(return_value=Session(id=sid, agent_id=owner)),
    )
    monkeypatch.setattr(
        harness.growth_store,
        "get",
        AsyncMock(return_value={"notes": {}, "interactions": 0, "feedback_count": 0}),
    )
    monkeypatch.setattr(
        harness.growth_store, "rsi_status", AsyncMock(return_value={"enabled": False})
    )

    async def broken(_name):
        raise RuntimeError("persona resolution exploded")

    monkeypatch.setattr(agent_registry, "get", broken)
    payload = json.loads(await harness.harness_status())
    persona = payload["effective_persona"]
    assert persona == {"available": False, "reason": "persona_resolution_failed"}


# ---------------------------------------------------------------------------
# (d) additionalProperties: declaration and runtime validator never drift
# ---------------------------------------------------------------------------


def test_every_model_facing_tool_schema_declares_additional_properties():
    import machina.tools  # noqa: F401 — registers every built-in tool
    from machina.tools.base import registry

    definitions = registry.get_tool_definitions()
    assert definitions
    for definition in definitions:
        declared = definition.parameters.get("additionalProperties")
        assert declared in (True, False), (
            f"{definition.name} schema must declare additionalProperties, got {declared!r}"
        )
        tool = registry.get_tool(definition.name)
        assert tool is not None
        args = _required_args(definition.parameters)
        args["__r4_probe__"] = 1
        if declared is True:
            registry._validate_tool_args(tool, args)
        else:
            with pytest.raises(ValidationError, match="unknown parameters"):
                registry._validate_tool_args(tool, args)


def test_additional_properties_true_is_honored_by_the_validator():
    from machina.tools.base import ToolRegistry

    reg = ToolRegistry()

    @reg.register(
        name="flexible",
        parameters={"type": "object", "properties": {}, "additionalProperties": True},
    )
    def flexible(**_kwargs):
        return "ok"

    @reg.register(
        name="strict",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    )
    def strict(x: int):
        return x

    definitions = {d.name: d.parameters for d in reg.get_tool_definitions()}
    assert definitions["flexible"]["additionalProperties"] is True
    assert definitions["strict"]["additionalProperties"] is False

    # The declaration is authoritative in both directions.
    reg._validate_tool_args(reg.get_tool("flexible"), {"anything": 1})
    reg._validate_tool_args(reg.get_tool("strict"), {"x": 1})
    with pytest.raises(ValidationError, match="unknown parameters"):
        reg._validate_tool_args(reg.get_tool("strict"), {"x": 1, "extra": 2})


# ---------------------------------------------------------------------------
# (e) real parameter schemas for the five previously schema-less tools
# ---------------------------------------------------------------------------

_FIVE_TOOLS = {
    "web_search": ({"query", "limit"}, ["query"]),
    "web_extract": ({"url"}, ["url"]),
    "search_files": ({"pattern", "path", "file_glob"}, ["pattern"]),
    "skill_list": ({"query", "offset"}, []),
    "skill_read": ({"skill_name", "offset", "limit", "query"}, ["skill_name"]),
}


def test_five_tools_declare_typed_parameters_with_ranges_and_defaults():
    import machina.tools  # noqa: F401 — registers every built-in tool
    from machina.tools.base import registry

    for name, (properties, required) in _FIVE_TOOLS.items():
        tool = registry.get_tool(name)
        assert tool is not None, name
        schema = tool.parameters
        assert set(schema["properties"]) == properties, name
        assert schema.get("required", []) == required, name
        for prop_name, prop in schema["properties"].items():
            assert prop.get("description"), (name, prop_name)
            assert prop["description"] != prop_name, (
                f"{name}.{prop_name} still has the inferred placeholder description"
            )
        for optional in set(properties) - set(required):
            assert "default" in schema["properties"][optional], (name, optional)

    limit = registry.get_tool("web_search").parameters["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 10, 5)

    offset = registry.get_tool("skill_list").parameters["properties"]["offset"]
    assert (offset["minimum"], offset["default"]) == (0, 0)

    limit = registry.get_tool("skill_read").parameters["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 8000, 800)
    assert "default" in registry.get_tool("skill_read").parameters["properties"]["offset"]

    search = registry.get_tool("search_files").parameters["properties"]
    assert "200" in search["pattern"]["description"]
    assert search["file_glob"]["default"] == "*"
    assert "default" in search["path"]
