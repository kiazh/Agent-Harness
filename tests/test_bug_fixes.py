"""Regression tests for gateway/orchestrator bug fixes.

Bug 7: agents_save doesn't protect all built-in names.
Bug 8: agents_run parallel mode loses results on failure.
"""
from __future__ import annotations

import uuid

import pytest

from ah.gateway.errors import INVALID_PARAMS, RpcError


# ─── Bug 7: agents_save built-in name protection ──────────────────────────────


class _MockGateway:
    """Minimal gateway stub — agents_save only calls require_db()."""

    def require_db(self) -> None:
        pass


@pytest.mark.asyncio
async def test_agents_save_rejects_all_builtin_names():
    """agents_save must reject every name in BUILTIN_AGENTS."""
    from ah.core.agent_def import BUILTIN_AGENTS
    from ah.gateway.features import agents_save

    gw = _MockGateway()
    for name in BUILTIN_AGENTS:
        with pytest.raises(RpcError) as exc_info:
            await agents_save(gw, {"name": name, "tools": ["read_file"]})
        assert exc_info.value.code == INVALID_PARAMS, (
            f"agents_save should reject built-in name {name!r}"
        )


@pytest.mark.asyncio
async def test_agents_save_allows_non_builtin_names():
    """agents_save must allow names that are not in BUILTIN_AGENTS."""
    from ah.gateway.features import agents_save

    gw = _MockGateway()
    # "orchestrator" and "delegate-tool" are NOT in BUILTIN_AGENTS,
    # so they should pass the name check (and fail later on DB, which
    # is not available here — but the name check itself must not reject them).
    # We verify this by checking that the error is NOT INVALID_PARAMS from
    # the name check. Since there's no DB, we expect a DatabaseError.
    from ah.core.exceptions import DatabaseError

    for name in ("orchestrator", "delegate-tool"):
        with pytest.raises(DatabaseError):
            await agents_save(gw, {"name": name, "tools": ["read_file"]})


# ─── Bug 8: run_parallel loses results on failure ─────────────────────────────


@pytest.mark.asyncio
async def test_run_parallel_returns_results_despite_failure(monkeypatch):
    """One failing delegation must not lose the other results."""
    from ah.core import orchestrator as orch_mod
    from ah.core.agent_def import AgentDef
    from ah.core.models import AgentResponse
    from ah.core.orchestrator import Orchestrator

    # Mock agent_registry.get — no DB needed
    async def mock_get(name: str) -> AgentDef:
        return AgentDef(name=name, tools=[])

    monkeypatch.setattr(orch_mod.agent_registry, "get", mock_get)

    # Mock session_manager.create — no DB needed
    class _MockSession:
        def __init__(self) -> None:
            self.id = uuid.uuid4()

    async def mock_create(**kwargs) -> _MockSession:
        return _MockSession()

    monkeypatch.setattr(orch_mod.session_manager, "create", mock_create)

    # Build orchestrator and mock its DB-touching methods
    orch = Orchestrator()

    async def mock_record_start(*args, **kwargs) -> uuid.UUID:
        return uuid.uuid4()

    async def mock_record_end(*args, **kwargs) -> None:
        pass

    monkeypatch.setattr(orch, "_record_start", mock_record_start)
    monkeypatch.setattr(orch, "_record_end", mock_record_end)

    # Agent factory: "coder" raises in __init__ (outside delegate's try/except)
    class _FakeAgent:
        def __init__(self, definition: AgentDef) -> None:
            self.definition = definition

        async def run(self, session_id, user_message, verbose=True) -> AgentResponse:
            return AgentResponse(
                content=f"[{self.definition.name}] handled: {user_message}",
                tool_calls=[],
                tokens_used=10,
                iterations=1,
            )

    class _FailingAgent:
        def __init__(self, definition: AgentDef) -> None:
            raise RuntimeError("factory exploded")

    def factory(definition: AgentDef):
        if definition.name == "coder":
            return _FailingAgent(definition)
        return _FakeAgent(definition)

    orch._agent_factory = factory

    # Run parallel — one agent fails, two succeed
    results = await orch.run_parallel(
        [("researcher", "a"), ("coder", "b"), ("harness", "c")]
    )

    # All three slots must be present (no results lost)
    assert len(results) == 3

    # Separate successful DelegationResults from exceptions
    delegation_results = [r for r in results if hasattr(r, "agent")]
    exceptions = [r for r in results if isinstance(r, Exception)]

    # The two good agents must have complete results
    assert len(delegation_results) == 2
    assert {r.agent for r in delegation_results} == {"researcher", "harness"}
    assert all(r.status == "complete" for r in delegation_results)

    # The failing agent must appear as an exception, not kill the batch
    assert len(exceptions) == 1
    assert isinstance(exceptions[0], RuntimeError)
