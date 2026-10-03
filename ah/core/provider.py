"""LLM provider abstraction — OpenRouter, Ollama, OpenAI-compatible."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from ah.core.config import config
from ah.core.exceptions import ValidationError
from ah.core.metrics import metrics
from ah.core.models import (  # noqa: F401 — re-exported for backward compat
    LLMResponse,
    StreamEvent,
    ToolDefinition,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------
_audit_logger = logging.getLogger("ah.audit")
_audit_logger.setLevel(logging.INFO)
if not _audit_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [AUDIT] %(message)s"))
    _audit_logger.addHandler(_handler)


def _sanitize_value(value: Any) -> Any:
    """Sanitize a value for audit logging — redact potential secrets."""
    if isinstance(value, str):
        sanitized = value
        sanitized = re.sub(r"sk-[a-zA-Z0-9]{20,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(r"sk-ant-[a-zA-Z0-9\-_]{20,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(r"sk-or-[a-zA-Z0-9]{20,}", "[REDACTED_KEY]", sanitized)
        sanitized = re.sub(
            r"(Bearer\s+)[a-zA-Z0-9\-._~+/]+=*",
            r"\1[REDACTED_TOKEN]",
            sanitized,
            flags=re.IGNORECASE,
        )
        sanitized = re.sub(
            r"(password|passwd|pwd)\s*[=:]\s*['\"]?([^\s'\"]{4,})['\"]?",
            r"\1=[REDACTED_PASSWORD]",
            sanitized,
            flags=re.IGNORECASE,
        )
        sanitized = re.sub(
            r"(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://([^:]+):([^@]+)@",
            r"\1://\2:[REDACTED_DB_PASSWORD]@",
            sanitized,
        )
        sanitized = re.sub(r"AKIA[0-9A-Z]{16}", "[REDACTED_AWS_KEY]", sanitized)
        sanitized = re.sub(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            "[REDACTED_PRIVATE_KEY]",
            sanitized,
            flags=re.DOTALL,
        )
        return sanitized
    elif isinstance(value, dict):
        return {k: _sanitize_value(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    elif isinstance(value, tuple):
        return tuple(_sanitize_value(v) for v in value)
    else:
        return value


def audit_log(event_type: str, **kwargs) -> None:
    """Log a security-relevant event as JSON. All kwargs are sanitized to redact secrets."""
    sanitized_kwargs = {k: _sanitize_value(v) for k, v in kwargs.items()}
    entry = {
        "timestamp": time.time(),
        "event": event_type,
        **sanitized_kwargs,
    }
    _audit_logger.info(json.dumps(entry, default=str))


# ---------------------------------------------------------------------------
# Rate limiting — async token bucket
# ---------------------------------------------------------------------------
class AsyncTokenBucket:
    """Async token bucket rate limiter.

    Configurable via LLM_RATE_LIMIT_CALLS_PER_MINUTE env var (default: 10).
    """

    def __init__(self, rate: float, capacity: int) -> None:
        self.rate = rate  # tokens per second
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> None:
        """Acquire tokens, waiting if necessary."""
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_refill = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                wait_time = (tokens - self.tokens) / self.rate
            await asyncio.sleep(wait_time)


def _get_rate_limiter() -> AsyncTokenBucket:
    """Build a rate limiter from configuration."""
    calls_per_minute = int(config.get("rate_limit_calls_per_minute"))
    return AsyncTokenBucket(rate=calls_per_minute / 60.0, capacity=calls_per_minute)


# ---------------------------------------------------------------------------
# Data classes — re-exported from ah.core.models for backward compatibility
# ---------------------------------------------------------------------------
# LLMResponse, StreamEvent, ToolDefinition are now defined in ah.core.models


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------
def _validate_messages(messages: list[dict[str, str]]) -> None:
    """Validate LLM message format."""
    if not isinstance(messages, list) or not messages:
        raise ValidationError("messages must be a non-empty list")
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise ValidationError(f"Message at index {i} must be a dict")
        if "role" not in msg:
            raise ValidationError(f"Message at index {i} missing 'role' field")
        if "content" not in msg:
            raise ValidationError(f"Message at index {i} missing 'content' field")


def _validate_params(temperature: float, max_tokens: int) -> None:
    """Validate LLM call parameters."""
    if not (0.0 <= temperature <= 2.0):
        raise ValidationError(f"temperature must be between 0.0 and 2.0, got {temperature}")
    if not (1 <= max_tokens <= 32768):
        raise ValidationError(f"max_tokens must be between 1 and 32768, got {max_tokens}")


def _tools_payload(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """Convert tool definitions to the OpenAI-compatible function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


