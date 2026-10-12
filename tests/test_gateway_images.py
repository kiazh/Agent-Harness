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

from ah.core.agent import ReActAgent
from ah.core.config import config
from ah.core.context import context_manager
from ah.core.models import AgentResponse, LLMResponse, StreamEvent
from ah.core.provider import LLMProvider
from ah.db.connection import db
from ah.gateway.errors import INVALID_PARAMS
from ah.gateway.server import Gateway

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_DATABASE_URL"),
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
        return next(frame for frame in self.frames if frame.get("id") == rid)

    def events(self, kind):
        return [
            frame["params"]
            for frame in self.frames
            if frame.get("method") == "event" and frame["params"]["type"] == kind
        ]

    async def complete(self):
        async with asyncio.timeout(10):
            while not self.events("message.complete"):
                await asyncio.sleep(0.01)
        return self.events("message.complete")[-1]


@pytest_asyncio.fixture
async def image_gateway(db_pool, monkeypatch):
    monkeypatch.setattr(db, "_pool", db_pool._pool)
    monkeypatch.setenv("AH_GATEWAY_NO_SCHEDULER", "1")
    monkeypatch.setenv("AH_GATEWAY_TOKEN", "image-fixture-gateway-token")
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
    provider.window = 18000
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
