"""Machina core — models, agent, context, session, provider, config."""

from __future__ import annotations

from machina.core.config import Config
from machina.core.exceptions import (
    ContextBudgetExceededError,
    DatabaseError,
    MachinaError,
    ProviderError,
    SessionNotFoundError,
    ToolError,
    ValidationError,
)
from machina.core.models import (
    AgentResponse,
    ContextChunk,
    LLMResponse,
    Session,
    StreamEvent,
    ToolDefinition,
)

__all__ = [
    "MachinaError",
    "ContextBudgetExceededError",
    "DatabaseError",
    "ProviderError",
    "SessionNotFoundError",
    "ToolError",
    "ValidationError",
    "AgentResponse",
    "ContextChunk",
    "LLMResponse",
    "Session",
    "StreamEvent",
    "ToolDefinition",
    "Config",
    "config",
]


def __getattr__(name: str) -> object:
    """Lazily re-export ``config`` without loading configuration at import (Bug 15).

    ``machina/core/__init__.py`` is executed for every ``machina.core.*``
    import, so eagerly reading ``config`` here forced ``Config.load()`` —
    including ``persona_description`` validation, which imports the memory
    package and transitively the whole provider stack — on any import of
    ``machina.core`` or any of its submodules. Returning the live singleton
    from module ``__getattr__`` keeps ``from machina.core import config``
    working while config stays unloaded until it is actually used.
    """
    if name == "config":
        from machina.core.config import get_config

        return get_config()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
