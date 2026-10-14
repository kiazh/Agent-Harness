"""Gemini subscription provider — the Gemini CLI's Cloud Code Assist protocol.

Speaks the same backend the Gemini CLI uses on a Gemini Code Assist license
(Standard/Enterprise or equivalent Workspace seat):

* ``POST https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse``
  with a Bearer access token and the request wrapper
  ``{model, project, user_prompt_id, request:{contents, systemInstruction,
  tools, generationConfig}}`` (pinned against the Apache-2.0 gemini-cli
  ``code_assist/server.ts`` + ``converter.ts``).
* First call per account onboards: ``loadCodeAssist`` discovers the
  Cloud Code Assist project (and eligibility); when onboarding is required
  the provider runs ``onboardUser`` and polls the operation. The resolved
  project is persisted onto the Machina token store so later runs skip it.
* Google stopped serving consumer tiers (free / AI Pro / Ultra) through this
  flow on 2026-06-18; an ineligible account fails with a typed
  ``gemini_not_eligible`` error carrying Google's reason codes.

Credential resolution order: ``~/.machina/auth/gemini.json`` -> import
``~/.gemini/oauth_creds.json`` (read-only there) -> :class:`GeminiAuthError`
with the login hint. A 401 refreshes the token once and retries the request a
single time. SSE streams must end with a candidate ``finishReason``; a clean
EOF without one raises the truncated-stream error (same discipline as the
other transports).
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
from machina.core.exceptions import ProviderError, RateLimitError
from machina.core.metrics import metrics
from machina.core.models import LLMResponse, StreamEvent, ToolDefinition
from machina.core.provider.audit import audit_log
from machina.core.provider.base import LLMProvider
from machina.core.provider.rate_limit import _get_rate_limiter
from machina.core.provider.transport import (
    _truncated_stream_error,
    _validate_messages,
    _validate_params,
)
from machina.core.usage import _estimate_tokens, usage_store
from machina.security import gemini_auth

_PROVIDER_NAME = "gemini"
_CODE_ASSIST_METADATA = {
    "ideType": "GEMINI_CLI",
    "platform": "PLATFORM_UNSPECIFIED",
    "pluginType": "GEMINI",
}
_DEPRECATION_HINT = (
    "Google stopped serving consumer tiers (free, Google AI Pro, Ultra) through this "
    "flow on 2026-06-18; a Gemini Code Assist Standard/Enterprise (or equivalent "
    "Workspace) license is required"
)


def _rate_limit_error(model: str) -> RateLimitError:
    """Cloud Code Assist quota exhausted (429). Credentials stay valid."""
    return RateLimitError(
        f"Gemini usage limit reached (HTTP 429) for {model}; credentials are still valid. "
        "Retry after the usage window resets.",
        reset_at=None,
    )


def _parts_to_text(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    texts = [
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and not part.get("thought") and part.get("text")
    ]
    return "".join(texts)


class GeminiProvider(LLMProvider):
    """Cloud Code Assist over the Gemini subscription backend."""

    BASE_URL = "https://cloudcode-pa.googleapis.com"
    API_VERSION = "v1internal"
    DEFAULT_MODEL = "gemini-2.5-pro"
    ONBOARD_POLL_ATTEMPTS = 15
    ONBOARD_POLL_DELAY = 1.5

    def __init__(
        self,
        model: str | None = None,
        reasoning_effort: str | None = None,
        *,
        auth_path: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model or self.DEFAULT_MODEL
        self._default_effort = str(reasoning_effort or "").strip().lower()
        self._rate_limiter = _get_rate_limiter(_PROVIDER_NAME)
        self._auth_path = auth_path
        self._project_memo: str | None = None
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            transport=transport,
            timeout=httpx.Timeout(300.0, connect=15.0),
        )

    # ─── credentials + onboarding ────────────────────────────────────────────

    async def _resolve_tokens(self) -> dict[str, Any]:
        return await asyncio.to_thread(gemini_auth.resolve_tokens, path=self._auth_path)

    async def _refresh_tokens(self, tokens: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(gemini_auth.refresh, tokens, path=self._auth_path)
        except gemini_auth.GeminiAuthError:
            raise
        except Exception as exc:
            raise gemini_auth.GeminiAuthError(
                f"Gemini token refresh failed ({type(exc).__name__}); {gemini_auth.LOGIN_HINT}",
                code="gemini_refresh_failed",
            ) from exc

    def _method_url(self, method: str) -> str:
        return f"/{self.API_VERSION}:{method}"

    async def _post(
        self,
        method: str,
        payload: dict[str, Any],
        tokens: dict[str, Any],
        *,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        return await self.client.post(
            self._method_url(method),
            json=payload,
            params=params,
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

    async def _ensure_project(self, tokens: dict[str, Any]) -> str:
        """Resolve (and when needed onboard) the Cloud Code Assist project.

        The stored project wins; otherwise ``loadCodeAssist`` decides among
        adopt-existing / onboard / ineligible, exactly like the CLI's startup
        path. The result is memoized per process and persisted to the store.
        """
        stored = str(tokens.get("project") or "").strip()
        if stored:
            self._project_memo = stored
            return stored
        if self._project_memo:
            return self._project_memo
        request: dict[str, Any] = {"metadata": dict(_CODE_ASSIST_METADATA)}
        response = await self._post("loadCodeAssist", request, tokens)
        if response.status_code == 401:
            raise gemini_auth.GeminiAuthError(
                f"Gemini rejected the stored credential (HTTP 401); {gemini_auth.LOGIN_HINT}",
                code="gemini_unauthorized",
            )
        if response.status_code == 403:
            body = (await response.aread()).decode("utf-8", "replace")[:300]
            raise gemini_auth.GeminiAuthError(
                "Gemini onboarding was denied (HTTP 403) while resolving the Cloud Code "
                f"Assist project: {body}",
                code="gemini_onboarding_denied",
            )
        if response.status_code != 200:
            body = (await response.aread()).decode("utf-8", "replace")[:300]
            raise ProviderError(f"gemini loadCodeAssist HTTP {response.status_code}: {body}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("gemini loadCodeAssist returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderError("gemini loadCodeAssist returned an unexpected payload")

        project = payload.get("cloudaicompanionProject")
        if isinstance(project, str) and project.strip():
            return await self._remember_project(project.strip())

        ineligible = [
            tier for tier in (payload.get("ineligibleTiers") or []) if isinstance(tier, dict)
        ]
        current = (
            payload.get("currentTier") if isinstance(payload.get("currentTier"), dict) else None
        )
        allowed = [tier for tier in (payload.get("allowedTiers") or []) if isinstance(tier, dict)]
        tier = (
            current
            or next((t for t in allowed if t.get("isDefault")), None)
            or (allowed[0] if allowed else None)
        )
        if tier is None:
            reasons = (
                "; ".join(
                    str(item.get("reasonCode") or item.get("reasonMessage") or "ineligible")
                    for item in ineligible
                )
                or "no allowed tier was offered"
            )
            raise gemini_auth.GeminiAuthError(
                f"This Google account is not eligible for Cloud Code Assist via the "
                f"Gemini CLI flow ({reasons}); {_DEPRECATION_HINT}.",
                code="gemini_not_eligible",
            )
        tier_id = str(tier.get("id") or "").strip()
        onboard = await self._post(
            "onboardUser",
            {
                "tierId": tier_id,
                "cloudaicompanionProject": None,
                "metadata": dict(_CODE_ASSIST_METADATA),
            },
            tokens,
        )
        if onboard.status_code not in (200, 201):
            body = (await onboard.aread()).decode("utf-8", "replace")[:300]
            raise ProviderError(f"gemini onboardUser HTTP {onboard.status_code}: {body}")
        operation = onboard.json() if onboard.status_code == 200 else {}
        if not isinstance(operation, dict):
            operation = {}
        for _ in range(max(0, self.ONBOARD_POLL_ATTEMPTS)):
            if operation.get("done"):
                break
            name = str(operation.get("name") or "").strip()
            if not name:
                break
            await asyncio.sleep(self.ONBOARD_POLL_DELAY)
            poll = await self.client.get(
                f"/{self.API_VERSION}/{name}",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            if poll.status_code != 200:
                body = (await poll.aread()).decode("utf-8", "replace")[:300]
                raise ProviderError(f"gemini operation poll HTTP {poll.status_code}: {body}")
            polled = poll.json()
            operation = polled if isinstance(polled, dict) else {}
        created = operation.get("response") if isinstance(operation.get("response"), dict) else {}
        created_project = (
            created.get("cloudaicompanionProject") if isinstance(created, dict) else None
        )
        if isinstance(created_project, dict):
            created_project = created_project.get("id")
        if not isinstance(created_project, str) or not created_project.strip():
            raise gemini_auth.GeminiAuthError(
                "Gemini onboarding finished without a Cloud Code Assist project; "
                f"{_DEPRECATION_HINT}.",
                code="gemini_onboarding_no_project",
            )
        return await self._remember_project(created_project.strip())

    async def _remember_project(self, project: str) -> str:
        self._project_memo = project
        try:
            await asyncio.to_thread(gemini_auth.update_project, project, path=self._auth_path)
        except Exception:  # a store write must never break the live call
            pass
        return project

    # ─── payload shaping ─────────────────────────────────────────────────────

    @staticmethod
    def _to_contents(
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Split OpenAI-style history into (system text, Vertex contents)."""
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        call_names: dict[str, str] = {}
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "system":
                if content:
                    system_parts.append(str(content))
                continue
            if role == "tool":
                call_id = str(message.get("tool_call_id") or "")
                name = call_names.get(call_id) or "tool"
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": name,
                                    "response": {"content": str(content or "")},
                                }
                            }
                        ],
                    }
                )
                continue
            if role == "assistant":
                parts: list[dict[str, Any]] = []
                if content:
                    parts.append({"text": str(content)})
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    function = (
                        call.get("function") if isinstance(call.get("function"), dict) else {}
                    )
                    name = str(function.get("name") or "")
                    call_id = str(call.get("id") or "")
                    if call_id and name:
                        call_names[call_id] = name
                    raw_args = function.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    parts.append(
                        {
                            "functionCall": {
                                "name": name,
                                "args": args if isinstance(args, dict) else {},
                            }
                        }
                    )
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue
            contents.append({"role": "user", "parts": [{"text": str(content or "")}]})
        return "\n\n".join(system_parts), contents

    def _payload(
        self,
        messages: list[dict[str, Any]],
        model: str,
        project: str,
        temperature: float,
        max_tokens: int,
        tools: list[ToolDefinition] | None,
    ) -> dict[str, Any]:
        system, contents = self._to_contents(messages)
        request: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": temperature},
        }
        if system:
            request["systemInstruction"] = {"parts": [{"text": system}]}
        if max_tokens:
            request["generationConfig"]["maxOutputTokens"] = max_tokens
        if tools:
            request["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                        }
                        for tool in tools
                    ]
                }
            ]
        return {
            "model": model,
            "project": project,
            "user_prompt_id": str(uuid.uuid4()),
            "request": request,
        }

    # ─── streaming ───────────────────────────────────────────────────────────

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
        """Stream a Cloud Code Assist completion as Machina StreamEvents."""
        del reasoning_effort  # Gemini model choice expresses effort
        _validate_messages(messages)
        _validate_params(temperature, max_tokens)
        await self._rate_limiter.acquire()

        resolved_model = model or self.model
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
        tool_calls: list[dict[str, Any]] = []
        stream_usage: dict[str, Any] = {}
        saw_terminal = False
        start = time.monotonic()
        try:
            tokens = await self._resolve_tokens()
            project = await self._ensure_project(tokens)
            payload = self._payload(
                messages, resolved_model, project, temperature, max_tokens, tools
            )
            attempt = 0
            while True:
                attempt += 1
                async with self.client.stream(
                    "POST",
                    self._method_url("streamGenerateContent"),
                    json=payload,
                    params={"alt": "sse"},
                    headers={"Authorization": f"Bearer {tokens['access_token']}"},
                ) as response:
                    if response.status_code == 401 and attempt == 1 and tokens.get("refresh_token"):
                        await response.aread()
                        tokens = await self._refresh_tokens(tokens)
                        continue
                    if response.status_code == 401:
                        await response.aread()
                        raise gemini_auth.GeminiAuthError(
                            f"Gemini rejected the stored credential (HTTP 401) for "
                            f"{resolved_model}; {gemini_auth.LOGIN_HINT}",
                            code="gemini_unauthorized",
                        )
                    if response.status_code == 429:
                        await response.aread()
                        raise _rate_limit_error(resolved_model)
                    if response.status_code != 200:
                        body = (await response.aread()).decode("utf-8", "replace")[:300]
                        raise ProviderError(
                            f"gemini provider HTTP {response.status_code} for "
                            f"{resolved_model}: {body}"
                        )
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        inner = chunk.get("response")
                        if not isinstance(inner, dict):
                            continue
                        model_version = inner.get("modelVersion")
                        if isinstance(model_version, str) and model_version:
                            resolved_model = model_version
                        usage = inner.get("usageMetadata")
                        if isinstance(usage, dict):
                            stream_usage = usage
                        candidates = inner.get("candidates")
                        candidate = (
                            candidates[0]
                            if isinstance(candidates, list)
                            and candidates
                            and isinstance(candidates[0], dict)
                            else None
                        )
                        if candidate is None:
                            continue
                        content = candidate.get("content")
                        parts = content.get("parts") if isinstance(content, dict) else None
                        if isinstance(parts, list):
                            for part in parts:
                                if not isinstance(part, dict):
                                    continue
                                text = part.get("text")
                                if isinstance(text, str) and text and not part.get("thought"):
                                    content_parts.append(text)
                                    yield StreamEvent(type="text", content=text)
                                call = part.get("functionCall")
                                if isinstance(call, dict) and call.get("name"):
                                    args = call.get("args")
                                    tool_calls.append(
                                        {
                                            "id": f"call_{uuid.uuid4().hex[:24]}",
                                            "type": "function",
                                            "function": {
                                                "name": str(call.get("name")),
                                                "arguments": json.dumps(
                                                    args if isinstance(args, dict) else {}
                                                ),
                                            },
                                        }
                                    )
                        finish = candidate.get("finishReason")
                        if (
                            isinstance(finish, str)
                            and finish
                            and finish != "FINISH_REASON_UNSPECIFIED"
                        ):
                            saw_terminal = True
                break
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

        full_content = "".join(content_parts)
        usage = {
            "prompt_tokens": int(stream_usage.get("promptTokenCount") or 0),
            "completion_tokens": int(stream_usage.get("candidatesTokenCount") or 0),
            "total_tokens": int(stream_usage.get("totalTokenCount") or 0),
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
            raise ProviderError("gemini stream produced no terminal response")
        return response

    async def close(self) -> None:
        await self.client.aclose()
