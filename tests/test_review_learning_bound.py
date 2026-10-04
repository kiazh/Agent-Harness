"""Post-turn learning cannot accumulate unlimited in-flight provider calls."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

from ah.core.agent import ReActAgent
from ah.core.models import AgentResponse


async def test_learning_reviews_have_a_process_wide_pending_limit(monkeypatch):
    from ah.core import agent as module

    monkeypatch.setattr(module.config, "get", lambda key: key == "learning_review_enabled")
    gate = asyncio.Event()

    async def wait_review(*_args):
        await gate.wait()

    agents = []
    response = AgentResponse(content="done", tool_calls=[], tokens_used=1, iterations=1)
    for _ in range(20):
        instance = ReActAgent.__new__(ReActAgent)
        instance._learning_tasks = set()
        instance._review_learning = AsyncMock(side_effect=wait_review)
        instance._schedule_learning_review(uuid.uuid4(), "task", response)
        agents.append(instance)
    await asyncio.sleep(0)
    try:
        assert sum(len(instance._learning_tasks) for instance in agents) <= 16
    finally:
        gate.set()
        await asyncio.gather(
            *(task for instance in agents for task in instance._learning_tasks),
            return_exceptions=True,
        )
        await asyncio.sleep(0)
