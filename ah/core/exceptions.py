"""Custom exception hierarchy for AgentHarness.

All exceptions inherit from AgentHarnessError. Where appropriate they also
inherit from built-in exceptions (ValueError, RuntimeError) for backward
compatibility with existing code that catches those types.
"""

from __future__ import annotations


class AgentHarnessError(Exception):
    """Base exception for all AgentHarness errors."""


class ToolError(AgentHarnessError, RuntimeError, ValueError):
    """Raised when a tool execution fails."""


class ProviderError(AgentHarnessError, RuntimeError, ValueError):
    """Raised when an LLM provider encounters an error."""


class DatabaseError(AgentHarnessError, RuntimeError):
    """Raised when a database operation fails."""


class ValidationError(AgentHarnessError, ValueError):
    """Raised when input validation fails."""


class SessionNotFoundError(AgentHarnessError, ValueError):
    """Raised when a session is not found."""


class ContextBudgetExceededError(AgentHarnessError, RuntimeError):
    """Raised when the context budget is exceeded."""


class UsageBudgetExceededError(AgentHarnessError, RuntimeError):
    """Raised before an LLM call when a persistent usage budget is exhausted."""


class StaleOwnershipError(AgentHarnessError, RuntimeError):
    """Raised when a fenced write finds ownership lost/expired (AH-AUDIT-014).

    The transaction rolls back: no stale archive/delete/insert commits.
    Callers abort the operation and surface an explicit stale outcome.
    """


class StaleSnapshotError(AgentHarnessError, RuntimeError):
    """Raised when captured input IDs no longer match durable rows (AH-015).

    Aborts stale replacement instead of inserting duplicate/conflicting
    output. Concurrent inserts outside the captured set are preserved.
    """


class _StatusShim:
    """Minimal stand-in for an httpx.Response carrying only a status code.

    Lets ``getattr(exc.response, "status_code", None) == 429`` style detection
    (see ``ah.core.agent._is_rate_limit_error``) keep working without this
    module importing httpx.
    """

    __slots__ = ("status_code",)

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class RateLimitError(AgentHarnessError, RuntimeError):
    """Raised when a provider rate limit (HTTP 429) blocks the request.

    A quota that resets at a fixed wall-clock time cannot be waited out with a
    few seconds of backoff, so callers must NOT retry this exception — retrying
    burns more of the very quota that is exhausted. ``reset_at`` is a UNIX
    timestamp (seconds) when the provider said the quota returns, when known.

    ``status_code`` and the ``response`` shim are provided so existing
    duck-typed 429 detection keeps working.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 429,
        reset_at: float | None = None,
        daily_limit: int | None = None,
        remaining: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reset_at = reset_at
        self.daily_limit = daily_limit
        self.remaining = remaining
        self.response = _StatusShim(status_code)


__all__ = [
    "AgentHarnessError",
    "ToolError",
    "ProviderError",
    "DatabaseError",
    "ValidationError",
    "SessionNotFoundError",
    "ContextBudgetExceededError",
    "UsageBudgetExceededError",
    "RateLimitError",
    "StaleOwnershipError",
    "StaleSnapshotError",
]
