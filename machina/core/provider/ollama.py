"""Ollama transport — local NDJSON chat API."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from machina.core.exceptions import ProviderError
from machina.core.images import ollama_messages
from machina.core.metrics import metrics
from machina.core.models import LLMResponse, StreamEvent, ToolDefinition
from machina.core.provider._log import logger
from machina.core.provider.audit import audit_log
from machina.core.provider.base import LLMProvider
from machina.core.provider.caches import _cache_get, _cache_key, _cache_put, _caller_identity
from machina.core.provider.rate_limit import _get_rate_limiter
from machina.core.provider.reasoning import _resolve_effort, normalize_reasoning_effort
from machina.core.provider.transport import (
    _stream_with_retry,
    _tools_payload,
    _validate_messages,
    _validate_params,
)
from machina.core.usage import usage_store


# ---------------------------------------------------------------------------
# Ollama provider
# ---------------------------------------------------------------------------
class OllamaProvider(LLMProvider):
    """Local Ollama provider — free, self-hosted."""

    def __init__(
        self,
        model: str = "llama3.1",
        base_url: str = "http://localhost:11434",
        reasoning_effort: str | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self._default_effort = normalize_reasoning_effort(reasoning_effort)
        self._rate_limiter = _get_rate_limiter("ollama")
        self.client = httpx.AsyncClient(base_url=base_url, timeout=120.0)

    @staticmethod
    def _think_flag(effort: str) -> bool | None:
        """Map effort to Ollama's boolean ``think`` flag (None = omit, model default)."""
        if not effort:
            return None
        return effort != "low"

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
        # Input validation
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)

        # Rate limiting
        await self._rate_limiter.acquire()

        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": ollama_messages(messages),
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        think = self._think_flag(_resolve_effort(reasoning_effort, self._default_effort))
        if think is not None:
            payload["think"] = think

        # Check cache before making any HTTP request
        ckey = _cache_key(
            messages,
            tools,
            f"ollama:{model or self.model}",
            payload,
            _caller_identity(session_id, agent_id),
        )
        cached = _cache_get(ckey)
        if cached is not None:
            logger.debug("cache hit for ollama model=%s", model or self.model)
            return cached

        audit_log(
            "llm_call_start",
            provider="ollama",
            model=model or self.model,
            message_count=len(messages),
        )

        # Reserve usage if session_id and agent_id are provided
        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id,
                agent_id,
                "ollama",
                model or self.model,
                messages,
                tools or [],
                max_tokens,
            )

        start = time.monotonic()
        try:
            resp = await self.client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_llm_call(
                session_id="",
                model=model or self.model,
                duration_ms=duration_ms,
                is_error=True,
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log("llm_call_error", provider="ollama", model=model or self.model, error=str(e))
            raise

        # Ollama can return 200 with an error body (e.g. model not found).
        if isinstance(data, dict) and "error" in data:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_llm_call(
                session_id="",
                model=model or self.model,
                duration_ms=duration_ms,
                is_error=True,
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log(
                "llm_call_error",
                provider="ollama",
                model=model or self.model,
                error=data["error"],
            )
            raise ProviderError(f"Ollama error: {data['error']}")

        duration_ms = (time.monotonic() - start) * 1000
        # A 200 body without a message (e.g. {"done": true}) must fail like the
        # other providers instead of a bare KeyError that strands the usage
        # reservation and bypasses metrics/audit.
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict):
            metrics.record_llm_call(
                session_id="",
                model=model or self.model,
                duration_ms=duration_ms,
                is_error=True,
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log(
                "llm_call_error",
                provider="ollama",
                model=model or self.model,
                error="no_message",
            )
            detail = data if isinstance(data, dict) else "empty response"
            raise ProviderError(f"Ollama returned no message for {model or self.model}: {detail}")

        content = message.get("content", "")
        tool_calls = message.get("tool_calls", []) or []

        metrics.record_llm_call(
            session_id="",
            model=data.get("model", model or self.model),
            duration_ms=duration_ms,
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
        )

        # Finish usage reservation
        if reservation_id is not None:
            await usage_store.finish(
                reservation_id,
                {
                    "prompt_tokens": data.get("prompt_eval_count", 0),
                    "completion_tokens": data.get("eval_count", 0),
                    "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
                },
                model=data.get("model", model or self.model),
            )

        audit_log(
            "llm_call_complete",
            provider="ollama",
            model=data.get("model", model or self.model),
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            tool_calls=len(tool_calls),
        )

        response = LLMResponse(
            content=content,
            model=data.get("model", model or self.model),
            usage={
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
                "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
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
        """Stream completion via newline-delimited JSON."""
        # Input validation
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)

        # Rate limiting
        await self._rate_limiter.acquire()

        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": ollama_messages(messages),
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if tools:
            payload["tools"] = _tools_payload(tools)
        think = self._think_flag(_resolve_effort(reasoning_effort, self._default_effort))
        if think is not None:
            payload["think"] = think

        audit_log(
            "llm_stream_start",
            provider="ollama",
            model=model or self.model,
            message_count=len(messages),
        )

        # Reserve usage if session_id and agent_id are provided
        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id,
                agent_id,
                "ollama",
                model or self.model,
                messages,
                tools or [],
                max_tokens,
            )

        content_parts: list[str] = []
        tool_calls: list[dict] = []
        saw_terminal = False

        try:
            async with _stream_with_retry(self.client, "/api/chat", payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        # Ollama streams NDJSON; stray keep-alive lines are
                        # benign and must not fail the stream.
                        continue
                    if not isinstance(data, dict):
                        continue
                    if data.get("error"):
                        # Explicit error frame: fail honestly instead of
                        # reporting a truncated stream as success.
                        raise ProviderError(
                            f"Ollama stream error for {model or self.model}: {data['error']}"
                        )

                    message = data.get("message", {})
                    content = message.get("content", "")
                    if content:
                        content_parts.append(content)
                        yield StreamEvent(type="text", content=content)

                    # Ollama streaming tool calls
                    tc = message.get("tool_calls", [])
                    if tc:
                        for call in tc:
                            if isinstance(call, dict):
                                fn = call.get("function", {}) or {}
                                args = fn.get("arguments", "")
                                if isinstance(args, dict):
                                    # Normalize structured args to a string so
                                    # downstream json.loads / concat keeps working.
                                    call = {
                                        **call,
                                        "function": {**fn, "arguments": json.dumps(args)},
                                    }
                            tool_calls.append(call)

                    if data.get("done"):
                        saw_terminal = True
                        break
            if not saw_terminal:
                # A clean EOF without a ``done: true`` frame is a truncation:
                # partial text and tool calls must not be reported as a
                # successful completion.
                raise ProviderError(
                    f"Ollama stream ended without a terminal done frame for {model or self.model}"
                )
        except Exception as e:
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log(
                "llm_stream_error", provider="ollama", model=model or self.model, error=str(e)
            )
            raise

        full_content = "".join(content_parts)

        # Finish usage reservation
        if reservation_id is not None:
            await usage_store.finish(
                reservation_id,
                {},
                model=model or self.model,
            )

        audit_log(
            "llm_stream_complete",
            provider="ollama",
            model=model or self.model,
            tool_calls=len(tool_calls),
        )

        yield StreamEvent(
            type="done",
            response=LLMResponse(
                content=full_content,
                model=model or self.model,
                usage={},
                tool_calls=tool_calls,
            ),
        )

    async def close(self) -> None:
        # Close the shared client (it will be recreated if needed)
        await self.client.aclose()
