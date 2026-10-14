"""E1 pilot — Anthropic prompt-cache breakpoints (config-gated, default OFF).

Measured motivation (S5.3 probe): the stable prefix (system prompt + tool
schemas) is ~3.4k tokens and ~71% of a fixture turn's resent bytes are the
identical prefix. When ``prompt_cache_enabled`` is on, the two stable-prefix
ends get an Anthropic-style ephemeral ``cache_control`` breakpoint:

* the final system content block, and
* the last tool schema entry.

Scope: ``AnthropicProvider`` natively; OpenRouter only for models routed to
Anthropic (``anthropic`` in the model id, using OpenRouter's documented
passthrough for system content blocks and function tools); Direct providers
and Ollama are no-ops.

Expected savings are deliberately NOT claimed or measured here: real benefit
is bounded by each serving model's minimum cacheable prompt length (~1k-4k
tokens for current Claude models), by real-turn prefix stability, and by the
~1.25x cache-write premium until a prefix is read back within the TTL. Sizing
that on real turns remains a follow-up; the debug log states the expectation
instead of inventing a number.

The OFF-path goldens were captured from the pre-change payload builders and
pin the exact wire bytes: with the flag off (the default) requests must be
byte-identical to before this feature existed.
"""

from __future__ import annotations

import copy
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from machina.core.config import config
from machina.core.models import ToolDefinition
from machina.core.provider import (
    PROVIDER_SPECS,
    AnthropicProvider,
    AsyncTokenBucket,
    DirectProvider,
    OllamaProvider,
    OpenRouterProvider,
)

# ─── fixture turn ───────────────────────────────────────────────────────────

SYSTEM_TEXT = "You are Machina, a careful agent."
MESSAGES = [
    {"role": "system", "content": SYSTEM_TEXT},
    {"role": "user", "content": "hello"},
]
TOOLS = [
    ToolDefinition("clock", "Read the time", {"type": "object", "properties": {}}),
    ToolDefinition(
        "echo",
        "Echo text",
        {"type": "object", "properties": {"text": {"type": "string"}}},
    ),
]
OR_ANTHROPIC_MODEL = "anthropic/claude-sonnet-4"
OR_OTHER_MODEL = "openai/gpt-4o"

# ─── pre-change goldens (captured before the feature existed) ───────────────
# Key order is part of the pin: httpx serializes with
# json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).

_ANTHROPIC_TOOLS = [
    {
        "name": "clock",
        "description": "Read the time",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "echo",
        "description": "Echo text",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
]

GOLDEN_ANTHROPIC_COMPLETE = {
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 4096,
    "messages": [{"role": "user", "content": "hello"}],
    "system": SYSTEM_TEXT,
    "temperature": 0.7,
    "tools": _ANTHROPIC_TOOLS,
}
GOLDEN_ANTHROPIC_STREAM = {**GOLDEN_ANTHROPIC_COMPLETE, "stream": True}

_OR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "clock",
            "description": "Read the time",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo text",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        },
    },
]

GOLDEN_OR_COMPLETE = {
    "model": OR_ANTHROPIC_MODEL,
    "messages": [
        {"role": "system", "content": SYSTEM_TEXT},
        {"role": "user", "content": "hello"},
    ],
    "temperature": 0.7,
    "max_tokens": 4096,
    "tools": _OR_TOOLS,
}
GOLDEN_OR_STREAM = {
    **{k: v for k, v in GOLDEN_OR_COMPLETE.items() if k != "tools"},
    "stream": True,
    "stream_options": {"include_usage": True},
    "tools": _OR_TOOLS,
}
GOLDEN_OR_OTHER_MODEL = {**GOLDEN_OR_COMPLETE}
GOLDEN_OR_OTHER_MODEL["model"] = OR_OTHER_MODEL  # in-place keeps key position

GOLDEN_DIRECT_COMPLETE = {
    "model": "gpt-4o-mini",
    "messages": [
        {"role": "system", "content": SYSTEM_TEXT},
        {"role": "user", "content": "hello"},
    ],
    "temperature": 0.7,
    "max_tokens": 4096,
    "tools": _OR_TOOLS,
}

