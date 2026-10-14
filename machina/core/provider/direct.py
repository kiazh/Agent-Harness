"""OpenAI-compatible direct transport for every PROVIDER_SPECS family."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from machina.core.config import config
from machina.core.exceptions import ProviderError, ValidationError
from machina.core.metrics import metrics
from machina.core.models import LLMResponse, StreamEvent, ToolDefinition
from machina.core.provider._log import logger
from machina.core.provider.audit import audit_log
from machina.core.provider.base import LLMProvider
from machina.core.provider.caches import _cache_get, _cache_key, _cache_put, _caller_identity
from machina.core.provider.rate_limit import _get_rate_limiter
from machina.core.provider.reasoning import _resolve_effort, normalize_reasoning_effort
from machina.core.provider.specs import ProviderSpec
from machina.core.provider.transport import (
    _post_with_retry,
    _stream_error_message,
    _stream_with_retry,
    _tools_payload,
    _truncated_stream_error,
    _validate_messages,
    _validate_params,
)
from machina.core.usage import _estimate_tokens, usage_store


# ---------------------------------------------------------------------------
# Direct provider — one transport for every OpenAI-compatible family
# (OpenAI, DeepSeek, xAI, Groq, Together, Mistral, Google). Driven by a
# ProviderSpec so base URL, key, default model and effort mapping stay
# accurate in one table instead of seven copy-pasted classes.
# ---------------------------------------------------------------------------
class DirectProvider(LLMProvider):
    """OpenAI-compatible chat API for a single vendor (see PROVIDER_SPECS)."""

    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.spec = spec
        self.api_key = api_key or config.get(spec.config_key) or ""
        if not self.api_key:
            raise ValidationError(f"{spec.api_key_env} not set")
        self.model = model or spec.default_model
        self._default_effort = normalize_reasoning_effort(reasoning_effort)
        self._rate_limiter = _get_rate_limiter(spec.name)
        self.client = httpx.AsyncClient(
            base_url=spec.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )

    def _effort_payload(self, reasoning_effort: str | None) -> dict[str, Any]:
        """Extra body params for the requested effort ({} when unset/unsupported)."""
        effort = _resolve_effort(reasoning_effort, self._default_effort)
        if effort and self.spec.reasoning == "openai":
            return {"reasoning_effort": effort}
        return {}

    def _cache_key_for(self, messages, tools, model: str, request=None, identity=None) -> str:
        # Namespace by provider so identical model ids on two vendors can't collide.
        return _cache_key(messages, tools, f"{self.spec.name}:{model}", request, identity)

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
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **self._effort_payload(reasoning_effort),
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        ckey = self._cache_key_for(
            messages, tools, model, payload, _caller_identity(session_id, agent_id)
        )
        cached = _cache_get(ckey)
        if cached is not None:
            logger.debug("cache hit for %s model=%s", self.spec.name, model)
            return cached

        audit_log(
            "llm_call_start", provider=self.spec.name, model=model, message_count=len(messages)
        )

        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id, agent_id, self.spec.name, model, messages, tools or [], max_tokens
            )

        start = time.monotonic()
        try:
            resp = await _post_with_retry(self.client, "/chat/completions", payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_llm_call(
                session_id="", model=model, duration_ms=duration_ms, is_error=True
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log("llm_call_error", provider=self.spec.name, model=model, error=str(e))
            raise

        duration_ms = (time.monotonic() - start) * 1000
        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            error = data.get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else str(error)
            message = (
                f"provider returned no choices for {model}: {detail or data or 'empty response'}"
            )
            metrics.record_llm_call(
                session_id="", model=model, duration_ms=duration_ms, is_error=True
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log("llm_call_error", provider=self.spec.name, model=model, error="no_choices")
            raise ProviderError(message)

        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content", "")
        tool_calls = message.get("tool_calls", []) or []
        usage = data.get("usage", {})

        metrics.record_llm_call(
            session_id="",
            model=data.get("model", model),
            duration_ms=duration_ms,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
        if reservation_id is not None:
            await usage_store.finish(reservation_id, usage, model=data.get("model", model))
        audit_log(
            "llm_call_complete",
            provider=self.spec.name,
            model=data.get("model", model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            tool_calls=len(tool_calls),
        )
        response = LLMResponse(
            content=content,
            model=data.get("model", model),
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            raw=data,
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
        """Stream completion via SSE — yields text deltas as they arrive."""
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)
        await self._rate_limiter.acquire()

        model = model or self.model
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            **self._effort_payload(reasoning_effort),
        }
        # stream_options.include_usage is only sent for vendors whose docs
        # confirm it — others 400 on the param.
        if self.spec.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = _tools_payload(tools)

        audit_log(
            "llm_stream_start", provider=self.spec.name, model=model, message_count=len(messages)
        )

        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id, agent_id, self.spec.name, model, messages, tools or [], max_tokens
            )

        content_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, str]] = {}
        stream_usage: dict[str, int] = {}
        resolved_model = model
        saw_terminal = False
        try:
            async with _stream_with_retry(self.client, "/chat/completions", payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        saw_terminal = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("error"):
                        # Explicit error frame: fail the stream honestly
                        # instead of reporting a truncated success.
                        raise ProviderError(_stream_error_message(chunk, resolved_model))
                    resolved_model = chunk.get("model") or resolved_model
                    # The usage chunk arrives with an empty choices list.
                    if chunk.get("usage"):
                        stream_usage = chunk["usage"]
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    if choices[0].get("finish_reason"):
                        saw_terminal = True
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        content_parts.append(content)
                        yield StreamEvent(type="text", content=content)
                    for tc_delta in delta.get("tool_calls", []):
                        idx = tc_delta.get("index", 0)
                        if idx not in tool_calls_by_index:
                            tool_calls_by_index[idx] = {"id": "", "name": "", "arguments": ""}
                        tc = tool_calls_by_index[idx]
                        if tc_delta.get("id"):
                            tc["id"] = tc_delta["id"]
                        func = tc_delta.get("function", {})
                        if func.get("name"):
                            tc["name"] = func["name"]
                        if func.get("arguments"):
                            frag = func["arguments"]
                            if isinstance(frag, dict):
                                frag = json.dumps(frag)
                            tc["arguments"] += frag
            if not saw_terminal:
                raise _truncated_stream_error(self.spec.name, resolved_model)
        except Exception as e:
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log("llm_stream_error", provider=self.spec.name, model=model, error=str(e))
            raise

        tool_calls: list[dict] = []
        for idx in sorted(tool_calls_by_index.keys()):
            tc = tool_calls_by_index[idx]
            tool_calls.append(
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
            )
        full_content = "".join(content_parts)
        usage = {
            "prompt_tokens": int(stream_usage.get("prompt_tokens", 0)),
            "completion_tokens": int(stream_usage.get("completion_tokens", 0)),
        }
        if not usage["prompt_tokens"] and not usage["completion_tokens"]:
            # Estimates support turn accounting and display, but must never
            # release the conservative durable reservation as vendor usage.
            usage = {
                "prompt_tokens": _estimate_tokens(messages, tools or [], 0),
                "completion_tokens": _estimate_tokens(
                    [{"role": "assistant", "content": full_content, "tool_calls": tool_calls}],
                    [],
                    0,
                ),
                "estimated": True,
            }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        if reservation_id is not None:
            await usage_store.finish(reservation_id, usage, model=resolved_model)
        audit_log(
            "llm_stream_complete", provider=self.spec.name, model=model, tool_calls=len(tool_calls)
        )
        yield StreamEvent(
            type="done",
            response=LLMResponse(
                content=full_content, model=resolved_model, usage=usage, tool_calls=tool_calls
            ),
        )

    async def close(self) -> None:
        await self.client.aclose()
