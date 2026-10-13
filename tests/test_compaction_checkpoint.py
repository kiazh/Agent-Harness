"""Stream C: compaction checkpoint round-trip (mocked providers)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from machina.core.compression import (
    CompressionConfig,
    ContextCompressor,
    build_checkpoint,
    effective_instruction,
    extract_checkpoint,
    validate_checkpoint,
)
from machina.core.models import ContextChunk


def _chunks(n=6):
    now = datetime.now(UTC).replace(tzinfo=None)
    out = []
    for i in range(n):
        out.append(
            ContextChunk(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                agent_id="harness",
                chunk_type="user_message",
                payload={"content": f"message {i} with enough text to compress"},
                token_count=20,
                created_at=now,
            )
        )
    return out


def _checkpoint():
    return build_checkpoint(
        objective="Fix delegation outcomes",
        expected_artifact="/tmp/report.md",
        constraints=["do not edit outside owned files", "no commits"],
        read_only_scope="/tmp read-only except owned files",
        findings=[{"finding": "failed children marked complete", "count": 3}],
        unresolved_questions=["deadline for nested?"],
        next_action="Map failed outcome to error status",
    )


def test_checkpoint_round_trip_preserves_constraints_and_counts():
    cp = _checkpoint()
    comp = ContextCompressor(config=CompressionConfig(llm_summarize=False, preserve_recent=2))
    result = comp.compress(_chunks(), uuid.uuid4(), "harness", checkpoint=cp)
    summaries = [c for c in result.compressed_chunks if c.chunk_type == "compression_summary"]
    assert summaries
    recovered = extract_checkpoint(summaries[0].payload)
    assert recovered is not None
    assert recovered["constraints"] == ["do not edit outside owned files", "no commits"]
    assert recovered["read_only_scope"] == "/tmp read-only except owned files"
    assert recovered["findings"] == [{"finding": "failed children marked complete", "count": 3}]
    assert recovered["objective"] == "Fix delegation outcomes"
    assert recovered["next_action"] == "Map failed outcome to error status"


async def test_checkpoint_round_trip_async_llm_path():
    from unittest.mock import AsyncMock

    from machina.core.models import LLMResponse

    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(content="summary", model="m", usage={"total_tokens": 5})
    )
    # Route complete_call through usage_store mock
    from machina.core.usage import usage_store

    async def fake_complete_call(llm_provider, *args, **kwargs):
        resp = await llm_provider.complete()
        return resp

    import unittest.mock as mock

    cp = _checkpoint()
    comp = ContextCompressor(config=CompressionConfig(llm_summarize=True, preserve_recent=2))
    with mock.patch.object(usage_store, "complete_call", side_effect=fake_complete_call):
        result = await comp.acompress(_chunks(), uuid.uuid4(), "harness", llm_provider=provider, checkpoint=cp)
    summaries = [c for c in result.compressed_chunks if c.chunk_type == "compression_summary"]
    assert summaries
    recovered = extract_checkpoint(summaries[0].payload)
    assert recovered["findings"][0]["count"] == 3


def test_incomplete_checkpoint_rejected():
    with pytest.raises(ValueError, match="missing"):
        validate_checkpoint({"objective": "x"})
    with pytest.raises(ValueError, match="count"):
        validate_checkpoint(
            {
                "objective": "o",
                "expected_artifact": "a",
                "constraints": [],
                "read_only_scope": "",
                "findings": [{"finding": "f"}],
                "unresolved_questions": [],
                "next_action": "n",
            }
        )
    with pytest.raises(ValueError):
        extract_checkpoint({"content": "s", "checkpoint": {"objective": "x"}})
    assert extract_checkpoint({"content": "no checkpoint here"}) is None


def test_latest_instruction_takes_priority():
    cp = _checkpoint()
    assert effective_instruction(cp, "New explicit direction: stop and verify") == (
        "New explicit direction: stop and verify"
    )
    assert effective_instruction(cp, None) == "Map failed outcome to error status"
    assert effective_instruction(None, None) is None
