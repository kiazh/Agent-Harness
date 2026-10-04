"""Scheduled jobs keep an explicit model/provider selection across restarts."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from ah.core.agent_def import AgentDef, agent_registry
from ah.core.scheduler import JobRunner, JobStore
from ah.core.session import SessionManager


async def test_job_model_pin_persists_after_reload(db_pool, monkeypatch):
    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    session = await SessionManager().create(title="pinned job", agent_id=f"agent-{uuid.uuid4()}")
    store = JobStore()
    try:
        created = await store.create(
            name="pinned",
            kind="interval",
            session_id=session.id,
            prompt="Check deployment",
            interval_seconds=60,
            agent_name="researcher",
            model="openrouter/free",
            provider="openrouter",
        )
        assert created.model == "openrouter/free"
        assert created.provider == "openrouter"
        reloaded = await store.get(created.id)
        assert reloaded.to_dict()["model"] == "openrouter/free"
        assert reloaded.provider == "openrouter"
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


async def test_job_pin_overrides_agent_definition_for_runner(monkeypatch):
    definition = AgentDef(
        name="researcher",
        model="old-model",
        provider="ollama",
        system_prompt="Research",
        tools=["read_file"],
        max_iterations=2,
    )
    monkeypatch.setattr(agent_registry, "get", AsyncMock(return_value=definition))
    selected = []
    provider = object()

    def fake_get_provider(**kwargs):
        selected.append(kwargs)
        return provider

    monkeypatch.setattr("ah.core.provider.get_provider", fake_get_provider)
    agent = await JobRunner()._build_agent(
        "researcher", model="openrouter/free", provider="openrouter"
    )
    assert selected == [{"provider": "openrouter", "model": "openrouter/free"}]
    assert agent.allowed_tools == ["read_file"]


async def test_gateway_accepts_job_pin_and_rejects_empty_model(db_pool, monkeypatch):
    from ah.gateway.errors import INVALID_PARAMS, RpcError
    from ah.gateway.features.jobs import jobs_create

    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    session = await SessionManager().create(title="gateway pin", agent_id=f"agent-{uuid.uuid4()}")

    class GatewayStub:
        def require_db(self):
            return None

        async def get_session(self, params):
            return session

    try:
        result = await jobs_create(
            GatewayStub(),
            {
                "sessionId": str(session.id),
                "kind": "interval",
                "prompt": "check",
                "model": "openrouter/free",
                "provider": "openrouter",
            },
        )
        assert result["job"]["model"] == "openrouter/free"
        assert result["job"]["provider"] == "openrouter"
        defaulted = await jobs_create(
            GatewayStub(),
            {
                "sessionId": str(session.id),
                "kind": "interval",
                "prompt": "check",
            },
        )
        assert defaulted["job"]["agent"] == session.agent_id
        try:
            await jobs_create(
                GatewayStub(),
                {
                    "sessionId": str(session.id),
                    "kind": "interval",
                    "prompt": "check",
                    "model": "",
                },
            )
        except RpcError as exc:
            assert exc.code == INVALID_PARAMS
        else:
            raise AssertionError("empty model was accepted")
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", session.id)


async def test_session_scoped_job_mutations_reject_foreign_jobs(db_pool, monkeypatch):
    from ah.gateway.errors import NOT_FOUND, RpcError
    from ah.gateway.features.jobs import jobs_delete, jobs_set_enabled

    monkeypatch.setattr("ah.core.session.db", db_pool)
    monkeypatch.setattr("ah.core.scheduler.db", db_pool)
    owner = await SessionManager().create(title="job owner", agent_id=f"owner-{uuid.uuid4()}")
    other = await SessionManager().create(title="other", agent_id=f"other-{uuid.uuid4()}")
    store = JobStore()
    job = await store.create(
        name="owned",
        kind="interval",
        session_id=owner.id,
        prompt="check",
        interval_seconds=60,
    )

    class GatewayStub:
        def require_db(self):
            return None

        async def get_session(self, params):
            return other

    try:
        for method, params in (
            (jobs_set_enabled, {"id": str(job.id), "enabled": False}),
            (jobs_delete, {"id": str(job.id)}),
        ):
            try:
                await method(GatewayStub(), {**params, "sessionId": str(other.id)})
            except RpcError as exc:
                assert exc.code == NOT_FOUND
            else:
                raise AssertionError("foreign job was modified")
        assert (await store.get(job.id)).enabled is True
    finally:
        await db_pool.execute(
            "DELETE FROM sessions WHERE id = ANY($1::uuid[])", [owner.id, other.id]
        )
