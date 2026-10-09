"""Domain models — typed dataclasses for the agent harness."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "Session",
    "ContextChunk",
    "ToolCall",
    "ToolResult",
    "ToolDefinition",
    "LLMResponse",
    "StreamEvent",
    "AgentResponse",
]


@dataclass
class Session:
    """An agent session — the unit of work."""

    id: uuid.UUID
    title: str | None = None
    agent_id: str = "harness"
    status: str = "active"
    state: dict = field(default_factory=dict)
    goal: str | None = None
    model: str | None = None
    provider: str | None = None
    context_budget: int = 8000
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class ContextChunk:
    """A typed, embedding-backed context record."""

    id: uuid.UUID
    session_id: uuid.UUID
    agent_id: str
    chunk_type: str
    payload: dict[str, Any]
    token_count: int = 0
    embedding: list[float] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    accessed_at: datetime | None = None


@dataclass
class ToolCall:
    """A tool call from the LLM."""

    id: str
    type: str = "function"
    function_name: str = ""
    function_arguments: str = ""
    arguments: dict = field(default_factory=dict)


@dataclass
class ToolResult:
    """The result of executing a tool."""

    tool_call_id: str
    content: str
    tool_name: str = ""
    is_error: bool = False


@dataclass
class ToolDefinition:
    """JSON Schema tool definition for function calling."""

    name: str
    description: str
    parameters: dict  # JSON Schema


@dataclass
class LLMResponse:
    """Standardized LLM response."""

    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)


@dataclass
class StreamEvent:
    """Streaming event from LLM provider or agent."""

    type: str  # "text", "tool_call", "tool_result", "token_usage", "needs_approval", "done"
    content: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    tool_result: str = ""
    tokens_used: int = 0
    response: Any = None  # LLMResponse or AgentResponse
    # AH-020: stable call ID so UI can pair start→complete even when
    # completions arrive out of order. Provider tc["id"] when present,
    # else f"call-{index}".
    tool_call_id: str = ""


@dataclass
class AgentResponse:
    """Response from the agent loop.

    needs_approval carries typed pause outcomes (AH-AUDIT-022): each entry
    binds request_id/operation/target/session for one action awaiting human
    approval. Scheduling and transport decisions MUST use this field — never
    parse prose in content or result previews.
    """

    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tokens_used: int = 0
    iterations: int = 0
    needs_approval: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Derive typed pauses from tool entries (AH-AUDIT-022).

        Every construction site is covered automatically: entries carrying
        a needs_approval marker contribute their pause record. Explicitly
        passed entries are merged (deduplicated by request_id).
        """
        try:
            seen = {
                str(p.get("request_id", ""))
                for p in (self.needs_approval or [])
                if isinstance(p, dict)
            }
            for entry in self.tool_calls or []:
                if not isinstance(entry, dict):
                    continue
                pause = entry.get("needs_approval")
                if isinstance(pause, dict) and pause.get("request_id"):
                    if str(pause["request_id"]) not in seen:
                        seen.add(str(pause["request_id"]))
                        self.needs_approval.append(dict(pause))
        except Exception:
            pass