GOLDEN_OLLAMA_COMPLETE = {
    "model": "llama3.1",
    "messages": [
        {"role": "system", "content": SYSTEM_TEXT},
        {"role": "user", "content": "hello"},
    ],
    "stream": False,
    "options": {"temperature": 0.7, "num_predict": 4096},
    "tools": _OR_TOOLS,
}

EPHEMERAL = {"type": "ephemeral"}


def _wire(payload: dict) -> bytes:
    """Serialize exactly like httpx.encode_json does on the wire."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def _enable_prompt_cache() -> None:
    config.set("prompt_cache_enabled", True)


# ─── transport harness (MockTransport — no network) ─────────────────────────


class _Recorder:
    """MockTransport handler that records raw request bodies and replies."""

    def __init__(self, response: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response

    @property
    def body(self) -> bytes:
        return self.requests[-1].content

    @property
    def payload(self) -> dict:
        return json.loads(self.requests[-1].content)


def _openai_response(model: str = OR_ANTHROPIC_MODEL) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"content": "OK", "tool_calls": []}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        },
    )


def _anthropic_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "type": "message",
            "model": AnthropicProvider.DEFAULT_MODEL,
            "content": [{"type": "text", "text": "OK"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
        },
    )


def _anthropic_sse() -> httpx.Response:
    body = (
        "event: message_start\n"
        'data: {"message":{"model":"claude-3-5-sonnet-20241022",'
        '"usage":{"input_tokens":5}}}\n\n'
        "event: content_block_delta\n"
        'data: {"delta":{"type":"text_delta","text":"OK"}}\n\n'
        "event: message_delta\n"
        'data: {"usage":{"output_tokens":3}}\n\n'
    )
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _openai_sse(model: str = OR_ANTHROPIC_MODEL) -> httpx.Response:
    body = (
        f"data: {json.dumps({'model': model, 'choices': [{'delta': {'content': 'OK'}}]})}\n\n"
        "data: [DONE]\n\n"
    )
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _ollama_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "llama3.1",
            "message": {"content": "OK", "tool_calls": []},
            "done": True,
            "prompt_eval_count": 5,
            "eval_count": 3,
        },
    )


async def _drain(generator) -> None:
    async for _ in generator:
        pass


@pytest.fixture
async def make():
    clients: list[httpx.AsyncClient] = []

    def _client(base_url: str, handler) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            base_url=base_url, transport=httpx.MockTransport(handler), timeout=5.0
        )
        clients.append(client)
        return client

    def openrouter(handler, model: str = OR_ANTHROPIC_MODEL) -> OpenRouterProvider:
        provider = OpenRouterProvider(api_key="test-key", model=model)
        provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
        provider.client = _client(OpenRouterProvider.BASE_URL, handler)
        return provider

    def anthropic(handler) -> AnthropicProvider:
        provider = AnthropicProvider(api_key="test-key")
        provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
        provider.client = _client(AnthropicProvider.BASE_URL, handler)
        return provider

    def direct(name: str, handler) -> DirectProvider:
        spec = PROVIDER_SPECS[name]
        provider = DirectProvider(spec, api_key="test-key")
        provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
        provider.client = _client(spec.base_url, handler)
        return provider

    def ollama(handler) -> OllamaProvider:
        provider = OllamaProvider(model="llama3.1")
        provider._rate_limiter = AsyncTokenBucket(rate=0, capacity=0)
        provider.client = _client("http://localhost:11434", handler)
        return provider

    yield SimpleNamespace(openrouter=openrouter, anthropic=anthropic, direct=direct, ollama=ollama)

    for client in clients:
        await client.aclose()


# ─── config surface ─────────────────────────────────────────────────────────


def test_prompt_cache_default_is_off():
    from machina.core.config import DEFAULTS, Config

    assert DEFAULTS["prompt_cache_enabled"] is False
    assert Config().prompt_cache_enabled is False
    assert config.get("prompt_cache_enabled") is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        (True, True),
        (False, False),
    ],
)
def test_prompt_cache_bool_validation(raw, expected):
    from machina.core.config import validate_value

    assert validate_value("prompt_cache_enabled", raw) is expected


def test_prompt_cache_rejects_non_boolean_strings():
    from machina.core.config import validate_value

    with pytest.raises(ValueError, match="prompt_cache_enabled"):
        validate_value("prompt_cache_enabled", "banana")


def test_prompt_cache_loads_from_yaml(tmp_path):
    from machina.core.config import Config

    path = tmp_path / "config.yaml"
    path.write_text("prompt_cache_enabled: true\n", encoding="utf-8")
    loaded = Config.load(path)
    assert loaded.prompt_cache_enabled is True
    assert loaded.get("prompt_cache_enabled") is True


def test_prompt_cache_config_set_coerces_to_bool():
    config.set("prompt_cache_enabled", "on")
    assert config.prompt_cache_enabled is True
    assert config.get("prompt_cache_enabled") is True
    config.set("prompt_cache_enabled", "off")
    assert config.prompt_cache_enabled is False


def test_prompt_cache_is_registered_in_config_surface():
    from machina.core.config import DEFAULTS, LEGACY_ENV_VARS, SECRET_KEYS, Config

    assert "prompt_cache_enabled" in DEFAULTS
    assert "prompt_cache_enabled" not in SECRET_KEYS
    # No legacy env alias: MACHINA_PROMPT_CACHE_ENABLED is the explicit name.
    assert "prompt_cache_enabled" not in LEGACY_ENV_VARS
    assert isinstance(Config().prompt_cache_enabled, bool)


# ─── OFF path: byte-identical wire payloads ────────────────────────────────


async def test_off_anthropic_complete_wire_payload_frozen(make):
    recorder = _Recorder(_anthropic_response())
    provider = make.anthropic(recorder)

    await provider.complete(MESSAGES, tools=TOOLS)

    assert recorder.body == _wire(GOLDEN_ANTHROPIC_COMPLETE)
    assert recorder.payload == GOLDEN_ANTHROPIC_COMPLETE
    assert list(recorder.payload) == list(GOLDEN_ANTHROPIC_COMPLETE)


async def test_off_anthropic_stream_wire_payload_frozen(make):
    recorder = _Recorder(_anthropic_sse())
    provider = make.anthropic(recorder)

    await _drain(provider.stream_complete(MESSAGES, tools=TOOLS))

    assert recorder.body == _wire(GOLDEN_ANTHROPIC_STREAM)
    assert recorder.payload == GOLDEN_ANTHROPIC_STREAM


async def test_off_openrouter_anthropic_complete_wire_payload_frozen(make):
    recorder = _Recorder(_openai_response())
    provider = make.openrouter(recorder)

    await provider.complete(MESSAGES, tools=TOOLS)

    assert recorder.body == _wire(GOLDEN_OR_COMPLETE)
    assert recorder.payload == GOLDEN_OR_COMPLETE


async def test_off_openrouter_anthropic_stream_wire_payload_frozen(make):
    recorder = _Recorder(_openai_sse())
    provider = make.openrouter(recorder)

    await _drain(provider.stream_complete(MESSAGES, tools=TOOLS))

    assert recorder.body == _wire(GOLDEN_OR_STREAM)
    assert recorder.payload == GOLDEN_OR_STREAM


async def test_off_openrouter_other_model_wire_payload_frozen(make):
    recorder = _Recorder(_openai_response(OR_OTHER_MODEL))
    provider = make.openrouter(recorder, model=OR_OTHER_MODEL)

    await provider.complete(MESSAGES, tools=TOOLS)

    assert recorder.body == _wire(GOLDEN_OR_OTHER_MODEL)


async def test_off_direct_and_ollama_wire_payloads_frozen(make):
    direct_recorder = _Recorder(_openai_response("gpt-4o-mini"))
    direct = make.direct("openai", direct_recorder)
    await direct.complete(MESSAGES, tools=TOOLS)
    assert direct_recorder.body == _wire(GOLDEN_DIRECT_COMPLETE)

    ollama_recorder = _Recorder(_ollama_response())
    ollama = make.ollama(ollama_recorder)
    await ollama.complete(MESSAGES, tools=TOOLS)
    assert ollama_recorder.body == _wire(GOLDEN_OLLAMA_COMPLETE)


# ─── ON path: AnthropicProvider ─────────────────────────────────────────────


async def test_enabled_anthropic_marks_system_tail_and_last_tool_only(make):
    _enable_prompt_cache()
    recorder = _Recorder(_anthropic_response())
    provider = make.anthropic(recorder)

    await provider.complete(MESSAGES, tools=TOOLS)

    payload = recorder.payload
    assert payload["system"] == [{"type": "text", "text": SYSTEM_TEXT, "cache_control": EPHEMERAL}]
    assert payload["tools"][-1]["cache_control"] == EPHEMERAL
    assert all("cache_control" not in tool for tool in payload["tools"][:-1])
    assert "cache_control" not in json.dumps(payload["messages"])
    assert recorder.body.decode("utf-8").count('"cache_control"') == 2

    # ON = OFF golden + exactly the two markers.
    expected = copy.deepcopy(GOLDEN_ANTHROPIC_COMPLETE)
    expected["system"] = payload["system"]
    expected["tools"][-1] = payload["tools"][-1]
    assert payload == expected


async def test_enabled_anthropic_stream_matches_complete_breakpoints(make):
    _enable_prompt_cache()
    complete_recorder = _Recorder(_anthropic_response())
    stream_recorder = _Recorder(_anthropic_sse())

    await make.anthropic(complete_recorder).complete(MESSAGES, tools=TOOLS)
    await _drain(make.anthropic(stream_recorder).stream_complete(MESSAGES, tools=TOOLS))

    complete_payload = complete_recorder.payload
    stream_payload = stream_recorder.payload
    assert stream_payload["stream"] is True
    assert stream_payload["system"] == complete_payload["system"]
    assert stream_payload["tools"] == complete_payload["tools"]
    assert stream_recorder.body.decode("utf-8").count('"cache_control"') == 2


async def test_enabled_anthropic_without_system_marks_nothing(make):
    _enable_prompt_cache()
    recorder = _Recorder(_anthropic_response())
    provider = make.anthropic(recorder)

    await provider.complete([{"role": "user", "content": "hi"}])

    payload = recorder.payload
    assert "system" not in payload
    assert "tools" not in payload
    assert "cache_control" not in recorder.body.decode("utf-8")


async def test_enabled_anthropic_system_only_marks_system(make):
    _enable_prompt_cache()
    recorder = _Recorder(_anthropic_response())
    provider = make.anthropic(recorder)

    await provider.complete(MESSAGES)

    payload = recorder.payload
    assert payload["system"][0]["cache_control"] == EPHEMERAL
    assert "tools" not in payload
    assert recorder.body.decode("utf-8").count('"cache_control"') == 1


# ─── ON path: OpenRouter (Anthropic-routed only) ────────────────────────────


async def test_enabled_openrouter_anthropic_marks_system_tail_and_last_tool_only(make):
    _enable_prompt_cache()
    recorder = _Recorder(_openai_response())
    provider = make.openrouter(recorder)

    await provider.complete(MESSAGES, tools=TOOLS)

    payload = recorder.payload
    assert payload["messages"][0] == {
        "role": "system",
        "content": [{"type": "text", "text": SYSTEM_TEXT, "cache_control": EPHEMERAL}],
    }
    assert payload["messages"][1] == {"role": "user", "content": "hello"}
    assert payload["tools"][-1] == {**_OR_TOOLS[-1], "cache_control": EPHEMERAL}
    assert "cache_control" not in payload["tools"][0]
    assert recorder.body.decode("utf-8").count('"cache_control"') == 2

    # ON = OFF golden + exactly the system tail and last-tool markers.
    expected = copy.deepcopy(GOLDEN_OR_COMPLETE)
    expected["messages"][0] = payload["messages"][0]
    expected["tools"][-1] = payload["tools"][-1]
    assert payload == expected


async def test_enabled_openrouter_stream_matches_complete_breakpoints(make):
    _enable_prompt_cache()
    complete_recorder = _Recorder(_openai_response())
    stream_recorder = _Recorder(_openai_sse())

    await make.openrouter(complete_recorder).complete(MESSAGES, tools=TOOLS)
    await _drain(make.openrouter(stream_recorder).stream_complete(MESSAGES, tools=TOOLS))

    complete_payload = complete_recorder.payload
    stream_payload = stream_recorder.payload
    assert stream_payload["stream"] is True
    assert stream_payload["messages"] == complete_payload["messages"]
    assert stream_payload["tools"] == complete_payload["tools"]
    assert stream_recorder.body.decode("utf-8").count('"cache_control"') == 2


async def test_enabled_openrouter_non_anthropic_model_untouched(make):
    _enable_prompt_cache()
    recorder = _Recorder(_openai_response(OR_OTHER_MODEL))
    provider = make.openrouter(recorder, model=OR_OTHER_MODEL)

    await provider.complete(MESSAGES, tools=TOOLS)

    assert recorder.body == _wire(GOLDEN_OR_OTHER_MODEL)


async def test_enabled_openrouter_without_system_still_marks_last_tool(make):
    _enable_prompt_cache()
    recorder = _Recorder(_openai_response())
    provider = make.openrouter(recorder)

    await provider.complete([{"role": "user", "content": "hi"}], tools=TOOLS)

    payload = recorder.payload
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["tools"][-1]["cache_control"] == EPHEMERAL
    assert recorder.body.decode("utf-8").count('"cache_control"') == 1


async def test_enabled_openrouter_does_not_mutate_caller_messages(make):
    _enable_prompt_cache()
    before = copy.deepcopy(MESSAGES)
    recorder = _Recorder(_openai_response())

    await make.openrouter(recorder).complete(MESSAGES, tools=TOOLS)

    assert MESSAGES == before
    assert isinstance(MESSAGES[0]["content"], str)


# ─── no-op providers and model gating ───────────────────────────────────────


async def test_enabled_direct_provider_untouched(make):
    _enable_prompt_cache()
    recorder = _Recorder(_openai_response("gpt-4o-mini"))
    provider = make.direct("openai", recorder)

    await provider.complete(MESSAGES, tools=TOOLS)

    assert recorder.body == _wire(GOLDEN_DIRECT_COMPLETE)


async def test_enabled_ollama_untouched(make):
    _enable_prompt_cache()
    recorder = _Recorder(_ollama_response())
    provider = make.ollama(recorder)

    await provider.complete(MESSAGES, tools=TOOLS)

    assert recorder.body == _wire(GOLDEN_OLLAMA_COMPLETE)


def test_openrouter_anthropic_model_gate_is_case_insensitive():
    from machina.core.provider.transport import _openrouter_anthropic_model

    assert _openrouter_anthropic_model("anthropic/claude-sonnet-4")
    assert _openrouter_anthropic_model("ANTHROPIC/claude-opus-4")
    assert not _openrouter_anthropic_model("openai/gpt-4o")
    assert not _openrouter_anthropic_model("google/gemini-2.0-flash")
    assert not _openrouter_anthropic_model("")


# ─── logging and invalid-config degradation ─────────────────────────────────


async def test_enabled_attachment_logs_debug_expectation(make, caplog):
    _enable_prompt_cache()
    recorder = _Recorder(_anthropic_response())
    provider = make.anthropic(recorder)

    with caplog.at_level(logging.DEBUG, logger="machina.core.provider"):
        await provider.complete(MESSAGES, tools=TOOLS)

    attached = [
        record.message
        for record in caplog.records
        if record.name == "machina.core.provider"
        and "prompt cache breakpoints attached" in record.message
    ]
    assert attached, "attaching breakpoints must be visible at debug level"
    assert "unmeasured" in attached[0]
    assert "minimum" in attached[0]


async def test_off_attachment_logs_nothing(make, caplog):
    recorder = _Recorder(_anthropic_response())
    provider = make.anthropic(recorder)

    with caplog.at_level(logging.DEBUG, logger="machina.core.provider"):
        await provider.complete(MESSAGES, tools=TOOLS)

    assert not [
        record for record in caplog.records if "prompt cache breakpoints attached" in record.message
    ]


async def test_invalid_prompt_cache_config_degrades_to_off(make, monkeypatch, caplog):
    real_get = config.get

    def broken_get(key, *args, **kwargs):
        if key == "prompt_cache_enabled":
            raise ValueError("prompt_cache_enabled must be a boolean, got 'banana'")
        return real_get(key, *args, **kwargs)

    monkeypatch.setattr(config, "get", broken_get)
    recorder = _Recorder(_openai_response())
    provider = make.openrouter(recorder)

    with caplog.at_level(logging.WARNING, logger="machina.core.provider"):
        await provider.complete(MESSAGES, tools=TOOLS)

    # The request still goes out, unmarked, and the bad value is not silent.
    assert recorder.body == _wire(GOLDEN_OR_COMPLETE)
    assert any(
        "invalid prompt_cache_enabled" in record.message
        for record in caplog.records
        if record.name == "machina.core.provider"
    )
