"""Stream D god-file extraction parity tests (moves-only proof).

Proves the ``agent.py`` → ``prompt.py`` and ``compression.py`` →
``checkpoint.py`` extractions changed homes without changing behavior:

- Re-export identity: names imported from the old home ARE the same objects
  as the new home (``is`` checks, not just equality).
- Parity: moved functions produce byte-identical outputs to the contract the
  old inline code implemented (round-trip through both import paths).
- Loop bounds untouched: the ReAct loops remain ``while True`` with no caps.
- Section builders compose: per-section ``(role, content)`` pieces join into
  the same system prompt text the inline version built.
"""

from __future__ import annotations

import inspect

import machina.core.agent as agent_mod
import machina.core.checkpoint as checkpoint_mod
import machina.core.compression as compression_mod
import machina.core.prompt as prompt_mod
from machina.core.agent import BaseReActAgent


class TestPromptReexportIdentity:
    def test_stale_preview_is_same_object(self):
        assert agent_mod._stale_tool_preview is prompt_mod.stale_tool_preview

    def test_capacity_helpers_are_same_objects(self):
        assert agent_mod._turn_model is prompt_mod.turn_model
        assert agent_mod._effective_turn_budget is prompt_mod.effective_turn_budget
        assert agent_mod._request_context_tokens is prompt_mod.request_context_tokens

    def test_section_builders_reexported(self):
        for name in (
            "build_persona_section",
            "build_capability_section",
            "build_skill_section",
            "build_system_prompt",
            "assemble_turn_prompt",
            "build_turn_messages",
            "append_tool_messages",
            "condense_history",
            "prompt_section_tokens",
            "tool_message_limit",
        ):
            assert getattr(agent_mod, name) is getattr(prompt_mod, name), name


class TestCheckpointReexportIdentity:
    def test_checkpoint_names_are_same_objects(self):
        assert compression_mod.CompactionCheckpoint is checkpoint_mod.CompactionCheckpoint
        assert compression_mod.validate_checkpoint is checkpoint_mod.validate_checkpoint
        assert compression_mod.build_checkpoint is checkpoint_mod.build_checkpoint
        assert compression_mod.extract_checkpoint is checkpoint_mod.extract_checkpoint
        assert compression_mod.effective_instruction is checkpoint_mod.effective_instruction
        assert compression_mod.CHECKPOINT_VERSION == checkpoint_mod.CHECKPOINT_VERSION == 1

    def test_checkpoint_importable_from_both_homes(self):
        from machina.core.checkpoint import build_checkpoint as new_build
        from machina.core.compression import build_checkpoint as old_build

        assert new_build is old_build


