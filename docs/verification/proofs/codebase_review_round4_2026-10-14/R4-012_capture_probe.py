"""R4-012 capture probe: dump the outgoing Anthropic request body.

Run with ``.venv/bin/python`` from the repo root against the revision under
proof. Exercises the real provider path (only the httpx transport is a
``MockTransport``); ``_to_anthropic_messages`` is never patched. Output is
sanitized: synthetic tool ids/content only.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from machina.core.models import LLMResponse
from machina.core.prompt import append_tool_messages
from machina.core.provider import AnthropicProvider
from machina.core.provider.rate_limit import AsyncTokenBucket

MODEL = "claude-3-5-sonnet-20241022"


def _assistant(content: str, *call_ids: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        model=MODEL,
        usage={},
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": f"run_{call_id}", "arguments": "{}"},
            }
            for call_id in call_ids
        ],
    )


def _append_batch(messages: list[dict], response: LLMResponse, results: list[tuple]) -> None:
    for call_id, text, is_error in results:
        tc = next(call for call in response.tool_calls if call["id"] == call_id)
        append_tool_messages(messages, response, tc, text)
        if is_error:
            messages[-1]["is_error"] = True


async def _capture(messages: list[dict]) -> dict:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "type": "message",
                "model": MODEL,
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    provider = AnthropicProvider(api_key="test-key", model=MODEL)
    provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
    provider.client = httpx.AsyncClient(
        base_url=AnthropicProvider.BASE_URL,
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    await provider.complete(messages)
    await provider.close()
    return captured[0]


async def main() -> None:
    cases: dict[str, list[dict]] = {}

    one = [{"role": "user", "content": "probe one tool"}]
    _append_batch(one, _assistant("", "toolu_a"), [("toolu_a", "ok-1", False)])
    cases["(a) one tool"] = one

    two = [{"role": "user", "content": "probe two tools"}]
    _append_batch(
        two,
        _assistant("", "toolu_a", "toolu_b"),
        [("toolu_a", "ok-a", False), ("toolu_b", "ok-b", False)],
    )
    cases["(b) two tools"] = two

    three = [{"role": "user", "content": "probe three tools"}]
    _append_batch(
        three,
        _assistant("", "toolu_a", "toolu_b", "toolu_c"),
        [
            ("toolu_a", "ok-a", False),
            ("toolu_b", "Error: boom-b", True),
            ("toolu_c", "ok-c", False),
        ],
    )
    cases["(c) three mixed"] = three

    interleaved = [{"role": "user", "content": "probe two batches"}]
    _append_batch(
        interleaved,
        _assistant("first batch", "toolu_a", "toolu_b"),
        [("toolu_a", "ok-a", False), ("toolu_b", "ok-b", False)],
    )
    _append_batch(
        interleaved,
        _assistant("second batch", "toolu_c"),
        [("toolu_c", "Error: boom-c", True)],
    )
    cases["(d) interleaved batches"] = interleaved

    for name, messages in cases.items():
        payload = await _capture(messages)
        roles = [message["role"] for message in payload["messages"]]
        print(f"--- {name}: roles={roles}")
        print(json.dumps(payload["messages"], indent=2))
        print()


if __name__ == "__main__":
    asyncio.run(main())
