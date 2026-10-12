"""Custom exception hierarchy for Machina.

All exceptions inherit from MachinaError. Where appropriate they also
inherit from built-in exceptions (ValueError, RuntimeError) for backward
compatibility with existing code that catches those types.
"""

from __future__ import annotations


class MachinaError(Exception):
    """Base exception for all Machina errors."""


class ToolError(MachinaError, RuntimeError, ValueError):
    """Raised when a tool execution fails."""


class ProviderError(MachinaError, RuntimeError, ValueError):
    """Raised when an LLM provider encounters an error."""


class DatabaseError(MachinaError, RuntimeError):
    """Raised when a database operation fails."""


class DelegationAccountingError(DatabaseError):
    """Child spend is unknown, so the parent turn must stop before more calls."""


class ValidationError(MachinaError, ValueError):
    """Raised when input validation fails."""


class SessionNotFoundError(MachinaError, ValueError):
    """Raised when a session is not found."""


class ContextBudgetExceededError(MachinaError, RuntimeError):
    """Raised when the context budget is exceeded."""


class UsageBudgetExceededError(MachinaError, RuntimeError):
    """Raised before an LLM call when a persistent usage budget is exhausted."""


class StaleOwnershipError(MachinaError, RuntimeError):
    """Raised when a fenced write finds ownership lost/expired (AH-AUDIT-014).

    The transaction rolls back: no stale archive/delete/insert commits.
    Callers abort the operation and surface an explicit stale outcome.
    """


class StaleSnapshotError(MachinaError, RuntimeError):
    """Raised when captured input IDs no longer match durable rows (AH-015).

    Aborts stale replacement instead of inserting duplicate/conflicting
    output. Concurrent inserts outside the captured set are preserved.
    """


class _StatusShim:
    """Minimal stand-in for an httpx.Response carrying only a status code.

    Lets ``getattr(exc.response, "status_code", None) == 429`` style detection
    (see ``machina.core.agent._is_rate_limit_error``) keep working without this
    module importing httpx.
    """

    __slots__ = ("status_code",)

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class RateLimitError(MachinaError, RuntimeError):
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
    "MachinaError",
    "ToolError",
    "ProviderError",
    "DatabaseError",
    "DelegationAccountingError",
    "ValidationError",
    "SessionNotFoundError",
    "ContextBudgetExceededError",
    "UsageBudgetExceededError",
    "RateLimitError",
    "StaleOwnershipError",
    "StaleSnapshotError",
]
