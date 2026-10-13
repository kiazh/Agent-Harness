"""Exports and resumed history survive compaction and worker restarts."""

import uuid
from unittest.mock import AsyncMock

import pytest

from machina import services
from machina.core.context import ContextManager, context_manager
from machina.core.models import ContextChunk, Session
from machina.core.serialization import payload_to_msgpack
from machina.gateway.server import Gateway


async def test_export_preserves_saved_tool_result_and_marks_legacy_preview(monkeypatch):
    session = Session(id=uuid.uuid4())
    secret = "sk-" + "a" * 24
    result = "first\n" + "line\n" * 150 + secret + "\nlast verdict"
    chunks = [
        ContextChunk(
            id=uuid.uuid4(),
            session_id=session.id,
            agent_id="harness",
            chunk_type="tool_call",
            payload={
                "tool": "terminal",
                "args": {},
                "result": result,
                "result_preview": result[:500],
            },
        ),
        ContextChunk(
            id=uuid.uuid4(),
            session_id=session.id,
            agent_id="harness",
            chunk_type="tool_call",
            payload={"tool": "read_file", "args": {}, "result_preview": "legacy preview"},
        ),
    ]
    # Both seams allow this regression to fail on the old exporter, then test
    # the new transcript reader independently against the real database below.
    monkeypatch.setattr(context_manager, "get_all_chunks", AsyncMock(return_value=chunks))
    monkeypatch.setattr(
        context_manager, "get_transcript_chunks", AsyncMock(return_value=chunks), raising=False
    )
    markdown = await services.export_markdown(session)
    assert "last verdict" in markdown
    assert secret not in markdown
    assert "legacy preview" in markdown
    assert "only a preview was stored" in markdown


async def test_resume_reads_beyond_two_hundred_chunks(monkeypatch):
    session = Session(id=uuid.uuid4())
    chunks = [
        ContextChunk(
            id=uuid.uuid4(),
            session_id=session.id,
            agent_id="harness",
            chunk_type="user_message",
            payload={"content": f"message {index}"},
        )
        for index in range(205)
    ]
    gw = Gateway(lambda frame: None)
    gw._db_ready = True
    monkeypatch.setattr(gw, "get_session", AsyncMock(return_value=session))
    monkeypatch.setattr(context_manager, "get_chunks", AsyncMock(return_value=chunks[:200]))
    monkeypatch.setattr(
        context_manager, "get_transcript_chunks", AsyncMock(return_value=chunks), raising=False
    )
    resumed = await gw._session_resume({"sessionId": str(session.id)})
    assert len(resumed["history"]) == 205


async def test_export_and_resume_include_archived_originals_after_restart(db_pool, monkeypatch):
    import machina.core.context as context_module

    monkeypatch.setattr(context_module, "db", db_pool)
    context = ContextManager()
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions(agent_id, title) VALUES('harness', 'Transcript') RETURNING id"
    )
    session = Session(id=session_id)
    try:
        first = await context.add_chunk(
            session_id, "harness", "user_message", {"content": "original before compaction"}
        )
        # Real compaction archives the originals before removing active rows.
        summary = ContextChunk(
            id=uuid.uuid4(),
            session_id=session_id,
            agent_id="harness",
            chunk_type="compression_summary",
            payload={"content": "compacted summary"},
        )
        await context.replace_chunks_by_ids(
            session_id, [first.id], [summary], archive_reason="compressed"
        )
        await context.add_chunk(
            session_id, "harness", "assistant_message", {"content": "reply after resume"}
        )
        # Repeated archival/restoration must not duplicate a transcript message.
        for content in ("older copy", "original before compaction"):
            await context.archive_chunk(
                session_id,
                first.id,
                payload_to_msgpack({"content": content}),
                agent_id="harness",
                chunk_type="user_message",
                created_at=first.created_at,
            )
        await context.add_chunk(session_id, "harness", "document", {"text": "RAG only"})
        other_id = await db_pool.fetchval(
            "INSERT INTO sessions(agent_id) VALUES('other') RETURNING id"
        )
        try:
            await context.add_chunk(other_id, "other", "user_message", {"content": "private"})
            # Fresh objects stand in for a new gateway process: no cached transcript.
            monkeypatch.setattr(services, "context_manager", ContextManager())
            markdown = await services.export_markdown(session)
            assert markdown.count("original before compaction") == 1
            assert "older copy" not in markdown
            assert "RAG only" not in markdown and "private" not in markdown
            assert markdown.index("original before compaction") < markdown.index(
                "reply after resume"
            )
            monkeypatch.setattr("machina.gateway.server.context_manager", ContextManager())
            gw = Gateway(lambda frame: None)
            gw._db_ready = True
            monkeypatch.setattr(gw, "get_session", AsyncMock(return_value=session))
            resumed = await gw._session_resume({"sessionId": str(session_id)})
            assert "original before compaction" in [
                entry["content"] for entry in resumed["history"]
            ]
            assert (
                await db_pool.fetchval(
                    "SELECT SUM(resurrection_count) FROM context_archive WHERE session_id=$1",
                    session_id,
                )
                == 0
            )
        finally:
            await db_pool.execute("DELETE FROM sessions WHERE id=$1", other_id)
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", session_id)


