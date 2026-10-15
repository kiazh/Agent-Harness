"""Domain models — typed dataclasses for the agent harness."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from machina.observability.diagnostics import record_failure

__all__ = [
    "Session",
    "ContextChunk",
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
    # Derived per-request provider capacity; never persisted as authority/spend.
    context_window: int | None = None
    # Model id that produced context_window; guards against stale reuse.
    context_window_model: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    execution_mode: str = "ask"
    parent_session_id: uuid.UUID | None = None
    delegation_authority: dict[str, Any] = field(default_factory=dict)


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
    document_source: str | None = None
    # HELD-55 ordinal: durable per-event sequence assigned in original event
    # order. Additive and optional — None for legacy rows (sorts NULLS LAST).
    # The consolidation cursor carries it so a same-created_at batch that
    # straddles a page boundary is never skipped.
    event_seq: int | None = None


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
    tool_outcome: str = ""


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
    outcome: str = "completed"

    def __post_init__(self) -> None:
        """Derive typed pauses from tool entries (AH-AUDIT-022).

        Every construction site is covered automatically: entries carrying a
        ``needs_approval`` (singular) or ``needs_approvals`` (plural) marker
        contribute their pause record. The plural list wins when present and
        truthy — mirroring ``turn_flow.collect_pauses`` — so a delegated tool
        that paused N>1 approvals contributes all of them, never only the
        first; the singular remains the fallback. Explicitly passed entries
        are merged (deduplicated by request_id).
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
                pauses = entry.get("needs_approvals") or [entry.get("needs_approval")]
                for pause in pauses:
                    if not isinstance(pause, dict) or not pause.get("request_id"):
                        continue
                    request_id = str(pause["request_id"])
                    if request_id not in seen:
                        seen.add(request_id)
                        self.needs_approval.append(dict(pause))
        except Exception as _boundary_error:
            # Optional fallback preserves the primary outcome; report no payload.
            record_failure("models.__post_init__", _boundary_error)
