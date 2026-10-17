"""Anthropic Messages API transport, normalized to OpenAI-style shapes."""

from __future__ import annotations

import copy
import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from machina.core.config import config
from machina.core.exceptions import ProviderError, ValidationError
from machina.core.images import anthropic_content
from machina.core.metrics import metrics
from machina.core.models import LLMResponse, StreamEvent, ToolDefinition
from machina.core.prompt import PROVIDER_CONTENT_KEY
from machina.core.provider._log import logger
from machina.core.provider.audit import audit_log
from machina.core.provider.base import LLMProvider
from machina.core.provider.caches import _cache_get, _cache_key, _cache_put, _caller_identity
from machina.core.provider.prompt_cache import apply_anthropic_breakpoints, cache_profile
from machina.core.provider.rate_limit import RateLimitBinding
from machina.core.provider.reasoning import (
    ANTHROPIC_THINKING_BUDGETS,
    _resolve_effort,
    normalize_reasoning_effort,
)
from machina.core.provider.sse import sse_data, sse_event
from machina.core.provider.transport import (
    ReservationSettlement,
    _log_prompt_cache_breakpoints,
    _post_with_retry,
    _prompt_cache_breakpoints_enabled,
    _stream_with_retry,
    _validate_messages,
    _validate_params,
)
from machina.core.provider.usage_map import parse_anthropic_usage
from machina.core.usage import usage_store


