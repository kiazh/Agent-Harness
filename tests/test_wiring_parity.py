"""S3 wiring-parity tests: per-step harness metrics + hook-only behavior.

- Harness numbers are pinned: delivery constant (artifact + delegates) and
  spend flat (provider calls / tokens / recalls) across all four wiring
  steps (a)-(d). The replay harness exercises progress.py only, so agent
  hooks cannot move these numbers; this test locks that fact.
- Each hook is behavior-pinned: turn-scope entry/exit, diagnostics-only
  section logging (inputs unmutated), recorded-not-enforced triage (import
  failure never breaks turns), advisory-only complexity (no delegation
  change), and stagnation-only halts (productive loops stay unbounded in
  both run and run_stream).
- Preselect ranking parity is proved (cache == ports == direct) while the
  inline rank call site is explicitly NOT replaced (ownership: workflow.py
  is not S3-owned; complexity fan-out stays advisory).
"""

from __future__ import annotations

import copy
import json
import uuid
from unittest.mock import AsyncMock, patch

from machina.core.models import LLMResponse, Session


def _mock_session():
    return Session(id=uuid.uuid4(), title="Test", context_budget=8000)


def _run_mocks(mock_sm, mock_cm, session):
    mock_sm.get = AsyncMock(return_value=session)
    mock_sm.update_activity = AsyncMock()
    mock_cm.add_chunk = AsyncMock()
    mock_cm.add_chunks_batch = AsyncMock()
    mock_cm.get_latest_images = AsyncMock(return_value=[])
    mock_cm.get_recent_context = AsyncMock(return_value=[])


# --- harness metrics recorder (steps a-d share these numbers) ----------------


def test_harness_metrics_pinned_across_wiring_steps(tmp_path):
    """Delivery constant + spend flat: the four S3.1 step numbers."""
    from tests.test_replay_harness import replay_incident

    m = replay_incident(tmp_path)
    assert m["provider_calls"] == 1
    assert m["tokens_in"] == 22
    assert m["tokens_out"] == 256
    assert m["recall_calls"] == 3  # == STAGNATION_ROUNDS_K
    assert m["delegate_launches"] == 7
    assert m["artifact_written"] is True
    assert m["live_calls"] == 0


# --- (a) turn scope ------------------------------------------------------------


async def test_turn_scope_entered_during_run_and_reset_after():
    from machina.controller import wiring as _  # noqa: F401 (ensures importable)
    from machina.core.agent import ReActAgent
    from machina.core.session_mode import _turn_scope
    from machina.tools.base import ToolRegistry

    seen: dict = {}
    reg = ToolRegistry()

    @reg.register(description="scope probe")
    async def probe_tool() -> str:
        seen["in_scope"] = _turn_scope.get() is not None
        return "ok"

    provider = AsyncMock()
    provider.complete = AsyncMock(
        side_effect=[
            LLMResponse(
                content="",
                model="t",
                usage={"total_tokens": 5},
                tool_calls=[{"id": "c1", "function": {"name": "probe_tool", "arguments": "{}"}}],
            ),
            LLMResponse(content="done", model="t", usage={"total_tokens": 5}),
        ]
    )
    session = _mock_session()
    with (
        patch("machina.core.agent.session_manager") as mock_sm,
        patch("machina.core.agent.context_manager") as mock_cm,
        patch("machina.core.agent.registry", reg),
    ):
        _run_mocks(mock_sm, mock_cm, session)
        response = await ReActAgent(provider=provider).run(session.id, "hi", verbose=False)
    assert response.content == "done"
    assert seen.get("in_scope") is True
    assert _turn_scope.get() is None


# --- (b) section-token log -----------------------------------------------------


def test_section_log_never_mutates_inputs():
    from machina.controller import wiring

    messages = [{"role": "user", "content": "hello"}]
    tools = [{"name": "read_file", "args": {}}]
    snapshot = (copy.deepcopy(messages), copy.deepcopy(tools))
    assert wiring.log_section_tokens(messages, tools) is None
    assert (messages, tools) == snapshot


# --- (c) triage recorded, never enforced ---------------------------------------


async def test_triage_recorded_not_enforced():
    from machina.controller.triage import resolve_task
    from machina.core.agent import ReActAgent

    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(content="ok", model="t", usage={"total_tokens": 5})
    )
    session = _mock_session()
    query = "Fix this traceback in one file"
    with (
        patch("machina.core.agent.session_manager") as mock_sm,
        patch("machina.core.agent.context_manager") as mock_cm,
    ):
        _run_mocks(mock_sm, mock_cm, session)
        agent = ReActAgent(provider=provider)
        response = await agent.run(session.id, query, verbose=False)
    assert response.content == "ok"  # classification changed nothing
    assert agent._last_triage == resolve_task(query) == "debug"


