"""GitHub Copilot subscription provider — the Copilot API, no API key required.

Speaks the OpenAI-compatible endpoint the Copilot CLI / VS Code use against
``https://api.githubcopilot.com`` with the user's GitHub token (see
:mod:`machina.security.copilot_auth`). Wire facts confirmed live on
2026-10-14 (see ``docs/verification/proofs/oauth_research_2026-10-14/``):

* The API accepts the GitHub OAuth token directly as
  ``Authorization: Bearer <gho_…>``; the legacy
  ``api.github.com/copilot_internal/v2/token`` exchange answers HTTP 403 for
  non-first-party clients and is not used.
* Requests self-identify with ``Copilot-Integration-Id``, ``Editor-Version``,
  ``User-Agent``, ``Openai-Intent`` and ``x-initiator`` (the same headers VS
  Code's Copilot Chat sends); ``Accept: text/event-stream`` selects SSE.
* The model menu is per-account: ``GET /models`` lists what the seat serves
  and an unserved id fails with a typed ``copilot_model_not_supported`` error
  naming the fix. Model availability is checked live, not hardcoded.
* A clean EOF without ``[DONE]`` or a ``finish_reason`` raises the typed
  truncated-stream error (same discipline as the other transports).

Credential resolution order: ``~/.machina/auth/copilot.json`` -> import from
``gh auth token`` (read-only; the gh store is never modified) ->
:class:`CopilotAuthError` with the login hint. There is no refresh: the
GitHub token is long-lived and a revoked one surfaces as a typed 401.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from machina import __version__
from machina.core.exceptions import ProviderError, RateLimitError, ValidationError
from machina.core.metrics import metrics
from machina.core.models import LLMResponse, StreamEvent, ToolDefinition
from machina.core.provider._log import logger
from machina.core.provider.audit import audit_log
from machina.core.provider.base import LLMProvider
from machina.core.provider.rate_limit import RateLimitBinding
from machina.core.provider.sse import sse_data
from machina.core.provider.transport import (
    _truncated_stream_error,
    _validate_messages,
    _validate_params,
)
from machina.core.usage import _estimate_tokens, usage_store
from machina.security import copilot_auth

_PROVIDER_NAME = "copilot"
_DEFAULT_BASE_URL = "https://api.githubcopilot.com"
# The VS Code Copilot Chat identity; probed live on 2026-10-14.
_UA = "GitHubCopilotChat/0.26.7"
_EDITOR_VERSION = "vscode/1.104.1"
_INTEGRATION_ID = "vscode-chat"


def _rate_limit_error(model: str) -> RateLimitError:
    """Copilot seat quota exhausted (429). Credentials stay valid."""
    return RateLimitError(
        f"Copilot usage limit reached (HTTP 429) for {model}; the GitHub credential is "
        "still valid. Retry after the usage window resets.",
        reset_at=None,
    )


class _ToolCallAccumulator:
    """Accumulate OpenAI streaming tool-call deltas, indexed like the wire."""

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}
        self._order: list[int] = []

    def add(self, index: int, delta: dict[str, Any]) -> None:
        entry = self._calls.get(index)
        if entry is None:
            entry = {"id": "", "name": "", "arguments": ""}
            self._calls[index] = entry
            self._order.append(index)
        call_id = delta.get("id")
        if isinstance(call_id, str) and call_id:
            entry["id"] = call_id
        function = delta.get("function") if isinstance(delta.get("function"), dict) else {}
        name = function.get("name")
        if isinstance(name, str) and name:
            entry["name"] = name
        arguments = function.get("arguments")
        if isinstance(arguments, str) and arguments:
            entry["arguments"] += arguments

    def result(self) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for index in self._order:
            entry = self._calls[index]
            if not entry["name"]:
                continue
            calls.append(
                {
                    "id": entry["id"] or f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": entry["name"],
                        "arguments": entry["arguments"] or "{}",
                    },
                }
            )
        return calls


def _tools_payload(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


class CopilotProvider(LLMProvider, RateLimitBinding):
    """OpenAI-compatible Copilot API over the user's Copilot subscription."""

    DEFAULT_MODEL = "gpt-4o"

    def __init__(
        self,
        model: str | None = None,
        reasoning_effort: str | None = None,
        *,
        auth_path: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = self._normalize_model(model) or self.DEFAULT_MODEL
        self._default_effort = str(reasoning_effort or "").strip().lower()
        self._rate_limit_key = _PROVIDER_NAME
        self._auth_path = auth_path
        base = (
            base_url
            or os.environ.get("MACHINA_COPILOT_BASE_URL", "").strip().rstrip("/")
            or _DEFAULT_BASE_URL
        )
        self.client = httpx.AsyncClient(
            base_url=base,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": _UA,
                "Copilot-Integration-Id": _INTEGRATION_ID,
                "Editor-Version": _EDITOR_VERSION,
                "Openai-Intent": "conversation-edits",
                "x-initiator": "agent",
            },
            transport=transport,
            timeout=httpx.Timeout(300.0, connect=15.0),
        )

    @staticmethod
    def _normalize_model(model: str | None) -> str:
        """Accept bare model ids and ``copilot/``-prefixed ids."""
        name = str(model or "").strip()
        if name.lower().startswith("copilot/"):
            return name[len("copilot/") :]
        return name

    async def _resolve_tokens(self) -> dict[str, Any]:
        return await asyncio.to_thread(copilot_auth.resolve_tokens, path=self._auth_path)

    def _headers(self, tokens: dict[str, Any]) -> dict[str, str]:
        return {"Authorization": f"Bearer {tokens['token']}"}

    def _payload(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: list[ToolDefinition] | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if tools:
            payload["tools"] = _tools_payload(tools)
            payload["tool_choice"] = "auto"
        return payload

    def _model_not_supported(self, body: str, model: str) -> ProviderError:
        return ProviderError(
            f"copilot model {model!r} is not served for this GitHub account "
            f"(HTTP 400 model_not_supported). Pick a model the seat serves "
            f"(the account's menu is on GET /models): {body[:200]}"
        )

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
        """Stream a Copilot chat completion as Machina StreamEvents."""
        del reasoning_effort  # the Copilot API exposes no effort knob
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)
        await self._rate_limiter.acquire()

        resolved_model = model or self.model
        resolved_model = self._normalize_model(resolved_model)
        if not resolved_model:
            raise ValidationError("copilot provider requires a model id")
        payload = self._payload(messages, resolved_model, temperature, max_tokens, tools, True)
        audit_log(
            "llm_stream_start",
            provider=_PROVIDER_NAME,
            model=resolved_model,
            message_count=len(messages),
        )

        reservation_id = None
        if session_id is not None and agent_id is not None:
            reservation_id = await usage_store.reserve(
                session_id,
                agent_id,
                _PROVIDER_NAME,
                resolved_model,
                messages,
                tools or [],
                max_tokens,
            )

        content_parts: list[str] = []
        accumulator = _ToolCallAccumulator()
        stream_usage: dict[str, Any] = {}
        saw_terminal = False
        start = time.monotonic()
        try:
            tokens = await self._resolve_tokens()
            async with self.client.stream(
                "POST", "/chat/completions", json=payload, headers=self._headers(tokens)
            ) as response:
                if response.status_code == 401:
                    await response.aread()
                    raise copilot_auth.CopilotAuthError(
                        f"Copilot rejected the stored GitHub credential (HTTP 401) for "
                        f"{resolved_model}; {copilot_auth.LOGIN_HINT}",
                        code="copilot_unauthorized",
                    )
                if response.status_code == 403:
                    body = (await response.aread()).decode("utf-8", "replace")[:300]
                    raise ProviderError(
                        f"copilot provider HTTP 403 for {resolved_model}: the GitHub "
                        f"account may not have a Copilot seat ({body})"
                    )
                if response.status_code == 429:
                    await response.aread()
                    raise _rate_limit_error(resolved_model)
                if response.status_code == 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:400]
                    if "model_not_supported" in body or "not supported" in body:
                        raise self._model_not_supported(body, resolved_model)
                    raise ProviderError(f"copilot provider HTTP 400 for {resolved_model}: {body}")
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", "replace")[:300]
                    raise ProviderError(
                        f"copilot provider HTTP {response.status_code} for {resolved_model}: {body}"
                    )
                async for line in response.aiter_lines():
                    data = sse_data(line)
                    if data is None:
                        continue
                    data = data.strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        saw_terminal = True
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    usage = chunk.get("usage")
                    if isinstance(usage, dict):
                        stream_usage = usage
                    choices = chunk.get("choices")
                    choice = (
                        choices[0]
                        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
                        else None
                    )
                    if choice is None:
                        continue
                    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        content_parts.append(text)
                        yield StreamEvent(type="text", content=text)
                    for call in delta.get("tool_calls") or []:
                        if isinstance(call, dict):
                            index = call.get("index")
                            accumulator.add(
                                index if isinstance(index, int) else 0,
                                call,
                            )
                    finish = choice.get("finish_reason")
                    if isinstance(finish, str) and finish:
                        saw_terminal = True
            if not saw_terminal:
                raise _truncated_stream_error(_PROVIDER_NAME, resolved_model)
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_llm_call(
                session_id="", model=resolved_model, duration_ms=duration_ms, is_error=True
            )
            if reservation_id is not None:
                await usage_store.finish(reservation_id, failed=True)
            audit_log(
                "llm_stream_error",
                provider=_PROVIDER_NAME,
                model=resolved_model,
                error=str(exc),
            )
            raise

        tool_calls = accumulator.result()
        full_content = "".join(content_parts)
        usage = {
            "prompt_tokens": int(stream_usage.get("prompt_tokens") or 0),
            "completion_tokens": int(stream_usage.get("completion_tokens") or 0),
            "total_tokens": int(stream_usage.get("total_tokens") or 0),
        }
        if not usage["total_tokens"]:
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        if not usage["prompt_tokens"] and not usage["completion_tokens"]:
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
        duration_ms = (time.monotonic() - start) * 1000
        metrics.record_llm_call(
            session_id="",
            model=resolved_model,
            duration_ms=duration_ms,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
        )
        if reservation_id is not None:
            await usage_store.finish(reservation_id, usage, model=resolved_model)
        audit_log(
            "llm_stream_complete",
            provider=_PROVIDER_NAME,
            model=resolved_model,
            tool_calls=len(tool_calls),
        )
        if logger.isEnabledFor(20):  # DEBUG — stable fields only, never token values.
            logger.debug(
                "copilot stream complete model=%s tool_calls=%d", resolved_model, len(tool_calls)
            )
        yield StreamEvent(
            type="done",
            response=LLMResponse(
                content=full_content,
                model=resolved_model,
                usage=usage,
                tool_calls=tool_calls,
                raw={"provider": _PROVIDER_NAME, "version": __version__},
            ),
        )

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
        """Non-streaming call: drain the stream and return the terminal response."""
        response: LLMResponse | None = None
        async for event in self.stream_complete(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            reasoning_effort=reasoning_effort,
            session_id=session_id,
            agent_id=agent_id,
        ):
            if event.type == "done" and isinstance(event.response, LLMResponse):
                response = event.response
        if response is None:
            raise ProviderError("copilot stream produced no terminal response")
        return response

    async def close(self) -> None:
        await self.client.aclose()