# ---------------------------------------------------------------------------
# Base provider
# ---------------------------------------------------------------------------
class LLMProvider:
    """Base provider interface."""

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
    ) -> LLMResponse:
        raise NotImplementedError

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream completion — yields StreamEvent objects.

        Yields StreamEvent(type="text", content=...) for each text delta,
        then StreamEvent(type="done", response=LLMResponse) at the end.
        """
        raise NotImplementedError

    async def embed(self, text: str) -> list[float]:
        """Embed a text string into a vector."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# OpenRouter provider
# ---------------------------------------------------------------------------
class OpenRouterProvider(LLMProvider):
    """OpenRouter API — OpenAI-compatible, multi-model."""

    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self, api_key: str | None = None, model: str = "anthropic/claude-3.5-sonnet"
    ) -> None:
        self.api_key = api_key or config.get("openrouter_api_key") or ""
        if not self.api_key:
            raise ValidationError("OPENROUTER_API_KEY not set")
        self.model = model
        self._rate_limiter = _get_rate_limiter()
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/kiazh/agent-harness",
            },
            timeout=120.0,
        )

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
    ) -> LLMResponse:
        # Input validation
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)

        # Rate limiting
        await self._rate_limiter.acquire()

        model = model or self.model
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        audit_log("llm_call_start", provider="openrouter", model=model, message_count=len(messages))

        start = time.monotonic()
        try:
            resp = await self.client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_llm_call(
                session_id="",
                model=model,
                duration_ms=duration_ms,
                is_error=True,
            )
            audit_log("llm_call_error", provider="openrouter", model=model, error=str(e))
            raise

        duration_ms = (time.monotonic() - start) * 1000
        choice = data["choices"][0]
        message = choice["message"]
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

        audit_log(
            "llm_call_complete",
            provider="openrouter",
            model=data.get("model", model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            tool_calls=len(tool_calls),
        )

        return LLMResponse(
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

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream completion via SSE — yields text deltas as they arrive."""
        # Input validation
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)

        # Rate limiting
        await self._rate_limiter.acquire()

        model = model or self.model
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            # Ask for a final usage chunk so streamed turns report real token counts
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        audit_log(
            "llm_stream_start", provider="openrouter", model=model, message_count=len(messages)
        )

        content_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, str]] = {}
        stream_usage: dict[str, int] = {}

        try:
            async with self.client.stream("POST", "/chat/completions", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # The usage chunk arrives with an empty choices list.
                    if chunk.get("usage"):
                        stream_usage = chunk["usage"]
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})

                    # Text content
                    content = delta.get("content", "")
                    if content:
                        content_parts.append(content)
                        yield StreamEvent(type="text", content=content)

                    # Tool calls — accumulate deltas by index
                    tc_deltas = delta.get("tool_calls", [])
                    for tc_delta in tc_deltas:
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
                            tc["arguments"] += func["arguments"]
        except Exception as e:
            audit_log("llm_stream_error", provider="openrouter", model=model, error=str(e))
            raise

        # Build final tool calls list
        tool_calls: list[dict] = []
        for idx in sorted(tool_calls_by_index.keys()):
            tc = tool_calls_by_index[idx]
            tool_calls.append(
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                    },
                }
            )

        full_content = "".join(content_parts)
        usage = {
            "prompt_tokens": stream_usage.get("prompt_tokens", 0),
            "completion_tokens": stream_usage.get("completion_tokens", 0),
            "total_tokens": stream_usage.get("total_tokens", 0),
        }

        audit_log(
            "llm_stream_complete", provider="openrouter", model=model, tool_calls=len(tool_calls)
        )

        yield StreamEvent(
            type="done",
            response=LLMResponse(
                content=full_content,
                model=model,
                usage=usage,
                tool_calls=tool_calls,
            ),
        )

    async def embed(self, text: str) -> list[float]:
        """Embed using OpenRouter's embedding endpoint."""
        resp = await self.client.post(
            "/embeddings",
            json={"model": "text-embedding-3-small", "input": text},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"][0]["embedding"]

    async def close(self) -> None:
        # Close the shared client (it will be recreated if needed)
        await self.client.aclose()


# ---------------------------------------------------------------------------
# Ollama provider
# ---------------------------------------------------------------------------
class OllamaProvider(LLMProvider):
    """Local Ollama provider — free, self-hosted."""

    def __init__(self, model: str = "llama3.1", base_url: str = "http://localhost:11434") -> None:
        self.model = model
        self.base_url = base_url
        self._rate_limiter = _get_rate_limiter()
        self.client = httpx.AsyncClient(base_url=base_url, timeout=120.0)

    async def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
    ) -> LLMResponse:
        # Input validation
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)

        # Rate limiting
        await self._rate_limiter.acquire()

        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        audit_log(
            "llm_call_start",
            provider="ollama",
            model=model or self.model,
            message_count=len(messages),
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
            audit_log("llm_call_error", provider="ollama", model=model or self.model, error=str(e))
            raise

        duration_ms = (time.monotonic() - start) * 1000
        message = data["message"]
        content = message.get("content", "")
        tool_calls = message.get("tool_calls", []) or []

        metrics.record_llm_call(
            session_id="",
            model=data.get("model", model or self.model),
            duration_ms=duration_ms,
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
        )

        audit_log(
            "llm_call_complete",
            provider="ollama",
            model=data.get("model", model or self.model),
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            tool_calls=len(tool_calls),
        )

        return LLMResponse(
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

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream completion via newline-delimited JSON."""
        # Input validation
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)

        # Rate limiting
        await self._rate_limiter.acquire()

        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if tools:
            payload["tools"] = _tools_payload(tools)

        audit_log(
            "llm_stream_start",
            provider="ollama",
            model=model or self.model,
            message_count=len(messages),
        )

        content_parts: list[str] = []
        tool_calls: list[dict] = []

        try:
            async with self.client.stream("POST", "/api/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    message = data.get("message", {})
                    content = message.get("content", "")
                    if content:
                        content_parts.append(content)
                        yield StreamEvent(type="text", content=content)

                    # Ollama streaming tool calls
                    tc = message.get("tool_calls", [])
                    if tc:
                        tool_calls.extend(tc)

                    if data.get("done"):
                        break
        except Exception as e:
            audit_log(
                "llm_stream_error", provider="ollama", model=model or self.model, error=str(e)
            )
            raise

        full_content = "".join(content_parts)

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

    async def embed(self, text: str) -> list[float]:
        """Embed using Ollama's embedding endpoint."""
        resp = await self.client.post(
            f"{self.base_url}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": text},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["embedding"]

    async def close(self) -> None:
        # Close the shared client (it will be recreated if needed)
        await self.client.aclose()


def get_provider(
    provider: str = "openrouter",
    model: str | None = None,
) -> LLMProvider:
    """Factory: return configured provider."""
    if provider == "openrouter":
        return OpenRouterProvider(model=model or "anthropic/claude-3.5-sonnet")
    elif provider == "ollama":
        return OllamaProvider(model=model or "llama3.1")
    else:
        raise ValidationError(f"Unknown provider: {provider}")