async def test_controller_import_failure_never_breaks_turn(monkeypatch):
    import sys

    from machina.core.agent import ReActAgent

    monkeypatch.setitem(sys.modules, "machina.controller.wiring", None)
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(content="still works", model="t", usage={"total_tokens": 5})
    )
    session = _mock_session()
    with (
        patch("machina.core.agent.session_manager") as mock_sm,
        patch("machina.core.agent.context_manager") as mock_cm,
    ):
        _run_mocks(mock_sm, mock_cm, session)
        response = await ReActAgent(provider=provider).run(session.id, "hi", verbose=False)
    assert response.content == "still works"


# --- S3.2 complexity advisory only ----------------------------------------------


async def test_complexity_advisory_changes_no_delegation():
    from machina.controller.complexity import score_complexity
    from machina.core.agent import ReActAgent

    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(content="direct answer", model="t", usage={"total_tokens": 5})
    )
    session = _mock_session()
    query = "Review all files across the entire repo, dont implement changes just review"
    with (
        patch("machina.core.agent.session_manager") as mock_sm,
        patch("machina.core.agent.context_manager") as mock_cm,
    ):
        _run_mocks(mock_sm, mock_cm, session)
        agent = ReActAgent(provider=provider)
        response = await agent.run(session.id, query, verbose=False)
    assert response.content == "direct answer"
    assert response.tool_calls == []  # advisory: no fan-out was launched
    assert agent._last_complexity == score_complexity(query)
    assert agent._last_complexity["level"] == "subagents"  # recommended, not executed


def test_preselect_parity_holds_but_inline_call_not_replaced(tmp_path):
    """Ranking parity proved; replacement deferred (workflow.py not S3-owned)."""
    import pathlib

    from machina.controller.preselect import SkillPreselectCache
    from machina.skills import ports
    from machina.skills.registry import SkillRegistry
    from machina.skills.runtime import rank_skills as direct_rank

    reg = SkillRegistry(tmp_path)
    reg.create_skill(
        "pair-python-testing", "Python testing with pytest", "Run pytest.", triggers=["pytest"]
    )
    reg.create_skill(
        "pair-deploy", "Deploy services with docker", "Build images.", triggers=["deploy"]
    )
    cache = SkillPreselectCache(reg)
    for query in ("how do I run python testing with pytest?", "deploy the docker service"):
        assert [s.name for s in cache.rank(query)] == [
            s.name for s in ports.rank_skills(reg, query)
        ]
        assert [s.name for s in cache.rank(query)] == [s.name for s in direct_rank(reg, query)]
    source = pathlib.Path("machina/skills/workflow.py").read_text()
    assert "from machina.skills.runtime import" in source  # inline site untouched


# --- (d) stagnation mapping + halts ----------------------------------------------


def test_stagnation_mapping_rule():
    from machina.controller import wiring

    # Retrieval repeats: stable target, zero bytes (stagnation-eligible).
    assert wiring.target_bytes("session_recall", {"query": "q"}, "echo", "completed") == ("q", 0)
    # Execution/IO/unknown shapes: conservative progress, never a forced stop.
    assert wiring.target_bytes("read_file", {"path": "/tmp/x"}, "Error: missing", "failed")[1] == 1
    assert wiring.target_bytes("some_future_tool", {"a": 1}, "", "completed")[1] == 1
    assert wiring.target_bytes("write_file", {"path": "f"}, "ok", "completed")[1] == 1


def test_stagnation_targets_distinct_per_tool_arguments():
    """Wave-2 audit regression: distinct URLs/patterns must be distinct targets.

    The generic key list lacked "url" and let "path" shadow "pattern", so
    distinct-URL web_extract and distinct-pattern search_files loops read as
    stagnant and stopped at K — killing legitimate multi-page research.
    """
    from machina.controller import wiring

    a = wiring.target_bytes("web_extract", {"url": "https://a.example"}, "", "completed")
    b = wiring.target_bytes("web_extract", {"url": "https://b.example"}, "", "completed")
    assert a != b and a[1] == 0 and b[1] == 0
    # Identical repeat still collapses (stagnation remains detectable).
    assert wiring.target_bytes("web_extract", {"url": "https://a.example"}, "", "completed") == a

    p1 = wiring.target_bytes("search_files", {"pattern": "alpha", "path": "."}, "", "completed")
    p2 = wiring.target_bytes("search_files", {"pattern": "beta", "path": "."}, "", "completed")
    assert p1 != p2
    # Same pattern in a different subtree is new coverage too.
    p3 = wiring.target_bytes("search_files", {"pattern": "alpha", "path": "src"}, "", "completed")
    assert p3 != p1