class TestMovedBehaviorParity:
    def test_stale_preview_format(self):
        content = "x" * 2000
        out = prompt_mod.stale_tool_preview(content)
        assert out.startswith("x" * 350)
        assert out.endswith("x" * 350)
        assert "2000" in out
        assert agent_mod._stale_tool_preview(content) == out

    def test_condense_parity_static_vs_prompt(self):
        # Two rounds: the earliest round condenses, the latest stays full.
        tc_old = {"id": "c0", "function": {"name": "t", "arguments": "{}"}}
        tc_new = {"id": "c1", "function": {"name": "t", "arguments": "{}"}}
        old_response = type("R", (), {"content": "first", "tool_calls": [tc_old]})()
        new_response = type("R", (), {"content": "second", "tool_calls": [tc_new]})()
        for append in (prompt_mod.append_tool_messages, BaseReActAgent._append_tool_messages):
            messages: list = []
            if append is prompt_mod.append_tool_messages:
                append(messages, old_response, tc_old, "y" * 2000)
                append(messages, new_response, tc_new, "z" * 2000)
            else:
                append(None, messages, old_response, tc_old, "y" * 2000)
                append(None, messages, new_response, tc_new, "z" * 2000)
            BaseReActAgent._condense_history(messages)
            tool_msgs = [m for m in messages if m.get("role") == "tool"]
            assert len(tool_msgs) == 2
            assert "earlier output condensed" in tool_msgs[0]["content"]
            assert "earlier output condensed" not in tool_msgs[1]["content"]

    def test_append_tool_messages_parity(self):
        tc1 = {"id": "a", "function": {"name": "t", "arguments": "{}"}}
        tc2 = {"id": "b", "function": {"name": "t", "arguments": "{}"}}
        response = type("R", (), {"content": "think", "tool_calls": [tc1, tc2]})()
        via_agent: list = []
        agent_mod.BaseReActAgent._append_tool_messages(None, via_agent, response, tc1, "r1")
        agent_mod.BaseReActAgent._append_tool_messages(None, via_agent, response, tc2, "r2")
        via_prompt: list = []
        prompt_mod.append_tool_messages(via_prompt, response, tc1, "r1")
        prompt_mod.append_tool_messages(via_prompt, response, tc2, "r2")
        assert via_agent == via_prompt
        assert [m["role"] for m in via_agent] == ["assistant", "tool", "tool"]

    def test_tool_message_limit_parity(self):
        assert prompt_mod.tool_message_limit("skill_read") == 9000
        assert prompt_mod.tool_message_limit("search_documents") == 9000
        assert prompt_mod.tool_message_limit("terminal") == 1000
        assert prompt_mod.tool_message_limit(None) == 1000

    def test_section_builders_return_role_content(self):
        import uuid

        from machina.core.models import Session
        from machina.soulspec.schema import SoulSpec

        soul_spec = SoulSpec(
            name="t",
            persona=SoulSpec.Persona(name="t", system_prompt="CORE IDENTITY"),
        )
        session = Session(id=uuid.uuid4())
        session.context_budget = 8000

        role, text = prompt_mod.build_persona_section(soul_spec, None, None, "skipped", {})
        assert role == "system" and "CORE IDENTITY" in text
        role, suffix = prompt_mod.build_capability_section(session, set(), "skipped")
        assert role == "system" and isinstance(suffix, str) and suffix.startswith("\n\n")
        role, skill = prompt_mod.build_skill_section(
            "hello", session, [], None, {}, [], system_base="base text"
        )
        assert role == "system" and isinstance(skill, str)
        # Composer joins the three pieces in order with no drops/dupes.
        composed = prompt_mod.build_system_prompt(
            soul_spec, None, session, "hello", [], None, "skipped", {}, [], set()
        )
        assert composed == text + suffix + skill

    def test_checkpoint_round_trip_identical(self):
        cp = checkpoint_mod.build_checkpoint(
            objective="o",
            expected_artifact="a.md",
            constraints=["c"],
            read_only_scope="scope",
            findings=[{"finding": "f", "count": 2}],
            unresolved_questions=["q"],
            next_action="n",
        )
        assert compression_mod.extract_checkpoint({"checkpoint": cp}) == cp
        assert checkpoint_mod.effective_instruction(cp, "latest wins") == "latest wins"
        assert checkpoint_mod.effective_instruction(cp, None) == "n"
        assert checkpoint_mod.effective_instruction(None, None) is None

    def test_wrap_messages_shape(self):
        messages = prompt_mod.build_turn_messages("hello prompt", [])
        assert messages == [{"role": "user", "content": "hello prompt"}]

    def test_section_token_diagnostics(self):
        sections = {"system": "hello world", "query": "hi"}
        counts = prompt_mod.prompt_section_tokens(sections)
        assert set(counts) == set(sections)
        assert all(v > 0 for v in counts.values())
        from machina.core.execution_context import summarize_section_tokens

        summary = summarize_section_tokens(counts)
        assert summary.startswith("prompt_sections ")


class TestLoopBoundsUntouched:
    """The agent loop stays UNBOUNDED — no caps introduced by the extraction."""

    def test_loops_are_while_true(self):
        run_src = inspect.getsource(agent_mod.ReActAgent.run)
        stream_src = inspect.getsource(agent_mod.ReActAgent.run_stream)
        assert "while True:" in run_src
        assert "while True:" in stream_src

    def test_no_max_iterations_gate_in_loops(self):
        run_src = inspect.getsource(agent_mod.ReActAgent.run)
        stream_src = inspect.getsource(agent_mod.ReActAgent.run_stream)
        assert "max_iterations" not in run_src
        assert "max_iterations" not in stream_src

    def test_shared_executor_seam_intact(self):
        base_src = inspect.getsource(BaseReActAgent)
        assert "_execute_tool_calls" in base_src
        assert "_execute_tool_calls_stream" in base_src
        assert "_prepare_context" in base_src
        helper_src = inspect.getsource(BaseReActAgent._execute_tool_calls_stream)
        assert "registry.execute(" in helper_src
