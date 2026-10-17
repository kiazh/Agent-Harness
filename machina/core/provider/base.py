"""Base provider interface — the seam every transport implements."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from machina.core.models import LLMResponse, StreamEvent, ToolDefinition


# ---------------------------------------------------------------------------
# Base provider
# ---------------------------------------------------------------------------
class LLMProvider:
    """Base provider interface."""

    def catalog_scope(self) -> tuple[str, str]:
        """Account/base-URL scope for catalog metadata lookups (R6-1).

        Providers with account-scoped live catalogs override this. The default
        has no scope, so lookups keep the documented most-recent fallback.
        """
        return "", ""

    async def context_window(self, model: str | None = None) -> int:
        from machina.core.context_windows import context_window_for

        return context_window_for(model or getattr(self, "model", None), self)

    def reservation_max_tokens(self, max_tokens: int, reasoning_effort: str | None = None) -> int:
        """Maximum output requested on the wire, including reasoning tokens."""
        return max_tokens

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

    async def close(self) -> None:
        """Release provider resources.

        Base contract: a provider is always closable. Concrete transports
        override this to close their HTTP client; resource-free providers
        (and test doubles) inherit this no-op.
        """
