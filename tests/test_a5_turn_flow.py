"""A5-min: shared batch-tail helpers (``machina.core.turn_flow``).

``BaseReActAgent.run`` and ``.run_stream`` duplicated their post-batch tail
(pause collection, the S3.1(d) stagnation hook, the failed-stop response
shape); FM8 fixed the state-write honesty on both copies and flagged the
duplication as a divergence hazard. These tests pin:

- unit semantics for each helper: pauses found from mixed batches, the
  stagnation step returning the truthful text only once K rounds are
  stagnant, and the failed-stop response shape;
- behavior parity: for a scripted stagnation scenario, ``run()`` and
  ``run_stream()`` produce IDENTICAL stop payloads and session-state writes
  (mirrors the FM8 symmetry methodology);
- the source-level guarantee that the duplicated blocks are gone from
  ``agent.py`` — the wiring calls live in exactly one place, the helper.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from machina.core.models import LLMResponse, Session, StreamEvent
from machina.core.progress import STAGNATION_ROUNDS_K
from machina.core.turn_flow import collect_pauses, stagnation_step, stopped_response

ROOT = Path(__file__).resolve().parents[1]


# ─── fixtures: mocked DB/context, no providers ───────────────────────────────


def _mock_session():
    return Session(id=uuid.uuid4(), title="Test", context_budget=8000)


def _run_mocks(mock_sm, mock_cm, session):
    mock_sm.get = AsyncMock(return_value=session)
    mock_sm.update_activity = AsyncMock()
    mock_sm.update_state_fields = AsyncMock(return_value={})
    mock_cm.add_chunk = AsyncMock()
    mock_cm.add_chunks_batch = AsyncMock()
    mock_cm.get_latest_images = AsyncMock(return_value=[])
    mock_cm.get_recent_context = AsyncMock(return_value=[])


def _recall_response(query: str = "same query") -> LLMResponse:
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


def _recall_batch(query: str = "same query") -> list[dict]:
    """One stagnation-eligible tool round (stable target, zero artifact bytes)."""
    return [
        {
            "tool": "session_recall",
            "args": {"query": query},
            "result_preview": "echo: no new evidence",
            "outcome": "completed",
        }
    ]


# ─── unit: collect_pauses ────────────────────────────────────────────────────


def test_collect_pauses_finds_only_typed_request_ids():
    p1 = {"request_id": "r1", "operation": "process.exec"}
    p2 = {"request_id": "r2", "operation": "file.write"}
    batch = [
        # needs_approvals wins when truthy; non-dict / id-less pauses drop.
        {"tool": "terminal", "needs_approvals": [p1, {"operation": "no-id"}]},
        # single needs_approval shape, when no plural field is present.
        {"tool": "write_file", "needs_approval": p2},
        # empty plural falls back to the single field (exact prior semantics).
        {"tool": "edit_file", "needs_approvals": [], "needs_approval": p2},
        # plain completed entry: no pause.
        {"tool": "read_file", "outcome": "completed"},
        # non-dict pauses never count.
        {"tool": "x", "needs_approval": "not-a-dict"},
        {"tool": "y", "needs_approvals": None, "needs_approval": None},
    ]
    assert collect_pauses(batch) == [p1, p2, p2]


def test_collect_pauses_empty_batch():
    assert collect_pauses([]) == []


# ─── unit: stagnation_step ───────────────────────────────────────────────────


async def test_stagnation_step_returns_none_before_k_then_truthful_text():
    history: list = []
    tool_calls_made: list = []
    text = None
    for _ in range(STAGNATION_ROUNDS_K + 2):
        tool_calls_made.extend(_recall_batch())
        history, text = await stagnation_step(history, _recall_batch(), tool_calls_made)
        if text is not None:
            break
    assert text is not None, "identical rounds must halt at K stagnant rounds"
    assert len(history) == STAGNATION_ROUNDS_K + 1  # first identical round is new coverage
    assert "no new file coverage" in text
    assert "recorded as session state" in text
    assert "no further model call" in text
    assert "max iterations" not in text.lower()
    # The next batch after a non-stop fold keeps folding (no hidden state).
    history, text = await stagnation_step(history, [], tool_calls_made)
    assert text is not None  # trailing K are still stagnant
    assert len(history) == STAGNATION_ROUNDS_K + 1  # empty batch adds no round


async def test_productive_batches_never_stop():
    history: list = []
    tool_calls_made: list = []
    for i in range(6):
        batch = _recall_batch(f"distinct query {i}")
        tool_calls_made.extend(batch)
        history, text = await stagnation_step(history, batch, tool_calls_made)
        assert text is None, f"stop fired on new coverage at round {i}"


async def test_stagnation_step_survives_missing_wiring(monkeypatch):
    import machina.controller as controller_pkg

    # `from machina.controller import wiring` resolves via the package
    # attribute once imported; break both paths so the deferred import fails.
    monkeypatch.delattr(controller_pkg, "wiring", raising=False)
    monkeypatch.setitem(sys.modules, "machina.controller.wiring", None)
    history = [{"tool": "session_recall", "target": "same", "new_bytes": 0}]
    out_history, text = await stagnation_step(history, _recall_batch(), [])
    assert out_history == history
    assert text is None


async def test_stagnation_step_logs_audits_flushes_then_writes_state():
    """Original order preserved: warning → audit → caller flush → state write."""
    order: list[str] = []
    session_manager = AsyncMock()

    async def _record_state(session_id, fields):
        order.append("write")
        assert session_id == "sess-a5"
        assert fields["stagnation_stop"]["stop_text"].startswith("stagnant")

    session_manager.update_state_fields = AsyncMock(side_effect=_record_state)

    async def _flush():
        order.append("flush")

    audit_calls: list = []
    history: list = []
    tool_calls_made: list = []
    text = None
    for _ in range(STAGNATION_ROUNDS_K + 1):
        tool_calls_made.extend(_recall_batch())
        history, text = await stagnation_step(
            history,
            _recall_batch(),
            tool_calls_made,
            session_id="sess-a5",
            session_manager=session_manager,
            audit=lambda *args, **kwargs: audit_calls.append((args, kwargs)),
            flush=_flush,
        )
        if text is not None:
            break

    assert text is not None
    session_manager.update_state_fields.assert_awaited_once()
    args, kwargs = audit_calls[0]
    assert args == ("agent_stagnation_stop",)
    assert kwargs["session_id"] == "sess-a5"
    assert kwargs["reason"].lower().startswith("stagnant")
    assert order == ["flush", "write"]


# ─── unit: stopped_response ──────────────────────────────────────────────────


def test_stopped_response_builds_the_failed_stop_shape():
    tool_calls = [{"tool": "session_recall", "outcome": "completed"}]
    response = stopped_response("truthful stop text", tool_calls, 42, 7)
    assert response.content == "truthful stop text"
    assert response.outcome == "failed"
    assert response.tool_calls is tool_calls
    assert response.tokens_used == 42
    assert response.iterations == 7
    assert response.needs_approval == []


# ─── parity: run() vs run_stream() stop payloads + state ─────────────────────


async def _scripted_stagnation(*, stream: bool):
    from machina.core.agent import ReActAgent
    from machina.tools.base import ToolRegistry

    reg = ToolRegistry()

    @reg.register(description="stub recall")
    async def session_recall(query: str = "") -> str:
        return "echo: no new evidence"

    rounds = STAGNATION_ROUNDS_K + 3  # provider offers more; the loop must stop first
    session = _mock_session()
    seen = {"provider_calls": 0}
    with (
        patch("machina.core.agent.session_manager") as mock_sm,
        patch("machina.core.agent.context_manager") as mock_cm,
        patch("machina.core.agent.registry", reg),
    ):
        _run_mocks(mock_sm, mock_cm, session)
        provider = AsyncMock()
        if stream:
            responses = [_recall_response()] * rounds

            async def _counting_stream(*args, **kwargs):
                seen["provider_calls"] += 1
                index = min(seen["provider_calls"] - 1, len(responses) - 1)
                yield StreamEvent(type="done", response=responses[index])

            provider.stream_complete = _counting_stream
            events = [
                e
                async for e in ReActAgent(provider=provider).run_stream(
                    session.id, "go", verbose=False
                )
            ]
            done = [e for e in events if e.type == "done"]
            assert done and done[-1].response is not None
            response = done[-1].response
        else:
            provider.complete = AsyncMock(side_effect=[_recall_response()] * rounds)
            response = await ReActAgent(provider=provider).run(session.id, "go", verbose=False)
            seen["provider_calls"] = provider.complete.await_count

        state_calls = mock_sm.update_state_fields.await_count
        state = mock_sm.update_state_fields.await_args.args[1]
    return response, state, seen["provider_calls"], state_calls


async def test_run_and_run_stream_produce_identical_stop_payloads_and_state():
    run_resp, run_state, run_calls, run_state_calls = await _scripted_stagnation(stream=False)
    stream_resp, stream_state, stream_calls, stream_state_calls = await _scripted_stagnation(
        stream=True
    )

    # Identical spend: K+1 batches (first identical round is new coverage),
    # then exactly one truthful state write per loop — never another LLM call.
    assert run_calls == stream_calls == STAGNATION_ROUNDS_K + 1
    assert run_state_calls == stream_state_calls == 1

    # Identical typed payloads (dataclass equality covers every field).
    assert run_resp == stream_resp
    assert run_resp.outcome == "failed"
    assert run_resp.iterations == STAGNATION_ROUNDS_K + 1
    assert "recorded as session state" in run_resp.content
    assert "max iterations" not in run_resp.content.lower()

    # Identical durable state writes (the FM8-honest payload shape).
    assert run_state == stream_state
    fields = run_state["stagnation_stop"]
    assert fields["tool_calls"] == STAGNATION_ROUNDS_K + 1
    assert fields["evidence"]["session_recall"] == STAGNATION_ROUNDS_K + 1
    assert fields["stop_text"] == run_resp.content
    assert "stagnant" in fields["reason"].lower()


# ─── source-level: the duplicated blocks are gone ────────────────────────────


def test_stagnation_implementation_lives_once_in_turn_flow():
    agent_src = (ROOT / "machina" / "core" / "agent.py").read_text(encoding="utf-8")
    flow_src = (ROOT / "machina" / "core" / "turn_flow.py").read_text(encoding="utf-8")

    # Exactly one call site each — the helper's. agent.py has none.
    assert agent_src.count("check_stagnation(") == 0
    assert flow_src.count("check_stagnation(") == 1
    assert agent_src.count("stagnation_content(") == 0
    assert flow_src.count("stagnation_content(") == 1
    assert agent_src.count("record_stagnation_state(") == 0
    assert flow_src.count("record_stagnation_state(") == 1
    assert "_s3wh" not in agent_src

    # The duplicated pause comprehension is gone from agent.py.
    assert 'for pause in (entry.get("needs_approvals")' not in agent_src
    assert len(re.findall(r'entry\.get\("needs_approvals"\)', flow_src)) == 1


def test_both_loops_call_the_shared_batch_tail_helpers():
    import inspect

    import machina.core.agent as agent_mod

    for loop in (agent_mod.ReActAgent.run, agent_mod.ReActAgent.run_stream):
        src = inspect.getsource(loop)
        assert "collect_pauses(" in src
        assert "stagnation_step(" in src
        assert "stopped_response(" in src


def test_agent_py_shrank_below_its_pre_extraction_size():
    """A5-min must pay for itself: the helpers live OUTSIDE agent.py."""
    agent_lines = len(
        (ROOT / "machina" / "core" / "agent.py").read_text(encoding="utf-8").splitlines()
    )
    assert agent_lines < 2056, (
        f"agent.py must shrink below its pre-extraction 2056 lines, got {agent_lines}"
    )
