"""H04 (holistic review 2026-10-15): compaction summaries must reach prompts.

RED before fix: the compressor timestamps its summary *before* the preserved
recent chunks, so ``get_recent_context`` returns it at/after index 3 and the
assembler's newest-three recent window never selects it. When a summary does
reach the prompt (recent or retrieved), ``_compress_chunk`` has no
``compression_summary`` case and renders only the first 100 characters of the
payload dict repr.

Fixed contract: the latest validated compaction summary/checkpoint is
reserved as its own prompt section and its content is rendered explicitly
within the assembler's authoritative token budget. Archive keyword recall is
not required for a continuation to see the compacted context.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from machina.core.assembler import PromptAssembler, get_token_count
from machina.core.checkpoint import build_checkpoint
from machina.core.compression import CompressionConfig, ContextCompressor
from machina.core.models import ContextChunk, LLMResponse

SESSION_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _chunks() -> list[ContextChunk]:
    """Newest-first chunk list: 3 preserved recent + 3 older to compress."""
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        ("user_message", "recent turn zero", 0),
        ("assistant_message", "recent turn one", 1),
        ("user_message", "recent turn two", 2),
        ("user_message", "old turn three", 3),
        ("assistant_message", "old turn four", 4),
        ("user_message", "old turn five", 5),
    ]
    return [
        ContextChunk(
            id=uuid.uuid4(),
            session_id=SESSION_ID,
            agent_id="harness",
            chunk_type=chunk_type,
            payload={"content": text},
            token_count=10,
            created_at=now - timedelta(minutes=offset),
        )
        for chunk_type, text, offset in rows
    ]


def _recent_dicts(chunks: list[ContextChunk]) -> list[dict]:
    """Mimic ``get_recent_context``: newest-first dict rows for the assembler."""
    ordered = sorted(chunks, key=lambda c: c.created_at, reverse=True)
    return [{"type": c.chunk_type, "payload": c.payload, "tokens": c.token_count} for c in ordered]


def _checkpoint() -> dict:
    return build_checkpoint(
        objective="Finish the delegated refactor",
        expected_artifact="/tmp/report.md",
        constraints=["do not edit outside owned files"],
        read_only_scope="/tmp read-only except owned files",
        findings=[{"finding": "summary never reached the prompt", "count": 3}],
        unresolved_questions=["does archive recall suffice?"],
        next_action="Reserve a summary section in the assembler",
    )


def test_compaction_summary_survives_the_recent_window():
    """RED: preserving 3 recent chunks pushes the summary out of the window."""
    import asyncio

    provider = object()
    response = LLMResponse(
        content="COMPACTED-SUMMARY-MARKER: earlier turns decided X, Y, Z.",
        model="test-model",
        usage={"total_tokens": 10},
    )

    async def _fake_complete(*args, **kwargs):
        return response

    import unittest.mock as mock

    from machina.core.usage import usage_store

    with mock.patch.object(usage_store, "complete_call", _fake_complete):
        result = asyncio.run(
            ContextCompressor(config=CompressionConfig()).acompress(
                _chunks(), SESSION_ID, "harness", llm_provider=provider
            )
        )

    summaries = [c for c in result.compressed_chunks if c.chunk_type == "compression_summary"]
    assert len(summaries) == 1
    # The compressor stamps the summary with the newest OLD timestamp, so it
    # sorts *behind* every preserved recent chunk (the deliberate ordering).
    recent = _recent_dicts(result.compressed_chunks)
    assert [row["type"] for row in recent[:3]] == [
        "user_message",
        "assistant_message",
        "user_message",
    ], "summary must land outside the newest-three window for this reproduction"

    prompt = PromptAssembler(session_budget=8000).assemble(
        system_prompt="You are a test agent.",
        goal=None,
        recent_chunks=recent,
        retrieved_chunks=[],
        query="continue the task",
    )
    assert "## Compacted Context" in prompt
    assert "COMPACTED-SUMMARY-MARKER" in prompt
    assert "### Recent Activity" not in prompt or prompt.index(
        "## Compacted Context"
    ) < prompt.index("## Recent Activity")


def test_checkpoint_and_full_content_render_explicitly():
    """RED: the summary/checkpoint must render in full, not as a 100-char dict."""
    cp = _checkpoint()
    content = "P" * 220 + ":SUMMARY-TAIL-OMEGA"
    recent = [
        {
            "type": "compression_summary",
            "payload": {
                "content": content,
                "original_count": 4,
                "checkpoint": cp,
                "checkpoint_version": 1,
            },
            "tokens": 80,
        },
    ] + [
        {"type": "user_message", "payload": {"content": f"recent {i}"}, "tokens": 4}
        for i in range(3)
    ]

    prompt = PromptAssembler(session_budget=8000).assemble(
        system_prompt="system",
        goal=None,
        recent_chunks=recent,
        retrieved_chunks=[],
        query="continue",
    )
    assert "## Compacted Context" in prompt
    assert "SUMMARY-TAIL-OMEGA" in prompt, "free-form summary text must survive in full"
    assert cp["objective"] in prompt
    assert cp["next_action"] in prompt
    assert cp["constraints"][0] in prompt
    assert "finding (3)" in prompt


def test_compress_chunk_renders_summary_content_not_payload_repr():
    assembler = PromptAssembler()
    content = "C" * 150 + ":DIRECT-MARKER"
    rendered = assembler._compress_chunk(
        {"type": "compression_summary", "payload": {"content": content}}
    )
    assert "DIRECT-MARKER" in rendered
    assert "compression_summary] {" not in rendered


def test_retrieved_summary_is_reserved_not_dropped():
    """Archive recall of a summary also renders explicitly (not 100-char repr)."""
    content = "R" * 200 + ":RETRIEVED-MARKER"
    archived = ContextChunk(
        id=uuid.uuid4(),
        session_id=SESSION_ID,
        agent_id="harness",
        chunk_type="compression_summary",
        payload={"content": content},
        token_count=60,
        created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
    )
    prompt = PromptAssembler(session_budget=8000).assemble(
        system_prompt="system",
        goal=None,
        recent_chunks=[],
        retrieved_chunks=[(archived, 0.9)],
        query="continue",
    )
    assert "## Compacted Context" in prompt
    assert "RETRIEVED-MARKER" in prompt


def test_only_the_latest_summary_is_reserved():
    older = {
        "type": "compression_summary",
        "payload": {"content": "OLDER-SUMMARY"},
        "tokens": 10,
    }
    newer = {
        "type": "compression_summary",
        "payload": {"content": "NEWER-SUMMARY"},
        "tokens": 10,
    }
    prompt = PromptAssembler(session_budget=8000).assemble(
        system_prompt="system",
        goal=None,
        recent_chunks=[newer, older],
        retrieved_chunks=[],
        query="continue",
    )
    assert "NEWER-SUMMARY" in prompt
    assert "OLDER-SUMMARY" not in prompt


def test_summary_is_not_duplicated_in_recent_activity():
    summary = {
        "type": "compression_summary",
        "payload": {"content": "ONCE-ONLY-SUMMARY"},
        "tokens": 10,
    }
    recent = [summary] + [
        {"type": "user_message", "payload": {"content": f"recent {i}"}, "tokens": 4}
        for i in range(2)
    ]
    prompt = PromptAssembler(session_budget=8000).assemble(
        system_prompt="system",
        goal=None,
        recent_chunks=recent,
        retrieved_chunks=[],
        query="continue",
    )
    assert prompt.count("ONCE-ONLY-SUMMARY") == 1
    assert "## Compacted Context" in prompt
    assert "recent 1" in prompt


def test_summary_section_respects_the_token_budget():
    summary = {
        "type": "compression_summary",
        "payload": {"content": "S" * 4000},
        "tokens": 1000,
    }
    recent = [
        {"type": "user_message", "payload": {"content": "recent " + "r" * 2000}, "tokens": 500},
        summary,
    ]
    budget = 400
    prompt = PromptAssembler(session_budget=budget).assemble(
        system_prompt="tiny system",
        goal=None,
        recent_chunks=recent,
        retrieved_chunks=[],
        query="q",
    )
    assert get_token_count(prompt) <= budget + 5
    assert "## Compacted Context" in prompt, "summary keeps a reserved share"
