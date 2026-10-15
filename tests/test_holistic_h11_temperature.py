"""H11 (holistic review 2026-10-15): configured temperature must reach providers.

Pre-fix both agent call paths forwarded ``max_tokens`` but omitted the
validated configured ``temperature``; the provider default stayed in effect,
so changing the exposed setting did not change agent sampling. These pins
capture the provider-boundary kwargs for the completion and streaming paths.
"""

from __future__ import annotations

from machina.core.agent import ReActAgent
from machina.core.config import config
from machina.core.models import LLMResponse, StreamEvent


async def test_completion_path_forwards_configured_temperature(monkeypatch):
    seen: dict = {}

    class Provider:
        model = "stub-model"

        async def complete(self, **kwargs):
            seen.update(kwargs)
            return LLMResponse(content="ok", model="stub-model")

    monkeypatch.setattr(config, "temperature", 0.25)
    monkeypatch.setattr(config, "max_tokens", 2048)
    agent = ReActAgent(provider=Provider(), allowed_tools=[])

    response = await agent._call_llm_with_retry(
        [{"role": "user", "content": "hi"}], [], session_id=None
    )

    assert response.content == "ok"
    assert seen.get("temperature") == 0.25
    assert seen.get("max_tokens") == 2048


async def test_streaming_path_forwards_configured_temperature(monkeypatch):
    seen: dict = {}

    class Provider:
        model = "stub-model"

        async def stream_complete(self, **kwargs):
            seen.update(kwargs)
            yield StreamEvent(
                type="done",
                response=LLMResponse(content="ok", model="stub-model"),
            )

    monkeypatch.setattr(config, "temperature", 1.5)
    monkeypatch.setattr(config, "max_tokens", 2048)
    agent = ReActAgent(provider=Provider(), allowed_tools=[])

    events = [
        event
        async for event in agent._stream_llm_with_retry(
            [{"role": "user", "content": "hi"}], [], session_id=None
        )
    ]

    assert [event.type for event in events] == ["done"]
    assert seen.get("temperature") == 1.5
    assert seen.get("max_tokens") == 2048
