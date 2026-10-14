"""LLM provider abstraction — facade over the split ``provider`` package.

The former single module was split into focused modules (W5 review item):

* ``reasoning``  — effort normalization and precedence
* ``specs``      — direct-provider table + chat-provider registry
* ``audit``      — sanitized audit events
* ``rate_limit`` — process-wide async token buckets
* ``caches``     — TTL/size-bounded response cache
* ``transport``  — retry/quota policy, SSE helpers, payload shaping
* ``base`` / ``openrouter`` / ``ollama`` / ``direct`` / ``anthropic`` / ``chatgpt``
* ``factory``    — ``get_provider``

``__all__`` below is byte-identical to the pre-split module; consumers keep
importing ``from machina.core.provider import ...`` unchanged.
"""

from machina.core.models import (  # noqa: F401 — historical re-export
    LLMResponse,
    StreamEvent,
    ToolDefinition,
)

from .anthropic import AnthropicProvider  # noqa: F401 — pre-split import surface
from .audit import audit_log
from .base import LLMProvider
from .caches import _cache_clear  # noqa: F401 — documented test-only seam (tests/conftest.py)
from .chatgpt import ChatGPTProvider  # noqa: F401 — chatgpt-account import surface
from .claude import ClaudeProvider  # noqa: F401 — claude-subscription import surface
from .copilot import CopilotProvider  # noqa: F401 — copilot-seat import surface
from .direct import DirectProvider  # noqa: F401 — pre-split import surface
from .factory import get_provider
from .gemini import GeminiProvider  # noqa: F401 — gemini-license import surface
from .ollama import OllamaProvider  # noqa: F401 — pre-split import surface
from .openrouter import OpenRouterProvider  # noqa: F401 — pre-split import surface
from .rate_limit import AsyncTokenBucket, reload_rate_limiters
from .reasoning import (
    ANTHROPIC_THINKING_BUDGETS,  # noqa: F401 — pre-split import surface
    REASONING_EFFORTS,
    normalize_reasoning_effort,
)
from .specs import PROVIDER_SPECS, PROVIDERS, ProviderSpec

__all__ = [
    "audit_log",
    "AsyncTokenBucket",
    "LLMProvider",
    "get_provider",
    "PROVIDERS",
    "PROVIDER_SPECS",
    "ProviderSpec",
    "REASONING_EFFORTS",
    "normalize_reasoning_effort",
    "reload_rate_limiters",
]
