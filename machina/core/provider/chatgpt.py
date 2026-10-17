"""ChatGPT-account provider — the Codex CLI protocol, no API key required.

Speaks the Responses API shape the Codex CLI uses against
``https://chatgpt.com/backend-api/codex/responses`` with a ChatGPT OAuth
access token (see :mod:`machina.security.chatgpt_auth`). Wire facts confirmed
live on 2026-10-14 (see ``docs/verification/proofs/chatgpt_auth_2026-10-14/``):

* Required headers: ``Authorization: Bearer <access_token>``,
  ``Accept: text/event-stream``, ``originator`` (self-identifying), the
  ``ChatGPT-Account-ID`` derived from the access token's JWT, and a
  ``session_id``. No ``OpenAI-Beta`` header is needed for the HTTP SSE route
  (it belongs to the CLI's websocket transport).
* The backend rejects ``temperature`` (HTTP 400 "Unsupported parameter");
  the Codex CLI never sends sampling knobs, so this transport accepts the
  interface's ``temperature``/``max_tokens`` arguments, validates them, and
  does not put them on the wire.
* A successful stream ends with ``response.completed``; a clean EOF without it
  raises the typed truncated-stream error (same discipline as ``direct.py``).
* Model ids are Codex catalog slugs (``gpt-6.1-sol`` is the current
  priority-1 default; ``chatgpt/<slug>`` prefixes are accepted and
  stripped). The catalog is discovered live per account
  (:mod:`machina.core.provider.catalog`): when fresh live data is present an
  unknown slug is rejected with a typed error naming the available models;
  per-call ``reasoning_effort`` is validated against THAT model's catalog
  entry and an unsupported level is rejected with the supported set in the
  message (``ultra`` is catalog-advertised but wire-rejected, so it is never
  offered). Listing surfaces (``/models``, ``machina providers models``)
  refresh live data; the provider itself never makes an extra network call.

Credential resolution order: ``~/.machina/auth/chatgpt.json`` -> import
``~/.codex/auth.json`` (read-only there) -> :class:`ChatGPTAuthError` with the
login hint. A 401 refreshes the token once (peer-adopting a rotation already
in the store) and retries the request a single time.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from machina import __version__
from machina.core.background_tasks import joined_teardown
from machina.core.config import config
from machina.core.exceptions import ProviderError, RateLimitError
from machina.core.metrics import metrics
from machina.core.models import LLMResponse, StreamEvent, ToolDefinition
from machina.core.provider._log import logger
from machina.core.provider.audit import audit_log
from machina.core.provider.base import LLMProvider
from machina.core.provider.catalog import validate_effort, validate_model_slug
from machina.core.provider.rate_limit import RateLimitBinding
from machina.core.provider.sse import sse_data
from machina.core.provider.transport import (
    ReservationSettlement,
    _truncated_stream_error,
    _validate_messages,
    _validate_params,
)
from machina.core.provider.usage_map import parse_chatgpt_usage
from machina.core.usage import _estimate_tokens, usage_store
from machina.security import chatgpt_auth

_PROVIDER_NAME = "chatgpt"
_WIRE_EVENT_TEXT_DELTA = "response.output_text.delta"
_WIRE_EVENT_ERROR = "error"
_WIRE_EVENT_ITEM_ADDED = "response.output_item.added"
_WIRE_EVENT_ITEM_DONE = "response.output_item.done"
_WIRE_EVENT_ARGS_DELTA = "response.function_call_arguments.delta"
_WIRE_EVENT_ARGS_DONE = "response.function_call_arguments.done"
_WIRE_EVENT_COMPLETED = "response.completed"
_WIRE_EVENT_FATAL = ("response.failed", "response.incomplete")


class _FunctionCallAccumulator:
    """Collect streamed ``function_call`` items into OpenAI-style tool calls.

    Codex streams a call across several frames (item added -> argument deltas
    -> arguments.done -> item.done) and may rotate item ids; ``call_id`` is the
    stable key, with item-id and output-index used only as aliases.
    """

    def __init__(self) -> None:
        self._calls: dict[str, dict[str, str]] = {}
        self._order: list[str] = []
        self._by_item: dict[str, str] = {}
        self._by_index: dict[Any, str] = {}

    def _ensure(self, key: str) -> dict[str, str]:
        entry = self._calls.get(key)
        if entry is None:
            entry = {"id": "", "name": "", "arguments": ""}
            self._calls[key] = entry
            self._order.append(key)
        return entry

    def announce(self, item: dict[str, Any], output_index: Any) -> None:
        call_id = str(item.get("call_id") or "")
        item_id = str(item.get("id") or "")
        key = call_id or item_id or f"idx:{output_index}"
        entry = self._ensure(key)
        if call_id:
            entry["id"] = call_id
        if item_id:
            self._by_item[item_id] = key
        if output_index is not None:
            self._by_index[output_index] = key
        name = item.get("name")
        if isinstance(name, str) and name:
            entry["name"] = name

    def _key_from_event(self, event: dict[str, Any]) -> str | None:
        item_id = str(event.get("item_id") or "")
        if item_id and item_id in self._by_item:
            return self._by_item[item_id]
        output_index = event.get("output_index")
        if output_index in self._by_index:
            return self._by_index[output_index]
        if item_id:
            return item_id
        return f"idx:{output_index}" if output_index is not None else None

    def append_delta(self, event: dict[str, Any]) -> None:
        key = self._key_from_event(event)
        delta = event.get("delta")
        if key is not None and isinstance(delta, str) and delta:
            self._ensure(key)["arguments"] += delta

    def set_arguments(self, event: dict[str, Any]) -> None:
        key = self._key_from_event(event)
        if key is None or "arguments" not in event:
            return
        # Authoritative for the accumulated string; explicit "" means zero-arg.
        self._ensure(key)["arguments"] = str(event.get("arguments") or "")

    def settle(self, item: dict[str, Any], output_index: Any) -> None:
        call_id = str(item.get("call_id") or "")
        item_id = str(item.get("id") or "")
        if call_id:
            key = call_id
        elif item_id and item_id in self._by_item:
            key = self._by_item[item_id]
        elif output_index in self._by_index:
            key = self._by_index[output_index]
        elif item_id:
            key = item_id
        else:
            return
        entry = self._ensure(key)
        if call_id:
            entry["id"] = call_id
        name = item.get("name")
        if isinstance(name, str) and name:
            entry["name"] = name
        arguments = item.get("arguments")
        if isinstance(arguments, str) and arguments:
            entry["arguments"] = arguments
        if item_id:
            self._by_item[item_id] = key
        if output_index is not None:
            self._by_index[output_index] = key

    def result(self) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for key in self._order:
            entry = self._calls[key]
            if not entry["name"]:
                continue
            calls.append(
                {
                    "id": entry["id"] or key,
                    "type": "function",
                    "function": {
                        "name": entry["name"],
                        "arguments": entry["arguments"] or "{}",
                    },
                }
            )
        return calls


def _map_usage(usage: dict[str, Any]) -> dict[str, Any]:
    return parse_chatgpt_usage(usage)


def _stream_error(event: dict[str, Any], model: str) -> ProviderError:
    nested = event.get("error") if isinstance(event.get("error"), dict) else {}
    message = event.get("message") or nested.get("message") or "no detail"
    code = event.get("code") or nested.get("code")
    suffix = f" ({code})" if code else ""
    return ProviderError(f"chatgpt stream error for {model}: {message}{suffix}")


def _terminal_failure(event: dict[str, Any], model: str) -> ProviderError:
    response = event.get("response") if isinstance(event.get("response"), dict) else {}
    status = str(response.get("status") or event.get("type") or "failed").removeprefix("response.")
    error = response.get("error") if isinstance(response.get("error"), dict) else {}
    details = (
        response.get("incomplete_details")
        if isinstance(response.get("incomplete_details"), dict)
        else {}
    )
    reason = error.get("message") or details.get("reason") or "no detail"
    return ProviderError(f"chatgpt response {status} for {model}: {reason}")


def _rate_limit_error(response: httpx.Response, model: str) -> RateLimitError:
    """ChatGPT plan quota exhausted (429). Credentials stay valid."""
    reset_at: float | None = None
    raw_reset = (response.headers.get("x-codex-primary-reset-at") or "").strip()
    if raw_reset.isdigit():
        reset_at = float(raw_reset)
    else:
        raw_after = (response.headers.get("x-codex-primary-reset-after-seconds") or "").strip()
        if raw_after.isdigit():
            reset_at = time.time() + float(raw_after)
    return RateLimitError(
        f"ChatGPT usage limit reached (HTTP 429) for {model}; credentials are still valid. "
        "Retry after the usage window resets.",
        reset_at=reset_at,
    )


def _tools_payload(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """Responses-API function tools are flat (no nested ``function`` object)."""
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
            "strict": False,
        }
        for tool in tools
    ]


class ChatGPTProvider(LLMProvider, RateLimitBinding):
    """Codex protocol over the ChatGPT subscription backend."""

    BASE_URL = "https://chatgpt.com/backend-api/codex"
    RESPONSES_PATH = "/responses"
    ORIGINATOR = "machina"
    DEFAULT_MODEL = "gpt-6.1-sol"
    DEFAULT_INSTRUCTIONS = "You are a helpful assistant."

    def __init__(
        self,
        model: str | None = None,
        reasoning_effort: str | None = None,
        *,
        auth_path: str | None = None,
    ) -> None:
        self.model = self._normalize_model(model) or self.DEFAULT_MODEL
        # Stored raw (lowercased); per-model validation against the catalog
        # entry happens when a call resolves its effective model, so a bad
        # level fails with the supported set for THAT model, not a global one.
        self._default_effort = str(reasoning_effort or "").strip().lower()
        self._rate_limit_key = _PROVIDER_NAME
        self._auth_path = auth_path
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            timeout=httpx.Timeout(300.0, connect=15.0),
        )

    @staticmethod
    def _normalize_model(model: str | None) -> str:
        """Accept bare Codex slugs and ``chatgpt/<slug>`` vendor-prefixed ids."""
        name = str(model or "").strip()
        for prefix in ("chatgpt/", "codex/"):
            if name.lower().startswith(prefix):
                return name[len(prefix) :]
        return name

    def _headers(self, tokens: dict[str, Any]) -> dict[str, str]:
        access = str(tokens.get("access_token") or "")
        headers = {
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "originator": self.ORIGINATOR,
            "User-Agent": f"machina/{__version__}",
            "session_id": str(uuid.uuid4()),
        }
        account = str(tokens.get("account_id") or "") or chatgpt_auth.extract_account_id(access)
        if account:
            headers["chatgpt-account-id"] = account
        return headers

    @staticmethod
    def _responses_user_content(content: Any) -> list[dict[str, Any]]:
        """Convert one user message body into Responses content parts (R3-F5).

        Chat-completions multimodal blocks map to their Responses
        equivalents, preserving order: ``text`` -> ``input_text`` and
        ``image_url`` -> ``input_image``. On the Responses wire (OpenAI
        Responses API input items; the Codex CLI request shape) the image
        travels as a plain ``image_url`` string — a fully qualified URL or a
        ``data:<media-type>;base64,...`` URL — never the chat-completions
        ``{"url": ...}`` object, so an attachment stays a real image input
        instead of a serialized Python representation inside a text block.
        An already-Responses-shaped block (``input_text``/``input_image``)
        passes through; unsupported blocks degrade to their JSON text so no
        content silently disappears, and a plain string keeps the previous
        single ``input_text`` fallback.
        """
        if not isinstance(content, list):
            return [{"type": "input_text", "text": str(content or "")}]
        parts: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append({"type": "input_text", "text": str(block)})
                continue
            block_type = block.get("type")
            if block_type in ("text", "input_text"):
                text = block.get("text")
                parts.append(
                    {
                        "type": "input_text",
                        "text": text if isinstance(text, str) else str(text or ""),
                    }
                )
                continue
            if block_type in ("image_url", "input_image"):
                image = block.get("image_url")
                if isinstance(image, dict):
                    url, detail = image.get("url"), image.get("detail")
                else:
                    url, detail = image, block.get("detail")
                if url:
                    image_part: dict[str, Any] = {"type": "input_image", "image_url": str(url)}
                    if isinstance(detail, str) and detail:
                        image_part["detail"] = detail
                    parts.append(image_part)
                continue
            parts.append({"type": "input_text", "text": json.dumps(block, default=str)})
        return parts or [{"type": "input_text", "text": str(content or "")}]

    @staticmethod
    def _to_responses_input(
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Split OpenAI-style history into (instructions, Responses input items)."""
        instructions: list[str] = []
        items: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "system":
                if content:
                    instructions.append(str(content))
                continue
            if role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(message.get("tool_call_id") or ""),
                        "output": str(content or ""),
                    }
                )
                continue
            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    function = (
                        call.get("function") if isinstance(call.get("function"), dict) else {}
                    )
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": str(call.get("id") or ""),
                            "name": str(function.get("name") or ""),
                            "arguments": str(function.get("arguments") or "{}"),
                        }
                    )
                if content:
                    items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": str(content)}],
                        }
                    )
                continue
            items.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": ChatGPTProvider._responses_user_content(content),
                }
            )
        return "\n\n".join(instructions), items

    def _resolve_effort(self, model: str, explicit: str | None) -> str:
        """Catalog-validated effort: per-call > instance > config key.

        Same precedence as every other provider, but the allowed set comes
        from the catalog entry for *model* (wire-filtered), never a
        cross-model static vocabulary.
        """
        account = self._account_hint()
        if explicit is not None:
            return validate_effort(model, explicit, account_id=account)
        if self._default_effort:
            return validate_effort(model, self._default_effort, account_id=account)
        return validate_effort(model, config.get("reasoning_effort"), account_id=account)

    def _payload(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[ToolDefinition] | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        instructions, items = self._to_responses_input(messages)
        payload: dict[str, Any] = {
            "model": model,
            "instructions": instructions or self.DEFAULT_INSTRUCTIONS,
            "input": items,
            "store": False,
            "stream": True,
        }
        effort = self._resolve_effort(model, reasoning_effort)
        if effort:
            payload["reasoning"] = {"effort": effort}
        if tools:
            payload["tools"] = _tools_payload(tools)
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = True
        return payload

    def _account_hint(self) -> str:
        """Best-effort account id for account-scoped catalog reads.

        Read-only and never raises: without a hint the catalog keeps its
        documented most-recent behavior, so this only ever improves
        isolation between accounts (review round 6, R6-1).
        """
        try:
            return str(chatgpt_auth.status(path=self._auth_path).get("account_id") or "")
        except Exception:
            return ""

    def catalog_scope(self) -> tuple[str, str]:
        """Account + endpoint scope for catalog metadata (R6-1).

        ``context_window_for`` and the inherited ``LLMProvider.context_window``
        both read this, so one account's live catalog window can never answer
        another account's capacity check.
        """
        return self._account_hint(), self.BASE_URL

    async def _resolve_tokens(self) -> dict[str, Any]:
        return await asyncio.to_thread(chatgpt_auth.resolve_tokens, path=self._auth_path)

    async def _refresh_tokens(self, tokens: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(chatgpt_auth.refresh, tokens, path=self._auth_path)
        except chatgpt_auth.ChatGPTAuthError:
            raise
        except Exception as exc:
            raise chatgpt_auth.ChatGPTAuthError(
                f"ChatGPT token refresh failed ({type(exc).__name__}); {chatgpt_auth.LOGIN_HINT}",
                code="chatgpt_refresh_failed",
            ) from exc

    def _unauthorized(self, model: str) -> chatgpt_auth.ChatGPTAuthError:
        return chatgpt_auth.ChatGPTAuthError(
            f"ChatGPT rejected the stored credential (HTTP 401) for {model}; "
            f"{chatgpt_auth.LOGIN_HINT}",
            code="chatgpt_unauthorized",
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
        """Stream a Codex Responses completion as Machina StreamEvents.

        ``temperature``/``max_tokens`` are validated for interface parity but
        never sent: the Codex backend rejects sampling parameters.
        """
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)
        await self._rate_limiter.acquire()

        resolved_model = model or self.model
        validate_model_slug(resolved_model, account_id=self._account_hint())
        payload = self._payload(messages, resolved_model, tools, reasoning_effort)
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
        settlement = ReservationSettlement(reservation_id, usage_store)

        content_parts: list[str] = []
        call_accumulator = _FunctionCallAccumulator()
        stream_usage: dict[str, Any] = {}
        saw_terminal = False
        start = time.monotonic()
        try:
            tokens = await self._resolve_tokens()
            attempt = 0
            while True:
                attempt += 1
                async with self.client.stream(
                    "POST", self.RESPONSES_PATH, json=payload, headers=self._headers(tokens)
                ) as response:
                    if response.status_code == 401 and attempt == 1 and tokens.get("refresh_token"):
                        await response.aread()
                        tokens = await self._refresh_tokens(tokens)
                        continue
                    if response.status_code == 401:
                        await response.aread()
                        raise self._unauthorized(resolved_model)
                    if response.status_code == 429:
                        await response.aread()
                        raise _rate_limit_error(response, resolved_model)
                    if response.status_code != 200:
                        body = (await response.aread()).decode("utf-8", "replace")[:300]
                        raise ProviderError(
                            f"chatgpt provider HTTP {response.status_code} for "
                            f"{resolved_model}: {body}"
                        )
                    async for line in response.aiter_lines():
                        data = sse_data(line)
                        if data is None:
                            continue
                        data = data.strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        event_type = str(event.get("type") or "")
                        if event_type == _WIRE_EVENT_TEXT_DELTA:
                            delta = event.get("delta")
                            if isinstance(delta, str) and delta:
                                content_parts.append(delta)
                                yield StreamEvent(type="text", content=delta)
                        elif event_type == _WIRE_EVENT_ITEM_ADDED:
                            item = event.get("item")
                            if isinstance(item, dict) and item.get("type") == "function_call":
                                call_accumulator.announce(item, event.get("output_index"))
                        elif event_type == _WIRE_EVENT_ARGS_DELTA:
                            call_accumulator.append_delta(event)
                        elif event_type == _WIRE_EVENT_ARGS_DONE:
                            call_accumulator.set_arguments(event)
                        elif event_type == _WIRE_EVENT_ITEM_DONE:
                            item = event.get("item")
                            if isinstance(item, dict) and item.get("type") == "function_call":
                                call_accumulator.settle(item, event.get("output_index"))
                        elif event_type == _WIRE_EVENT_COMPLETED:
                            saw_terminal = True
                            final = event.get("response")
                            if isinstance(final, dict):
                                resolved_model = str(final.get("model") or resolved_model)
                                usage = final.get("usage")
                                if isinstance(usage, dict):
                                    stream_usage = usage
                        elif event_type == _WIRE_EVENT_ERROR:
                            raise _stream_error(event, resolved_model)
                        elif event_type in _WIRE_EVENT_FATAL:
                            raise _terminal_failure(event, resolved_model)
                break
            if not saw_terminal:
                raise _truncated_stream_error(_PROVIDER_NAME, resolved_model)
        except BaseException as exc:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_llm_call(
                session_id="", model=resolved_model, duration_ms=duration_ms, is_error=True
            )
            await settlement.fail()
            if not isinstance(exc, Exception):
                raise
            audit_log(
                "llm_stream_error",
                provider=_PROVIDER_NAME,
                model=resolved_model,
                error=str(exc),
            )
            raise

        tool_calls = call_accumulator.result()
        full_content = "".join(content_parts)
        usage = _map_usage(stream_usage)
        if not usage["prompt_tokens"] and not usage["completion_tokens"]:
            # Estimates support display/accounting but must never release the
            # durable reservation as vendor usage (same rule as direct.py).
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
        await settlement.settle(usage, model=resolved_model)
        audit_log(
            "llm_stream_complete",
            provider=_PROVIDER_NAME,
            model=resolved_model,
            tool_calls=len(tool_calls),
        )
        if logger.isEnabledFor(20):  # DEBUG — stable fields only, never token values.
            logger.debug(
                "chatgpt stream complete model=%s tool_calls=%d", resolved_model, len(tool_calls)
            )
        yield StreamEvent(
            type="done",
            response=LLMResponse(
                content=full_content,
                model=resolved_model,
                usage=usage,
                tool_calls=tool_calls,
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
        async with joined_teardown(
            stream=self.stream_complete(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                reasoning_effort=reasoning_effort,
                session_id=session_id,
                agent_id=agent_id,
            )
        ) as events:
            async for event in events:
                if event.type == "done" and isinstance(event.response, LLMResponse):
                    response = event.response
        if response is None:
            raise ProviderError("chatgpt stream produced no terminal response")
        return response

    async def close(self) -> None:
        await self.client.aclose()
