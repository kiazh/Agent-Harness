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


__all__ = [
    "AgentHarnessError",
    "ToolError",
    "ProviderError",
    "DatabaseError",
    "ValidationError",
    "SessionNotFoundError",
    "ContextBudgetExceededError",
]