@pytest.mark.parametrize("streaming", [False, True])
async def test_agent_persists_assistant_preamble_and_bounded_tool_output(monkeypatch, streaming):
    from machina.core.agent import ReActAgent
    from machina.core.models import LLMResponse, StreamEvent

    session = Session(id=uuid.uuid4())
    saved = []

    async def save(chunks):
        saved.extend(chunks)

    agent = ReActAgent(provider=object(), allowed_tools=["list_agents"])
    monkeypatch.setattr(agent, "_prepare_context", AsyncMock(return_value=(session, [])))
    monkeypatch.setattr(context_manager, "add_chunks_batch", save)
    monkeypatch.setattr("machina.core.agent.session_manager.get", AsyncMock(return_value=session))
    monkeypatch.setattr("machina.core.agent.session_manager.update_activity", AsyncMock())
    monkeypatch.setattr(agent, "_schedule_memory_consolidation", lambda *args: None)
    monkeypatch.setattr(agent, "_schedule_learning_review", lambda *args: None)
    monkeypatch.setattr(agent, "_complete_growth", AsyncMock())
    output = "detail\n" * 12000 + "last verdict"
    monkeypatch.setattr("machina.tools.agents.list_agents", lambda: output)
    # Registry dispatch holds its registered callable; patch its executor at
    # this seam to isolate persistence from permission/catalog behavior.
    monkeypatch.setattr("machina.core.agent.registry.execute", AsyncMock(return_value=output))
    responses = iter(
        [
            LLMResponse(
                model="fixture",
                content="I will inspect the available roles.",
                tool_calls=[
                    {
                        "id": "call",
                        "type": "function",
                        "function": {"name": "list_agents", "arguments": "{}"},
                    }
                ],
            ),
            LLMResponse(model="fixture", content="finished"),
        ]
    )

    async def complete(**kwargs):
        return next(responses)

    async def stream(**kwargs):
        yield StreamEvent(type="done", response=await complete(**kwargs))

    monkeypatch.setattr(agent, "_call_llm_with_retry", complete)
    monkeypatch.setattr(agent, "_stream_llm_with_retry", stream)
    if streaming:
        _ = [event async for event in agent.run_stream(session.id, "inspect roles", verbose=False)]
    else:
        await agent.run(session.id, "inspect roles", verbose=False)
    assert saved[0]["chunk_type"] == "assistant_message"
    assert saved[0]["payload"]["content"] == "I will inspect the available roles."
    tool = next(chunk for chunk in saved if chunk["chunk_type"] == "tool_call")
    assert len(tool["payload"]["result"]) == 65536
    assert tool["payload"]["result_truncated"] is True
    assert len(tool["payload"]["result_preview"]) == 500


