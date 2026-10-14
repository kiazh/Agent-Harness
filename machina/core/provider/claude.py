"""Claude Pro/Max subscription provider — the Claude Code OAuth identity.

Reuses the full Anthropic Messages transport (payload shaping, streaming,
thinking-block replay, prompt caching) from :mod:`machina.core.provider.anthropic`
but authenticates with the user's Claude subscription: an OAuth Bearer token
from :mod:`machina.security.claude_auth` instead of an API key, plus the
headers Anthropic routes the subscription by — the Claude Code user agent and
the ``claude-code-20250219`` / ``oauth-2025-04-20`` betas (without them the
subscription route answers 429/5xx). ``x-api-key`` is deliberately absent:
dual auth is rejected.

Credential resolution happens on every request through an httpx auth flow:
``~/.machina/auth/claude.json`` -> import ``~/.claude/.credentials.json`` /
macOS Keychain (read-only there) -> :class:`ClaudeAuthError` with the login
hint. A 401 refreshes the token once (peer-adopting a rotation already in the
store) and replays the request a single time.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from machina.core.exceptions import ValidationError
from machina.core.provider.anthropic import AnthropicProvider
from machina.core.provider.rate_limit import _get_rate_limiter
from machina.core.provider.reasoning import normalize_reasoning_effort
from machina.security import claude_auth

# The Claude Code identity Anthropic routes OAuth traffic by; the version is
# cosmetic on the wire (the prefix is what the subscription route checks).
CLAUDE_CODE_UA = "claude-code/2.1.150 (external, cli)"
OAUTH_BETAS = "claude-code-20250219,oauth-2025-04-20"


class _ClaudeAuth(httpx.Auth):
    """Per-request Bearer resolution + one 401 -> refresh -> replay."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path

    async def async_auth_flow(self, request: httpx.Request):  # type: ignore[override]
        tokens = await asyncio.to_thread(claude_auth.resolve_tokens, path=self.path)
        request.headers["Authorization"] = f"Bearer {tokens['access_token']}"
        response = yield request
        if response.status_code != 401:
            return
        if not tokens.get("refresh_token"):
            raise claude_auth.ClaudeAuthError(
                f"Claude rejected the stored credential (HTTP 401); {claude_auth.LOGIN_HINT}",
                code="claude_unauthorized",
            )
        tokens = await asyncio.to_thread(claude_auth.refresh, tokens, path=self.path)
        request.headers["Authorization"] = f"Bearer {tokens['access_token']}"
        yield request


class ClaudeProvider(AnthropicProvider):
    """Claude subscription (Pro/Max) over the Anthropic Messages API."""

    PROVIDER_NAME = "claude"
    DEFAULT_MODEL = "claude-sonnet-4-5"

    def __init__(
        self,
        model: str | None = None,
        reasoning_effort: str | None = None,
        *,
        auth_path: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Deliberately not calling super().__init__: no API key is involved
        # and the client carries the OAuth identity instead of x-api-key.
        self.model = self._normalize_model(model) or self.DEFAULT_MODEL
        self._default_effort = normalize_reasoning_effort(reasoning_effort)
        self._rate_limiter = _get_rate_limiter(self.PROVIDER_NAME)
        self.auth = _ClaudeAuth(auth_path)
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "anthropic-version": self.API_VERSION,
                "anthropic-beta": OAUTH_BETAS,
                "Content-Type": "application/json",
                "user-agent": CLAUDE_CODE_UA,
                "x-app": "cli",
            },
            auth=self.auth,
            transport=transport,
            timeout=120.0,
        )

    @staticmethod
    def _normalize_model(model: str | None) -> str:
        """Accept bare ids and ``claude/``-prefixed ids; reject non-Claude slugs."""
        name = str(model or "").strip()
        if name.lower().startswith("claude/"):
            name = name[len("claude/") :]
        if name and "claude" not in name.lower():
            raise ValidationError(
                f"model {model!r} is not a Claude model id; the claude provider "
                f"serves Anthropic models (e.g. claude-sonnet-4-5)"
            )
        return name

    async def _resolve_tokens(self) -> dict[str, Any]:
        """Credential resolution seam for tests/diagnostics (auth flow uses it too)."""
        return await asyncio.to_thread(claude_auth.resolve_tokens, path=self.auth.path)