# ---------------------------------------------------------------------------
# Anthropic provider — native Messages API (Anthropic has no OpenAI-compatible
# endpoint). Requests/responses are normalized to the harness's OpenAI-style
# shapes so the agent loop works unchanged.
# ---------------------------------------------------------------------------
class AnthropicProvider(LLMProvider, RateLimitBinding):
    """Anthropic Messages API — https://api.anthropic.com/v1/messages."""

    BASE_URL = "https://api.anthropic.com"
    API_VERSION = "2023-06-01"
    DEFAULT_MODEL = "claude-3-5-sonnet-20241022"
    # Audit/metrics/rate-limit label; the Claude subscription subclass shares
    # every code path but must not report itself as the API-key provider.
    PROVIDER_NAME = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.api_key = api_key or config.get("anthropic_api_key") or ""
        if not self.api_key:
            raise ValidationError("ANTHROPIC_API_KEY not set")
        self.model = model or self.DEFAULT_MODEL
        self._default_effort = normalize_reasoning_effort(reasoning_effort)
        self._rate_limit_key = self.PROVIDER_NAME
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": self.API_VERSION,
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )

    @staticmethod
    def _to_anthropic_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Split OpenAI-style history into (system, anthropic messages).

        Handles the harness's shapes: plain role/content dicts, assistant
        messages carrying OpenAI ``tool_calls``, and coalesced ``role: tool``
        results.
        """
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            if role == "system":
                system_parts.append(str(msg.get("content", "")))
                continue
            if role == "tool":
                result: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": str(msg.get("content", "")),
                    **({"is_error": True} if msg.get("is_error") else {}),
                }
                prev = out[-1]["content"] if out else None
                if isinstance(prev, list) and prev and prev[0].get("type") == "tool_result":
                    prev.append(result)
                else:
                    out.append({"role": "user", "content": [result]})
                continue
            tool_calls = msg.get("tool_calls")
            if role == "assistant" and tool_calls:
                native = msg.get(PROVIDER_CONTENT_KEY)
                if isinstance(native, list) and native:
                    # Replay the provider-native blocks verbatim. Anthropic
                    # requires the original signed thinking/redacted_thinking
                    # blocks to accompany tool_use on a continuation while
                    # extended thinking is enabled; rebuilding them from the
                    # OpenAI-style fields would drop the signatures.
                    out.append({"role": "assistant", "content": copy.deepcopy(native)})
                    continue
                blocks: list[dict[str, Any]] = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": str(msg.get("content"))})
                for tc in tool_calls:
                    fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                    args = fn.get("arguments", "{}")
                    try:
                        parsed = json.loads(args) if isinstance(args, str) else args
                    except (json.JSONDecodeError, TypeError):
                        parsed = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", "") if isinstance(tc, dict) else "",
                            "name": fn.get("name", ""),
                            "input": parsed if isinstance(parsed, dict) else {},
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
                continue
            out.append({"role": role, "content": anthropic_content(msg.get("content", ""))})
        return ("\n\n".join(system_parts) if system_parts else None, out)

    @staticmethod
    def _from_response_blocks(content: list[dict[str, Any]]) -> tuple[str, list[dict]]:
        """Normalize Anthropic content blocks to (text, OpenAI-style tool_calls)."""
        texts: list[str] = []
        tool_calls: list[dict] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                texts.append(block["text"])
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                )
        return "".join(texts), tool_calls

    @staticmethod
    def _provider_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Deep-copy native Anthropic blocks for verbatim replay.

        Extended thinking requires the original signed ``thinking`` /
        ``redacted_thinking`` blocks on the tool-continuation request, so they
        must survive normalization instead of becoming OpenAI-style shapes.
        """
        return [copy.deepcopy(block) for block in content or [] if isinstance(block, dict)]

    def _thinking_payload(
        self, reasoning_effort: str | None, max_tokens: int, temperature: float
    ) -> tuple[dict[str, Any], int, float]:
        """Return (extra body, effective max_tokens, effective temperature)."""
        effort = _resolve_effort(reasoning_effort, self._default_effort)
        if not effort:
            return {}, max_tokens, temperature
        budget = ANTHROPIC_THINKING_BUDGETS[effort]
        # Thinking requires max_tokens above the budget and temperature 1.
        return (
            {"thinking": {"type": "enabled", "budget_tokens": budget}},
            max(max_tokens, budget + 1024),
            1.0,
        )

    def _payload(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: list[ToolDefinition] | None,
        reasoning_effort: str | None,
        stream: bool,
    ) -> dict[str, Any]:
        system, anthropic_messages = self._to_anthropic_messages(messages)
        cache_breakpoints = _prompt_cache_breakpoints_enabled()
        thinking, max_tokens, temperature = self._thinking_payload(
            reasoning_effort, max_tokens, temperature
        )
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": anthropic_messages,
            **thinking,
        }
        if system and not cache_breakpoints:
            payload["system"] = system
        if temperature != 1.0:
            payload["temperature"] = temperature
        tool_defs = None
        if tools:
            # Two ends of the stable prefix: the last tool schema, and (via
            # the shared helper) the conversation tail so tool continuations
            # reuse the growing prefix. Breakpoints stay capability-gated.
            tool_defs = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
            if not cache_breakpoints:
                payload["tools"] = tool_defs
        if stream:
            payload["stream"] = True
        system_marked = tools_marked = tail_marked = False
        if cache_breakpoints:
            system_marked, tools_marked, tail_marked = apply_anthropic_breakpoints(
                payload,
                system=system,
                messages=anthropic_messages,
                tools=tool_defs,
                profile=cache_profile(self, model),
            )
        _log_prompt_cache_breakpoints(
            self.PROVIDER_NAME,
            model,
            system=system_marked,
            tools=tools_marked,
            tail=tail_marked,
        )
        return payload

    def reservation_max_tokens(self, max_tokens: int, reasoning_effort: str | None = None) -> int:
        return self._thinking_payload(reasoning_effort, max_tokens, 0.7)[1]

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        reasoning_effort: str | None = None,
        session_id: uuid.UUID | None = None,
        agent_id: str | None = None,
    ) -> LLMResponse:
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)
        await self._rate_limiter.acquire()

        model = model or self.model
        payload = self._payload(
            messages, model, temperature, max_tokens, tools, reasoning_effort, False
        )

        ckey = _cache_key(
            messages,
            tools,
            f"anthropic:{model}",
            payload,
            _caller_identity(session_id, agent_id),
        )
        cached = _cache_get(ckey)
        if cached is not None:
            logger.debug("cache hit for anthropic model=%s", model)
            return cached

        audit_log(
            "llm_call_start", provider=self.PROVIDER_NAME, model=model, message_count=len(messages)
        )

        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id,
                agent_id,
                self.PROVIDER_NAME,
                model,
                messages,
                tools or [],
                payload["max_tokens"],
            )
        settlement = ReservationSettlement(reservation_id, usage_store)

        start = time.monotonic()
        try:
            resp = await _post_with_retry(self.client, "/v1/messages", payload)
            resp.raise_for_status()
            data = resp.json()
        except BaseException as e:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_llm_call(
                session_id="", model=model, duration_ms=duration_ms, is_error=True
            )
            await settlement.fail()
            if not isinstance(e, Exception):
                raise
            audit_log("llm_call_error", provider=self.PROVIDER_NAME, model=model, error=str(e))
            raise

        duration_ms = (time.monotonic() - start) * 1000
        if data.get("type") == "error" or "content" not in data:
            error = data.get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else str(error)
            metrics.record_llm_call(
                session_id="", model=model, duration_ms=duration_ms, is_error=True
            )
            await settlement.fail()
            audit_log(
                "llm_call_error", provider=self.PROVIDER_NAME, model=model, error="no_content"
            )
            raise ProviderError(f"Anthropic returned no content for {model}: {detail or data}")

        content, tool_calls = self._from_response_blocks(data.get("content", []))
        usage = parse_anthropic_usage(data.get("usage", {}))
        metrics.record_llm_call(
            session_id="",
            model=data.get("model", model),
            duration_ms=duration_ms,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
        await settlement.settle(usage, model=data.get("model", model))
        audit_log(
            "llm_call_complete",
            provider=self.PROVIDER_NAME,
            model=data.get("model", model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            tool_calls=len(tool_calls),
        )
        native_blocks = self._provider_content(data.get("content", []))
        raw = {**data}
        if native_blocks:
            raw[PROVIDER_CONTENT_KEY] = native_blocks
        response = LLMResponse(
            content=content,
            model=data.get("model", model),
            usage=usage,
            raw=raw,
            tool_calls=tool_calls,
        )
        _cache_put(ckey, response)
        return response

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
        reasoning_effort: str | None = None,
        session_id: uuid.UUID | None = None,
        agent_id: str | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream via Anthropic SSE — yields text deltas as they arrive."""
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)
        await self._rate_limiter.acquire()

        model = model or self.model
        payload = self._payload(
            messages, model, temperature, max_tokens, tools, reasoning_effort, True
        )

        audit_log(
            "llm_stream_start",
            provider=self.PROVIDER_NAME,
            model=model,
            message_count=len(messages),
        )

        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id,
                agent_id,
                self.PROVIDER_NAME,
                model,
                messages,
                tools or [],
                payload["max_tokens"],
            )
        settlement = ReservationSettlement(reservation_id, usage_store)

        text_parts: list[str] = []
        blocks: dict[int, dict[str, Any]] = {}
        native_blocks: dict[int, dict[str, Any]] = {}
        stream_usage: dict[str, Any] = {}
        output_tokens = 0
        resolved_model = model
        saw_terminal = False
        try:
            async with _stream_with_retry(self.client, "/v1/messages", payload) as resp:
                resp.raise_for_status()
                event_type = ""
                async for line in resp.aiter_lines():
                    event = sse_event(line)
                    if event is not None:
                        event_type = event.strip()
                        continue
                    data = sse_data(line)
                    if data is None:
                        continue
                    try:
                        data = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    if event_type == "error" or data.get("type") == "error":
                        # Anthropic can fail mid-stream via an explicit error
                        # event; swallowing it would truncate the answer.
                        error = data.get("error") or {}
                        detail = error.get("message") if isinstance(error, dict) else str(error)
                        raise ProviderError(
                            f"Anthropic stream error for {resolved_model}: {detail or data}"
                        )
                    if event_type == "message_start":
                        msg = data.get("message", {})
                        resolved_model = msg.get("model") or resolved_model
                        start_usage = msg.get("usage", {})
                        if isinstance(start_usage, dict):
                            stream_usage.update(start_usage)
                    elif event_type == "content_block_start":
                        idx = data.get("index", 0)
                        block = data.get("content_block", {})
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        if block_type == "tool_use":
                            blocks[idx] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "arguments": "",
                            }
                            native_blocks[idx] = {
                                "type": "tool_use",
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "_arguments": "",
                            }
                        elif block_type == "text":
                            native_blocks[idx] = {"type": "text", "text": block.get("text", "")}
                        elif block_type == "thinking":
                            native_blocks[idx] = {
                                "type": "thinking",
                                "thinking": block.get("thinking", ""),
                                "signature": block.get("signature", ""),
                            }
                        elif block_type == "redacted_thinking":
                            native_blocks[idx] = copy.deepcopy(block)
                    elif event_type == "content_block_delta":
                        idx = data.get("index", 0)
                        delta = data.get("delta", {})
                        if not isinstance(delta, dict):
                            continue
                        delta_type = delta.get("type")
                        native = native_blocks.get(idx)
                        if delta_type == "text_delta" and delta.get("text"):
                            text_parts.append(delta["text"])
                            yield StreamEvent(type="text", content=delta["text"])
                            if native is not None and native.get("type") == "text":
                                native["text"] += delta["text"]
                        elif delta_type == "thinking_delta" and delta.get("thinking"):
                            if native is not None and native.get("type") == "thinking":
                                native["thinking"] += delta["thinking"]
                        elif delta_type == "signature_delta" and delta.get("signature"):
                            if native is not None and native.get("type") == "thinking":
                                native["signature"] += delta["signature"]
                        elif delta_type == "input_json_delta" and delta.get("partial_json"):
                            blocks.setdefault(idx, {"id": "", "name": "", "arguments": ""})[
                                "arguments"
                            ] += delta["partial_json"]
                            if native is None or native.get("type") != "tool_use":
                                native = {
                                    "type": "tool_use",
                                    "id": "",
                                    "name": "",
                                    "_arguments": "",
                                }
                                native_blocks[idx] = native
                            native["_arguments"] += delta["partial_json"]
                    elif event_type == "message_delta":
                        output_tokens = data.get("usage", {}).get("output_tokens", output_tokens)
                    elif data.get("type") == "message_stop":
                        saw_terminal = True
            if not saw_terminal:
                # A clean EOF without ``message_stop`` is a truncation: the
                # partial text must not be reported as a successful answer.
                raise ProviderError(
                    f"Anthropic stream ended without a terminal message_stop event "
                    f"for {resolved_model}"
                )
        except BaseException as e:
            # GeneratorExit/CancelledError bypass ``except Exception``; settle
            # before re-raising so an early close cannot strand the reservation.
            await settlement.fail()
            if not isinstance(e, Exception):
                raise
            audit_log("llm_stream_error", provider=self.PROVIDER_NAME, model=model, error=str(e))
            raise

        tool_calls = [
            {
                "id": b["id"],
                "type": "function",
                "function": {"name": b["name"], "arguments": b["arguments"]},
            }
            for _, b in sorted(blocks.items())
            if b.get("name") or b.get("arguments")
        ]
        native_content: list[dict[str, Any]] = []
        for _, native in sorted(native_blocks.items()):
            if native.get("type") != "tool_use":
                native_content.append(native)
                continue
            raw_arguments = native.get("_arguments", "")
            parsed_input: Any = {}
            if raw_arguments:
                try:
                    parsed_input = json.loads(raw_arguments)
                except (json.JSONDecodeError, TypeError):
                    parsed_input = {}
            native_content.append(
                {
                    "type": "tool_use",
                    "id": native.get("id", ""),
                    "name": native.get("name", ""),
                    "input": parsed_input if isinstance(parsed_input, dict) else {},
                }
            )
        # message_delta carries the cumulative output count; it wins over any
        # placeholder in message_start.
        stream_usage["output_tokens"] = output_tokens
        usage = parse_anthropic_usage(stream_usage)
        await settlement.settle(usage, model=resolved_model)
        audit_log(
            "llm_stream_complete",
            provider=self.PROVIDER_NAME,
            model=model,
            tool_calls=len(tool_calls),
        )
        raw: dict[str, Any] = {}
        if native_content:
            raw[PROVIDER_CONTENT_KEY] = native_content
        yield StreamEvent(
            type="done",
            response=LLMResponse(
                content="".join(text_parts),
                model=resolved_model,
                usage=usage,
                raw=raw,
                tool_calls=tool_calls,
            ),
        )

    async def close(self) -> None:
        await self.client.aclose()