async def test_stop_during_provider_stream_preserves_visible_partial_text(monkeypatch):
    import asyncio

    from machina.core.agent import ReActAgent
    from machina.core.models import StreamEvent

    session = Session(id=uuid.uuid4())
    agent = ReActAgent(provider=object(), allowed_tools=[])
    saved = []

    async def save(chunks):
        saved.extend(chunks)

    async def interrupted(**kwargs):
        yield StreamEvent(type="text", content="Visible before stop")
        raise asyncio.CancelledError()

    monkeypatch.setattr(agent, "_prepare_context", AsyncMock(return_value=(session, [])))
    monkeypatch.setattr(agent, "_stream_llm_with_retry", interrupted)
    monkeypatch.setattr(context_manager, "add_chunks_batch", save)
    with pytest.raises(asyncio.CancelledError):
        _ = [event async for event in agent.run_stream(session.id, "inspect", verbose=False)]
    assert any(chunk["payload"].get("content") == "Visible before stop" for chunk in saved)
    assert any(chunk["payload"].get("interrupted") is True for chunk in saved)


def test_export_redaction_does_not_treat_source_paths_as_aws_keys():
    from machina.memory.redaction import StreamingSecretRedactor, redact_secrets

    path = "/Users/shadow/machina/machina/core/orchestrator.py"
    assert redact_secrets(path).text == path
    for split in range(len(path) + 1):
        redactor = StreamingSecretRedactor()
        assert redactor.feed(path[:split]) + redactor.feed(path[split:]) + redactor.flush() == path
    secret = "aB3dE5fG7hI9jK1lM2nO4pQ6rS8tU0vWxYz01234"
    assert secret not in redact_secrets(secret).text


async def test_real_repeated_compaction_preserves_each_original_exactly_once(db_pool, monkeypatch):
    import machina.core.context as context_module
    from machina.core.config import config
    from machina.db.connection import db

    monkeypatch.setattr(context_module, "db", db_pool)
    monkeypatch.setattr(db, "_pool", db_pool.pool)
    monkeypatch.setattr(config, "compression_llm_summarize", False)
    monkeypatch.setattr(config, "compression_preserve_recent", 2)
    monkeypatch.setattr(config, "compression_target_ratio", 0.25)
    context = ContextManager()
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions(agent_id) VALUES('harness') RETURNING id"
    )
    session = Session(id=session_id)
    originals = []
    try:
        for index in range(8):
            content = f"unique message {index}: " + "original detail " * 50 + f"end {index}"
            originals.append(content)
            await context.add_chunk(
                session_id, "harness", "user_message", {"content": content}, 100
            )
        for _ in range(2):
            assert await services.compress_session(session) is not None
        markdown = await services.export_markdown(session)
        for content in originals:
            assert markdown.count(content) == 1
            assert markdown.count(content.split(":")[0]) == 1
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", session_id)


@pytest.mark.parametrize("streaming", [False, True])
async def test_stop_during_preamble_flush_retries_persistence(monkeypatch, streaming):
    import asyncio

    from machina.core.agent import ReActAgent
    from machina.core.models import LLMResponse, StreamEvent

    session = Session(id=uuid.uuid4())
    agent = ReActAgent(provider=object(), allowed_tools=[])
    saved = []
    attempts = 0

    async def save(chunks):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError()
        saved.extend(chunks)

    response = LLMResponse(
        model="fixture",
        content="Inspecting before tool execution",
        tool_calls=[
            {
                "id": "call",
                "type": "function",
                "function": {"name": "list_agents", "arguments": "{}"},
            }
        ],
    )

    async def stream(**kwargs):
        yield StreamEvent(type="done", response=response)

    monkeypatch.setattr(agent, "_prepare_context", AsyncMock(return_value=(session, [])))
    monkeypatch.setattr(agent, "_call_llm_with_retry", AsyncMock(return_value=response))
    monkeypatch.setattr(agent, "_stream_llm_with_retry", stream)
    monkeypatch.setattr(context_manager, "add_chunks_batch", save)
    monkeypatch.setattr(context_manager, "add_chunk", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        if streaming:
            _ = [event async for event in agent.run_stream(session.id, "inspect", verbose=False)]
        else:
            await agent.run(session.id, "inspect", verbose=False)
    assert saved[0]["payload"]["content"] == response.content
