"""R4-012: Anthropic/Claude tool-result coalescing at the network boundary.

The Messages API requires alternating roles and exactly ONE user message
carrying every ``tool_result`` of an assistant tool_use batch. The agent
appends one ``role:"tool"`` message per result (``prompt.append_tool_messages``
via ``agent._append_tool_messages``), so a >=2-tool batch used to produce
consecutive ``user`` roles.

These probes build the same conversations the agent builds, run them through
the real providers with only the httpx transport swapped (``MockTransport``),
and assert on the captured outgoing POST body. ``_to_anthropic_messages`` is
never patched — the capture exercises it on the production request path.
"""

from __future__ import annotations

import json

import httpx

from machina.core.models import LLMResponse
from machina.core.prompt import append_tool_messages
from machina.core.provider import AnthropicProvider
from machina.core.provider.rate_limit import AsyncTokenBucket

MODEL = "claude-3-5-sonnet-20241022"


def _assistant(content: str, *call_ids: str) -> LLMResponse:
    """One assistant batch: text plus an ordered tool_use per call id."""
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
    """Append one assistant batch and its results exactly as the agent does.

    ``results`` is an ordered list of ``(call_id, text, is_error)`` tuples;
    ``is_error`` marks the tool message before the converter runs.
    """
    for call_id, text, is_error in results:
        tc = next(call for call in response.tool_calls if call["id"] == call_id)
        append_tool_messages(messages, response, tc, text)
        if is_error:
            messages[-1]["is_error"] = True


def _message_body() -> dict:
    return {
        "type": "message",
        "model": MODEL,
        "content": [{"type": "text", "text": "done"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _anthropic_provider(captured: list[dict]) -> AnthropicProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_message_body())

    provider = AnthropicProvider(api_key="test-key", model=MODEL)
    provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
    # Swap only the HTTP boundary; the payload shaping under test is the real one.
    provider.client = httpx.AsyncClient(
        base_url=AnthropicProvider.BASE_URL,
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    return provider


def _assert_alternating_and_batched(payload: dict) -> None:
    """The Messages API contract: alternation + one user per tool batch."""
    messages = payload["messages"]
    roles = [message["role"] for message in messages]
    assert roles[0] == "user", roles
    assert all(left != right for left, right in zip(roles, roles[1:])), f"consecutive: {roles}"
    assert set(roles) <= {"user", "assistant"}, roles
    for index, message in enumerate(messages):
        content = message["content"]
        if message["role"] != "assistant" or not isinstance(content, list):
            continue
        call_ids = [
            block["id"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if not call_ids:
            continue
        assert index + 1 < len(messages), f"batch {call_ids} has no result message"
        results = messages[index + 1]
        assert results["role"] == "user", f"batch {call_ids}: {results['role']}"
        blocks = results["content"]
        assert [block.get("type") for block in blocks] == ["tool_result"] * len(call_ids)
        assert [block.get("tool_use_id") for block in blocks] == call_ids


async def test_single_tool_result_is_one_user_message():
    captured: list[dict] = []
    provider = _anthropic_provider(captured)
    messages = [{"role": "user", "content": "r412 one tool"}]
    _append_batch(messages, _assistant("", "toolu_a"), [("toolu_a", "ok-1", False)])

    await provider.complete(messages)
    await provider.close()

    payload = captured[0]
    _assert_alternating_and_batched(payload)
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user"]
    assert payload["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_a", "content": "ok-1"}
    ]


async def test_two_tool_batch_coalesces_into_one_user_message():
    captured: list[dict] = []
    provider = _anthropic_provider(captured)
    messages = [{"role": "user", "content": "r412 two tools"}]
    _append_batch(
        messages,
        _assistant("", "toolu_a", "toolu_b"),
        [("toolu_a", "ok-a", False), ("toolu_b", "ok-b", False)],
    )

    await provider.complete(messages)
    await provider.close()

    payload = captured[0]
    _assert_alternating_and_batched(payload)
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user"]
    blocks = payload["messages"][2]["content"]
    assert [block["tool_use_id"] for block in blocks] == ["toolu_a", "toolu_b"]
    assert [block["content"] for block in blocks] == ["ok-a", "ok-b"]
    assert all("is_error" not in block for block in blocks)


async def test_three_tool_batch_preserves_order_and_error_flags():
    captured: list[dict] = []
    provider = _anthropic_provider(captured)
    messages = [{"role": "user", "content": "r412 three tools"}]
    _append_batch(
        messages,
        _assistant("", "toolu_a", "toolu_b", "toolu_c"),
        [
            ("toolu_a", "ok-a", False),
            ("toolu_b", "Error: boom-b", True),
            ("toolu_c", "ok-c", False),
        ],
    )

    await provider.complete(messages)
    await provider.close()

    payload = captured[0]
    _assert_alternating_and_batched(payload)
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user"]
    blocks = payload["messages"][2]["content"]
    assert [block["tool_use_id"] for block in blocks] == ["toolu_a", "toolu_b", "toolu_c"]
    assert [block["content"] for block in blocks] == ["ok-a", "Error: boom-b", "ok-c"]
    assert [block.get("is_error") is True for block in blocks] == [False, True, False]


async def test_interleaved_batches_alternate_roles_and_text():
    captured: list[dict] = []
    provider = _anthropic_provider(captured)
    messages = [{"role": "user", "content": "r412 two batches"}]
    _append_batch(
        messages,
        _assistant("first batch", "toolu_a", "toolu_b"),
        [("toolu_a", "ok-a", False), ("toolu_b", "ok-b", False)],
    )
    _append_batch(
        messages,
        _assistant("second batch", "toolu_c"),
        [("toolu_c", "Error: boom-c", True)],
    )

    await provider.complete(messages)
    await provider.close()

    payload = captured[0]
    _assert_alternating_and_batched(payload)
    msgs = payload["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    assert msgs[1]["content"][0] == {"type": "text", "text": "first batch"}
    assert msgs[3]["content"][0] == {"type": "text", "text": "second batch"}
    assert [block["tool_use_id"] for block in msgs[2]["content"]] == ["toolu_a", "toolu_b"]
    assert msgs[4]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_c",
            "content": "Error: boom-c",
            "is_error": True,
        }
    ]


async def test_adjacent_tool_run_coalesces_without_corrupting_user_text():
    """Adversary: a tool run after a user text message must not merge into the
    text message (string content) yet must still coalesce into one message."""
    captured: list[dict] = []
    provider = _anthropic_provider(captured)
    messages = [
        {"role": "user", "content": "r412 standalone text"},
        {"role": "tool", "tool_call_id": "toolu_a", "content": "ok-a"},
        {"role": "tool", "tool_call_id": "toolu_b", "content": "ok-b"},
    ]

    await provider.complete(messages)
    await provider.close()

    msgs = captured[0]["messages"]
    assert msgs[0] == {"role": "user", "content": "r412 standalone text"}
    assert [m["role"] for m in msgs] == ["user", "user"]
    assert [block["tool_use_id"] for block in msgs[1]["content"]] == ["toolu_a", "toolu_b"]
    assert [block["content"] for block in msgs[1]["content"]] == ["ok-a", "ok-b"]
