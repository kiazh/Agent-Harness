"""Image submission crosses the real gateway, agent, context and usage boundary."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import uuid

import pytest
import pytest_asyncio

from machina.core.agent import ReActAgent
from machina.core.config import config
from machina.core.context import context_manager
from machina.core.models import AgentResponse, LLMResponse, StreamEvent
from machina.core.provider import LLMProvider
from machina.db.connection import db
from machina.gateway.errors import INVALID_PARAMS
from machina.gateway.server import Gateway

pytestmark = pytest.mark.skipif(
    not os.environ.get("MACHINA_TEST_DATABASE_URL"),
    reason="requires the dedicated test database",
)

PNG_DATA = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def png():
    return {"mediaType": "image/png", "data": PNG_DATA, "name": "pixel.png"}


class ImageFixtureProvider(LLMProvider):
    """Only the vendor network is replaced; the agent and usage store are real."""

    model = "fixture/vision"

    def __init__(self):
        self.messages = []
        self.window = 131072
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self.gate.set()

    async def context_window(self, model=None):
        return self.window

    async def stream_complete(self, messages, **kwargs):
        self.messages.append(copy.deepcopy(messages))
        self.started.set()
        await self.gate.wait()
        yield StreamEvent(type="text", content="A small image.")
        yield StreamEvent(
            type="done",
            response=LLMResponse(
                content="A small image.",
                model=self.model,
                usage={"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
            ),
        )


class ImageGateway:
    def __init__(self, agent):
        self.frames = []
        self.gateway = Gateway(
            self.frames.append, agent_factory=lambda _model, _provider: agent, owns_db=False
        )
        self.gateway._auth_token = "image-fixture-gateway-token"
        # The application owns the pool, as for a borrowed HTTP gateway.
        self.gateway._db_ready = True
        self.request_id = 0
        self.pending_turn_id = None

    async def call(self, method, params=None):
        self.request_id += 1
        rid = self.request_id
        await self.gateway.handle_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": method,
                    "params": {"token": "image-fixture-gateway-token", **(params or {})},
                }
            )
        )
        response = next(frame for frame in self.frames if frame.get("id") == rid)
        if method == "prompt.submit" and "result" in response:
            self.pending_turn_id = response["result"]["turnId"]
        return response

    def events(self, kind):
        return [
            frame["params"]
            for frame in self.frames
            if frame.get("method") == "event" and frame["params"]["type"] == kind
        ]

    async def complete(self):
        async with asyncio.timeout(10):
            while True:
                matches = [
                    event
                    for event in self.events("message.complete")
                    if event.get("turnId") == self.pending_turn_id
                ]
                if matches:
                    return matches[-1]
                await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def image_gateway(db_pool, monkeypatch):
    monkeypatch.setattr(db, "_pool", db_pool._pool)
    monkeypatch.setenv("MACHINA_GATEWAY_NO_SCHEDULER", "1")
    monkeypatch.setenv("MACHINA_GATEWAY_TOKEN", "image-fixture-gateway-token")
    for name in (
        "memory_enabled",
        "memory_consolidation_enabled",
        "personality_growth_enabled",
        "rsi_enabled",
        "rag_enabled",
        "auto_compaction_enabled",
    ):
        monkeypatch.setattr(config, name, False)
    provider = ImageFixtureProvider()
    agent = ReActAgent(provider=provider, max_iterations=1, allowed_tools=[])
    harness = ImageGateway(agent)
    result = await harness.call(
        "session.create", {"title": "image pipeline", "model": provider.model}
    )
    sid = result["result"]["session"]["id"]
    try:
        yield harness, sid, provider
    finally:
        await harness.gateway.close()
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", uuid.UUID(sid))


async def test_png_submission_reaches_provider_and_persists_attachment(image_gateway, db_pool):
    harness, sid, provider = image_gateway
    result = await harness.call(
        "prompt.submit", {"sessionId": sid, "text": "Describe this picture", "images": [png()]}
    )
    assert result["result"]["turnId"]
    complete = await harness.complete()
    assert complete["text"] == "A small image."
    assert not complete["cancelled"]

    blocks = provider.messages[0][0]["content"]
    text = next(block["text"] for block in blocks if block["type"] == "text")
    assert "Describe this picture" in text
    assert PNG_DATA not in text
    assert blocks[-1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + PNG_DATA},
    }
    chunks = await context_manager.get_chunks(uuid.UUID(sid))
    user = next(chunk for chunk in chunks if chunk.chunk_type == "user_message")
    assert user.payload["images"] == [png()]
    assert base64.b64decode(user.payload["images"][0]["data"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert user.token_count >= 8192
    row = await db_pool.fetchrow(
        "SELECT status, reserved_tokens FROM llm_usage WHERE session_id = $1", uuid.UUID(sid)
    )
    assert row["status"] == "complete"
    assert row["reserved_tokens"] > 8192
    history = (await harness.call("session.resume", {"sessionId": sid}))["result"]["history"]
    assert "[image 1]" in history[0]["content"]
    assert PNG_DATA not in json.dumps(history)


@pytest.mark.parametrize(
    "images",
    [
        {"mediaType": "image/png", "data": PNG_DATA},
        [{"mediaType": "image/png", "data": "!"}],
        [{"mediaType": "image/png", "data": "aGVsbG8="}],
        [{"mediaType": "image/png", "data": "A" * 7_000_000}],
    ],
)
async def test_invalid_images_are_rejected_before_turn_claim(image_gateway, db_pool, images):
    harness, sid, provider = image_gateway
    response = await harness.call(
        "prompt.submit", {"sessionId": sid, "text": "Describe", "images": images}
    )
    assert response["error"]["code"] == INVALID_PARAMS
    row = await db_pool.fetchrow("SELECT claim_owner FROM sessions WHERE id = $1", uuid.UUID(sid))
    assert row["claim_owner"] is None
    assert not harness.gateway.turn_running(uuid.UUID(sid))
    assert not harness.events("message.start")
    assert provider.messages == []
    assert await context_manager.get_chunks(uuid.UUID(sid)) == []


async def test_cancelled_image_turn_keeps_user_attachment_and_releases_claim(
    image_gateway, db_pool
):
    harness, sid, provider = image_gateway
    provider.gate.clear()
    await harness.call("prompt.submit", {"sessionId": sid, "text": "Describe", "images": [png()]})
    await asyncio.wait_for(provider.started.wait(), timeout=10)
    response = await harness.call("prompt.cancel", {"sessionId": sid})
    assert response["result"]["cancelled"]
    assert (await harness.complete())["cancelled"]
    async with asyncio.timeout(5):
        while harness.gateway.turn_running(uuid.UUID(sid)):
            await asyncio.sleep(0.01)
    row = await db_pool.fetchrow("SELECT claim_owner FROM sessions WHERE id = $1", uuid.UUID(sid))
    assert row["claim_owner"] is None
    users = await context_manager.get_chunks(uuid.UUID(sid), chunk_type="user_message")
    assert len(users) == 1 and users[0].payload["images"] == [png()]


@pytest.mark.parametrize("extra", [{}, {"images": []}])
async def test_text_only_submission_keeps_legacy_agent_signature(image_gateway, extra):
    harness, sid, _provider = image_gateway

    class LegacyAgent:
        async def run_stream(self, session_id, user_message, verbose=False):
            yield StreamEvent(type="done", response=AgentResponse(content=user_message))

    harness.gateway._agent_factory = lambda _model, _provider: LegacyAgent()
    response = await harness.call(
        "prompt.submit", {"sessionId": sid, "text": "Legacy text", **extra}
    )
    assert response["result"]["turnId"]
    assert (await harness.complete())["text"] == "Legacy text"
    assert not harness.events("error")


async def test_followup_after_resume_keeps_recent_image_bytes(image_gateway):
    harness, sid, provider = image_gateway
    await harness.call("prompt.submit", {"sessionId": sid, "text": "Describe", "images": [png()]})
    await harness.complete()
    await harness.call("session.resume", {"sessionId": sid})
    # A fresh gateway represents a resumed UI; no in-memory image side channel.
    agent = ReActAgent(provider=provider, max_iterations=1, allowed_tools=[])
    resumed = ImageGateway(agent)
    try:
        response = await resumed.call(
            "prompt.submit", {"sessionId": sid, "text": "What color was that image?"}
        )
        assert response["result"]["turnId"]
        assert (await resumed.complete())["text"] == "A small image."
        content = provider.messages[-1][0]["content"]
        assert isinstance(content, list)
        assert {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + PNG_DATA},
        } in content
        assert "What color was that image?" in next(
            block["text"] for block in content if block["type"] == "text"
        )
    finally:
        await resumed.gateway.close()


async def test_invalid_attachment_is_validated_before_existing_claim(image_gateway, db_pool):
    harness, sid, provider = image_gateway
    provider.gate.clear()
    await harness.call("prompt.submit", {"sessionId": sid, "text": "Describe", "images": [png()]})
    await asyncio.wait_for(provider.started.wait(), timeout=10)
    existing = await db_pool.fetchval(
        "SELECT claim_owner FROM sessions WHERE id = $1", uuid.UUID(sid)
    )
    assert existing
    rejected = await harness.call(
        "prompt.submit",
        {
            "sessionId": sid,
            "text": "second request",
            "images": [{"mediaType": "image/png", "data": "!"}],
        },
    )
    assert rejected["error"]["code"] == INVALID_PARAMS
    assert (
        await db_pool.fetchval("SELECT claim_owner FROM sessions WHERE id = $1", uuid.UUID(sid))
        == existing
    )
    assert len(harness.events("message.start")) == 1
    await harness.call("prompt.cancel", {"sessionId": sid})
    assert (await harness.complete())["cancelled"]


async def test_text_only_legacy_agent_can_be_cancelled(image_gateway):
    harness, sid, _provider = image_gateway
    started, gate = asyncio.Event(), asyncio.Event()

    class LegacyAgent:
        async def run_stream(self, session_id, user_message, verbose=False):
            started.set()
            await gate.wait()
            yield StreamEvent(type="done", response=AgentResponse(content="late answer"))

    harness.gateway._agent_factory = lambda _model, _provider: LegacyAgent()
    await harness.call("prompt.submit", {"sessionId": sid, "text": "Legacy text"})
    await asyncio.wait_for(started.wait(), timeout=5)
    response = await harness.call("prompt.cancel", {"sessionId": sid})
    assert response["result"]["cancelled"]
    assert (await harness.complete())["cancelled"]
    assert not harness.events("error")


async def test_followup_image_context_respects_budget_without_duplicating_text(image_gateway):
    harness, sid, provider = image_gateway
    # Fixture budget needs headroom over the prompt: automatic skill context
    # legitimately grows as skills are added (bounded injection), so a window
    # tuned to an older skill catalog would spuriously trip the guard. The
    # test's real assertions (budget respected, no text duplication) are below.
    provider.window = 19000
    original = "chart " * 3330
    response = await harness.call(
        "prompt.submit", {"sessionId": sid, "text": original, "images": [png()]}
    )
    assert response["result"]["turnId"]
    assert (await harness.complete())["text"] == "A small image."
    before = len(provider.messages)
    resumed = ImageGateway(ReActAgent(provider=provider, max_iterations=1, allowed_tools=[]))
    try:
        followup = await resumed.call(
            "prompt.submit", {"sessionId": sid, "text": "What color is it?"}
        )
        assert followup["result"]["turnId"]
        assert (await resumed.complete())["text"] == "A small image."
        assert len(provider.messages) == before + 1
        content = provider.messages[-1][0]["content"]
        assert {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + PNG_DATA},
        } in content
        text = next(block["text"] for block in content if block["type"] == "text")
        assert "What color is it?" in text
        assert text.count("chart ") <= 3330
        assert PNG_DATA not in text
    finally:
        await resumed.gateway.close()


@pytest.mark.parametrize("compact", [False, True])
async def test_followup_retains_image_after_tool_churn_and_compaction(image_gateway, compact):
    harness, sid, provider = image_gateway
    await harness.call("prompt.submit", {"sessionId": sid, "text": "Describe", "images": [png()]})
    await harness.complete()
    await harness.gateway.close()
    session_id = uuid.UUID(sid)
    await context_manager.add_chunks_batch(
        [
            {
                "session_id": session_id,
                "agent_id": "harness",
                "chunk_type": "result",
                "payload": {"content": f"Tool output {index}"},
                "token_count": 5,
            }
            for index in range(24)
        ]
    )
    recent = await context_manager.get_recent_context(session_id, limit=10)
    assert all(not chunk["payload"].get("images") for chunk in recent)
    if compact:
        originals = await context_manager.get_all_chunks(session_id)
        await context_manager.replace_chunks_by_ids(
            session_id, [chunk.id for chunk in originals], []
        )
        assert await context_manager.get_chunks(session_id) == []
    resumed = ImageGateway(ReActAgent(provider=provider, max_iterations=1, allowed_tools=[]))
    try:
        await resumed.call("session.resume", {"sessionId": sid})
        result = await resumed.call(
            "prompt.submit", {"sessionId": sid, "text": "What color is that image?"}
        )
        assert result["result"]["turnId"]
        await resumed.complete()
        content = provider.messages[-1][0]["content"]
        assert isinstance(content, list), (
            "tool churn must not discard the image referenced by a follow-up"
        )
        image = next(block for block in content if block["type"] == "image_url")
        assert base64.b64decode(image["image_url"]["url"].split(",", 1)[1]) == base64.b64decode(
            PNG_DATA
        )
    finally:
        await resumed.gateway.close()


@pytest.mark.parametrize("write", ["single", "batch"])
async def test_latest_image_storage_replaces_only_images_and_scopes_session_agent(
    image_gateway, db_pool, write
):
    harness, sid, provider = image_gateway
    session_id = uuid.UUID(sid)
    first, latest, foreign = (
        png(),
        {**png(), "name": "latest.png"},
        {**png(), "name": "foreign.png"},
    )
    await context_manager.add_chunk(
        session_id, "harness", "user_message", {"content": "first", "images": [first]}
    )
    image_chunk = {
        "session_id": session_id,
        "agent_id": "harness",
        "chunk_type": "user_message",
        "payload": {"content": "latest", "images": [latest]},
    }
    if write == "single":
        await context_manager.add_chunk(**image_chunk)
        await context_manager.add_chunk(
            session_id, "harness", "user_message", {"content": "text only"}
        )
    else:
        await context_manager.add_chunks_batch(
            [
                {**image_chunk, "payload": {"content": "earlier in batch", "images": [first]}},
                image_chunk,
                {**image_chunk, "payload": {"content": "text only"}},
            ]
        )
    await context_manager.add_chunk(
        session_id, "other-agent", "user_message", {"content": "foreign", "images": [foreign]}
    )
    other = (
        await harness.call("session.create", {"title": "other images", "model": provider.model})
    )["result"]["session"]["id"]
    other_id = uuid.UUID(other)
    try:
        await context_manager.add_chunk(
            other_id, "harness", "user_message", {"content": "another session", "images": [foreign]}
        )
        assert await context_manager.get_latest_images(session_id, "harness") == [latest]
        assert await context_manager.get_latest_images(session_id, "other-agent") == [foreign]
        assert await context_manager.get_latest_images(other_id, "harness") == [foreign]
        assert await context_manager.get_latest_images(session_id, "unknown-agent") == []
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM session_image_context WHERE session_id=$1 AND agent_id='harness'",
                session_id,
            )
            == 1
        )
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", other_id)
        assert await context_manager.get_latest_images(other_id, "harness") == []
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id=$1", other_id)


async def test_failed_chunk_batch_does_not_replace_latest_image_context(image_gateway, db_pool):
    import asyncpg

    harness, sid, provider = image_gateway
    session_id = uuid.UUID(sid)
    await context_manager.add_chunk(
        session_id, "harness", "user_message", {"content": "first", "images": [png()]}
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await context_manager.add_chunks_batch(
            [
                {
                    "session_id": session_id,
                    "agent_id": "harness",
                    "chunk_type": "user_message",
                    "payload": {"content": "new", "images": [{**png(), "name": "uncommitted.png"}]},
                },
                {
                    "session_id": session_id,
                    "agent_id": "harness",
                    "chunk_type": "invalid-type",
                    "payload": {"content": "reject"},
                },
            ]
        )
    assert await context_manager.get_latest_images(session_id, "harness") == [png()]
    assert len(await context_manager.get_chunks(session_id)) == 1


async def test_latest_image_getter_validates_saved_payload(image_gateway, db_pool):
    import msgpack

    harness, sid, provider = image_gateway
    session_id = uuid.UUID(sid)
    await context_manager.add_chunk(
        session_id, "harness", "user_message", {"content": "first", "images": [png()]}
    )
    corrupt = msgpack.packb(
        [{"mediaType": "image/png", "data": b"not-an-image", "name": "bad.png"}], use_bin_type=True
    )
    await db_pool.execute(
        "UPDATE session_image_context SET images_msgpack=$2 WHERE session_id=$1",
        session_id,
        corrupt,
    )
    # A corrupt cache row is quarantined and the turn recovers from legacy
    # chunks instead of failing every follow-up turn. Recovery reseeds the
    # cache, so the next read is served from a valid row.
    assert await context_manager.get_latest_images(session_id, "harness") == [png()]
    assert await context_manager.get_latest_images(session_id, "harness") == [png()]
    row = await db_pool.fetchrow(
        "SELECT images_msgpack FROM session_image_context WHERE session_id=$1", session_id
    )
    assert row is not None


async def test_corrupt_cache_without_legacy_chunks_returns_empty(image_gateway, db_pool):
    import msgpack

    harness, sid, _provider = image_gateway
    session_id = uuid.UUID(sid)
    await context_manager.add_chunk(session_id, "harness", "user_message", {"content": "plain"})
    await db_pool.execute(
        "INSERT INTO session_image_context (session_id, agent_id, images_msgpack)"
        " VALUES ($1, 'harness', $2)",
        session_id,
        msgpack.packb([{"mediaType": "image/png"}], use_bin_type=True),
    )
    assert await context_manager.get_latest_images(session_id, "harness") == []
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM session_image_context WHERE session_id=$1", session_id
        )
        == 0
    )


async def test_explicit_chunk_wipe_clears_latest_image_cache(image_gateway, db_pool):
    harness, sid, _provider = image_gateway
    session_id = uuid.UUID(sid)
    await context_manager.add_chunk(
        session_id, "harness", "user_message", {"content": "pic", "images": [png()]}
    )
    assert await context_manager.get_latest_images(session_id, "harness") == [png()]
    await context_manager.delete_chunks(session_id)
    assert await context_manager.get_latest_images(session_id, "harness") == []


async def test_image_cache_sql_bound_matches_application_constant():
    import re
    from pathlib import Path

    from machina.core.images import MAX_TOTAL_IMAGE_BYTES

    sql = (
        Path(__file__).resolve().parents[1] / "machina" / "db" / "image_context_schema.sql"
    ).read_text()
    match = re.search(r"octet_length\(images_msgpack\) <= (\d+)", sql)
    assert match is not None
    assert int(match.group(1)) == MAX_TOTAL_IMAGE_BYTES + 8192


async def test_unmigrated_database_still_completes_image_and_text_turns(image_gateway, db_pool):
    """Regression: a database without session_image_context (existing
    installations only run initialize_runtime_schema) must degrade, not
    fail every turn with 'agent turn failed'."""
    harness, sid, _provider = image_gateway
    await db_pool.execute("DROP TABLE IF EXISTS session_image_context")
    try:
        result = await harness.call(
            "prompt.submit", {"sessionId": sid, "text": "Describe", "images": [png()]}
        )
        assert result["result"]["turnId"]
        complete = await harness.complete()
        assert not complete.get("cancelled")
        assert harness.events("error") == []
        result = await harness.call("prompt.submit", {"sessionId": sid, "text": "heello"})
        assert result["result"]["turnId"]
        complete = await harness.complete()
        assert not complete.get("cancelled")
        assert harness.events("error") == []
    finally:
        await db.initialize_runtime_schema()


async def test_runtime_schema_migration_restores_image_context(image_gateway, db_pool):
    harness, sid, _provider = image_gateway
    session_id = uuid.UUID(sid)
    await db_pool.execute("DROP TABLE IF EXISTS session_image_context")
    try:
        assert await context_manager.get_latest_images(session_id, "harness") == []
        await db.initialize_runtime_schema()
        assert (
            await db_pool.fetchval("SELECT to_regclass('public.session_image_context')") is not None
        )
        await context_manager.add_chunk(
            session_id, "harness", "user_message", {"content": "first", "images": [png()]}
        )
        assert await context_manager.get_latest_images(session_id, "harness") == [png()]
    finally:
        await db.initialize_runtime_schema()


async def test_image_writes_fall_back_to_plain_chunks_without_table(image_gateway, db_pool):
    harness, sid, _provider = image_gateway
    session_id = uuid.UUID(sid)
    await db_pool.execute("DROP TABLE IF EXISTS session_image_context")
    try:
        chunk = await context_manager.add_chunk(
            session_id, "harness", "user_message", {"content": "keep me", "images": [png()]}
        )
        assert chunk.payload["content"] == "keep me"
        stored = await context_manager.get_chunks(session_id)
        assert any(c.payload.get("content") == "keep me" for c in stored)
        batch = await context_manager.add_chunks_batch(
            [
                {
                    "session_id": session_id,
                    "agent_id": "harness",
                    "chunk_type": "user_message",
                    "payload": {"content": "batch kept", "images": [png()]},
                }
            ]
        )
        assert [c.payload.get("content") for c in batch] == ["batch kept"]
    finally:
        await db.initialize_runtime_schema()


async def test_latest_image_storage_accepts_total_limit_with_unicode_names(image_gateway):
    from machina.core.images import MAX_IMAGES, MAX_TOTAL_IMAGE_BYTES

    _, sid, _ = image_gateway
    size = MAX_TOTAL_IMAGE_BYTES // MAX_IMAGES
    data = b"\x89PNG\r\n\x1a\n" + b"x" * (size - 8)
    images = [
        {"mediaType": "image/png", "data": base64.b64encode(data).decode(), "name": "😀" * 255}
        for _ in range(MAX_IMAGES)
    ]
    session_id = uuid.UUID(sid)
    await context_manager.add_chunk(
        session_id, "harness", "user_message", {"content": "maximum images", "images": images}
    )
    assert await context_manager.get_latest_images(session_id, "harness") == images


async def test_runtime_schema_updates_existing_image_size_constraint(image_gateway, db_pool):
    from machina.core.context import _MAX_IMAGE_CONTEXT_BYTES

    await db_pool.execute("""
        ALTER TABLE session_image_context DROP CONSTRAINT session_image_context_images_msgpack_check;
        ALTER TABLE session_image_context ADD CONSTRAINT session_image_context_images_msgpack_check
            CHECK (octet_length(images_msgpack) <= 20975616)
    """)
    await db.initialize_runtime_schema()
    await db.initialize_runtime_schema()
    constraint = await db_pool.fetchval("""
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid='session_image_context'::regclass
          AND conname='session_image_context_images_msgpack_check'
    """)
    assert str(_MAX_IMAGE_CONTEXT_BYTES) in constraint


async def test_upgrade_recovers_prior_image_turn_after_tool_churn(image_gateway, db_pool):
    import msgpack

    harness, sid, provider = image_gateway
    session_id = uuid.UUID(sid)
    # Simulate an attachment saved by the previous version, before the cache
    # existed. A later text-only user turn and other agents must not hide it.
    await db_pool.execute(
        """INSERT INTO context_chunks(session_id, agent_id, chunk_type, payload_msgpack)
           VALUES($1, 'harness', 'user_message', $2)""",
        session_id,
        msgpack.packb({"content": "prior attachment", "images": [png()]}, use_bin_type=True),
    )
    await context_manager.add_chunks_batch(
        [
            {
                "session_id": session_id,
                "agent_id": "harness",
                "chunk_type": "user_message",
                "payload": {"content": f"text only {index}"},
            }
            for index in range(15)
        ]
    )
    await context_manager.add_chunk(
        session_id,
        "other-agent",
        "user_message",
        {"content": "other agent", "images": [{**png(), "name": "other.png"}]},
    )
    await db.initialize_runtime_schema()
    await harness.call("prompt.submit", {"sessionId": sid, "text": "Recall the prior image"})
    await harness.complete()
    content = provider.messages[-1][0]["content"]
    assert isinstance(content, list)
    assert content[-1]["image_url"]["url"].endswith(PNG_DATA)


async def test_legacy_fallback_seeds_cache_before_compaction(image_gateway, db_pool):
    """An upgraded session (image chunks, no cache row) recovers images and
    seeds the durable cache, so compacting away legacy chunks cannot lose
    them for a later turn."""
    harness, sid, provider = image_gateway
    session_id = uuid.UUID(sid)
    await context_manager.add_chunk(
        session_id, "harness", "user_message", {"content": "old picture", "images": [png()]}
    )
    await db_pool.execute(
        "DELETE FROM session_image_context WHERE session_id=$1 AND agent_id='harness'",
        session_id,
    )
    try:
        recovered = await context_manager.get_latest_images(session_id, "harness")
        assert recovered == [png()]
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM session_image_context WHERE session_id=$1 AND agent_id='harness'",
                session_id,
            )
            == 1
        )
        originals = await context_manager.get_all_chunks(session_id)
        await context_manager.replace_chunks_by_ids(
            session_id, [chunk.id for chunk in originals], []
        )
        assert await context_manager.get_chunks(session_id) == []
        assert await context_manager.get_latest_images(session_id, "harness") == [png()]
        resumed = ImageGateway(ReActAgent(provider=provider, max_iterations=1, allowed_tools=[]))
        try:
            await resumed.call("session.resume", {"sessionId": sid})
            result = await resumed.call(
                "prompt.submit", {"sessionId": sid, "text": "What color is that image?"}
            )
            assert result["result"]["turnId"]
            await resumed.complete()
            content = provider.messages[-1][0]["content"]
            assert isinstance(content, list), "seeded cache must survive legacy compaction"
            image = next(block for block in content if block["type"] == "image_url")
            assert base64.b64decode(image["image_url"]["url"].split(",", 1)[1]) == base64.b64decode(
                PNG_DATA
            )
        finally:
            await resumed.gateway.close()
    finally:
        await db.initialize_runtime_schema()


async def test_legacy_scan_skips_newer_non_image_candidates(image_gateway, db_pool, caplog):
    """The legacy scan must continue past a newer text chunk whose payload
    trips the images-substring prefilter (a standalone "images" string that
    is not the dict key), and must not log a spurious cap warning."""
    import logging
    import time

    harness, sid, _provider = image_gateway
    session_id = uuid.UUID(sid)
    await context_manager.add_chunk(
        session_id,
        "harness",
        "user_message",
        {"content": "old picture", "images": [png()]},
    )
    time.sleep(0.02)
    await context_manager.add_chunk(
        session_id,
        "harness",
        "user_message",
        {"content": "images"},
    )
    await db_pool.execute(
        "DELETE FROM session_image_context WHERE session_id=$1 AND agent_id='harness'",
        session_id,
    )
    with caplog.at_level(logging.WARNING, logger="machina.core.context"):
        assert await context_manager.get_latest_images(session_id, "harness") == [png()]
    assert "exhausted" not in caplog.text


async def test_legacy_scan_advances_past_null_timestamp_candidate(image_gateway, db_pool):
    """Legacy rows with NULL timestamps still advance after a false image match."""
    import msgpack

    _harness, sid, _provider = image_gateway
    session_id = uuid.UUID(sid)
    image_id, text_id = sorted((uuid.uuid4(), uuid.uuid4()))
    for chunk_id, payload in (
        (image_id, {"content": "old picture", "images": [png()]}),
        (text_id, {"content": "images"}),
    ):
        await db_pool.execute(
            """INSERT INTO context_chunks
               (id, session_id, agent_id, chunk_type, payload_msgpack, created_at)
               VALUES ($1, $2, 'harness', 'user_message', $3, NULL)""",
            chunk_id,
            session_id,
            msgpack.packb(payload, use_bin_type=True),
        )

    assert await context_manager.get_latest_images(session_id, "harness") == [png()]


async def test_fork_preserves_compacted_image_context(image_gateway):
    """A fork keeps the latest image after its source chunk was compacted."""
    from machina.core.session import session_manager

    _harness, sid, _provider = image_gateway
    source_id = uuid.UUID(sid)
    image = png()
    chunk = await context_manager.add_chunk(
        source_id, "harness", "user_message", {"content": "picture", "images": [image]}
    )
    await context_manager.replace_chunks_by_ids(source_id, [chunk.id], [])
    assert await context_manager.get_latest_images(source_id, "harness") == [image]
    fork = await session_manager.fork(source_id)
    try:
        assert await context_manager.get_latest_images(fork.id, "harness") == [image]
    finally:
        await session_manager.delete(fork.id)


async def test_fork_without_image_cache_table_keeps_text_chunks(image_gateway, db_pool):
    """Forking on an unmigrated database still copies ordinary context."""
    from machina.core.session import session_manager

    _harness, sid, _provider = image_gateway
    source_id = uuid.UUID(sid)
    await context_manager.add_chunk(
        source_id, "harness", "user_message", {"content": "keep this text"}
    )
    await db_pool.execute("DROP TABLE IF EXISTS session_image_context")
    fork = None
    try:
        fork = await session_manager.fork(source_id)
        copied = await context_manager.get_chunks(fork.id)
        assert [chunk.payload["content"] for chunk in copied] == ["keep this text"]
    finally:
        if fork is not None:
            await session_manager.delete(fork.id)
        await db.initialize_runtime_schema()