def test_distinct_url_and_pattern_loops_never_stagnate():
    """Full mapping fold: 7 rounds of new URLs/patterns must never stop."""
    from machina.controller import wiring

    hist: list = []
    for i in range(6):
        hist, stop = wiring.check_stagnation(
            hist,
            [{"tool": "web_extract", "args": {"url": f"https://page-{i}.example"},
              "result_preview": "", "outcome": "completed"}],
        )
        assert stop is None, f"stop fired at round {i}: {stop}"
    hist, stop = wiring.check_stagnation(
        hist,
        [{"tool": "web_extract", "args": {"url": "https://page-6.example"},
          "result_preview": "", "outcome": "completed"}],
    )
    assert stop is None

    hist2: list = []
    for i in range(6):
        hist2, stop = wiring.check_stagnation(
            hist2,
            [{"tool": "search_files", "args": {"pattern": f"needle{i}", "path": "."},
              "result_preview": "", "outcome": "completed"}],
        )
        assert stop is None, f"search_files stop fired at round {i}: {stop}"


def test_identical_url_loop_still_stagnates_at_k():
    """The fix must not weaken stagnation: identical repeats halt at exactly K."""
    from machina.controller import wiring

    hist: list = []
    stop = None
    for _ in range(8):
        hist, stop = wiring.check_stagnation(
            hist,
            [{"tool": "web_extract", "args": {"url": "https://same.example"},
              "result_preview": "", "outcome": "completed"}],
        )
        if stop:
            break
    assert stop is not None


def _recall_responseAR(query):
    return LLMResponse(
        content="",
        model="t",
        usage={"total_tokens": 5},
        tool_calls=[
            {
                "id": "c1",
                "function": {"name": "session_recall", "arguments": json.dumps({"query": query})},
            }
        ],
    )


async def test_stagnation_halts_identical_recall_loop_at_k():
    from machina.core.agent import ReActAgent
    from machina.core.progress import STAGNATION_ROUNDS_K
    from machina.tools.base import ToolRegistry

    reg = ToolRegistry()

    @reg.register(description="stub recall")
    async def session_recall(query: str = "") -> str:
        return "echo: agent catalog / earlier recall output (no new evidence)"

    provider = AsyncMock()
    provider.complete = AsyncMock(
        side_effect=[_recall_responseAR("just write the md delegation doc")] * 6
        + [LLMResponse(content="unreached", model="t", usage={"total_tokens": 5})]
    )
    session = _mock_session()
    with (
        patch("machina.core.agent.session_manager") as mock_sm,
        patch("machina.core.agent.context_manager") as mock_cm,
        patch("machina.core.agent.registry", reg),
    ):
        _run_mocks(mock_sm, mock_cm, session)
        response = await ReActAgent(provider=provider).run(session.id, "write it", verbose=False)
    assert response.outcome == "failed"
    assert "stagnant" in response.content.lower()
    assert str(STAGNATION_ROUNDS_K) in response.content
    assert "max iterations" not in response.content.lower()  # stagnation, never a cap
    # First identical round is new coverage; the halt lands at K+1 batches.
    assert response.iterations == STAGNATION_ROUNDS_K + 1
    assert provider.complete.await_count == STAGNATION_ROUNDS_K + 1


async def test_productive_recall_history_never_halts():
    from machina.core.agent import ReActAgent
    from machina.tools.base import ToolRegistry

    reg = ToolRegistry()

    @reg.register(description="stub recall")
    async def session_recall(query: str = "") -> str:
        return f"fresh evidence for {query}"

    provider = AsyncMock()
    provider.complete = AsyncMock(
        side_effect=[_recall_responseAR(f"distinct query {i}") for i in range(4)]
        + [LLMResponse(content="finished", model="t", usage={"total_tokens": 5})]
    )
    session = _mock_session()
    with (
        patch("machina.core.agent.session_manager") as mock_sm,
        patch("machina.core.agent.context_manager") as mock_cm,
        patch("machina.core.agent.registry", reg),
    ):
        _run_mocks(mock_sm, mock_cm, session)
        response = await ReActAgent(provider=provider).run(session.id, "go", verbose=False)
    assert response.content == "finished"
    assert response.iterations == 5


async def test_stagnation_halts_stream_loop_at_k():
    from machina.core.agent import ReActAgent
    from machina.core.models import StreamEvent
    from machina.core.progress import STAGNATION_ROUNDS_K
    from machina.tools.base import ToolRegistry

    reg = ToolRegistry()

    @reg.register(description="stub recall")
    async def session_recall(query: str = "") -> str:
        return "echo: no new evidence"

    provider = AsyncMock()

    async def _stream(*args, **kwargs):
        for _ in range(6):
            yield StreamEvent(
                type="done",
                response=_recall_responseAR("same query every time"),
            )

    provider.stream_complete = _stream
    session = _mock_session()
    with (
        patch("machina.core.agent.session_manager") as mock_sm,
        patch("machina.core.agent.context_manager") as mock_cm,
        patch("machina.core.agent.registry", reg),
    ):
        _run_mocks(mock_sm, mock_cm, session)
        events = [
            e
            async for e in ReActAgent(provider=provider).run_stream(session.id, "go", verbose=False)
        ]
    done = [e for e in events if e.type == "done"]
    assert done and done[-1].response is not None
    assert "stagnant" in done[-1].response.content.lower()
    assert done[-1].response.iterations == STAGNATION_ROUNDS_K + 1
