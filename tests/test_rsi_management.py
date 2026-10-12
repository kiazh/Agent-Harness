"""Session-owned management and the real CLI share the RSI store."""

import json
import os
import subprocess
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


async def test_gateway_inspection_and_rollback_use_session_agent(db_pool, monkeypatch):
    from machina.gateway.errors import RpcError
    from machina.gateway.features import growth
    from machina.memory.growth import GrowthStore

    store = GrowthStore(db_pool)
    owner = "rsi-rpc-" + uuid.uuid4().hex
    other = owner + "other"
    monkeypatch.setattr(growth, "growth_store", store)
    gw = SimpleNamespace(
        require_db=lambda: None,
        get_session=AsyncMock(return_value=SimpleNamespace(agent_id=owner, id=uuid.uuid4())),
    )
    try:
        await store.teach(owner, "style", "tone", "Be direct")
        await store.teach(owner, "style", "tone", "Be warmer")
        inspected = await growth.growth_get(gw, {})
        assert inspected["rsi"]["revision"] == 2
        assert await growth.growth_rollback(gw, {"revision": 1}) == {"restored": True}
        assert (await store.get(owner))["notes"]["style:tone"] == "Be direct"
        gw.get_session.return_value = SimpleNamespace(agent_id=other, id=uuid.uuid4())
        assert await growth.growth_rollback(gw, {"revision": 1, "agentId": owner}) == {
            "restored": False
        }
        with pytest.raises(RpcError):
            await growth.growth_rollback(gw, {"revision": True})
    finally:
        await db_pool.execute(
            "DELETE FROM agent_growth WHERE agent_id = ANY($1::text[])", [owner, other]
        )


async def test_real_cli_can_inspect_and_restore_an_owned_revision(db_pool, tmp_path):
    owner = "rsi-cli-" + uuid.uuid4().hex
    sid = uuid.uuid4()
    from machina.memory.growth import GrowthStore

    store = GrowthStore(db_pool)
    await db_pool.execute("INSERT INTO sessions(id, agent_id) VALUES ($1, $2)", sid, owner)
    await store.teach(owner, "style", "tone", "Be direct")
    await store.teach(owner, "style", "tone", "Be warmer")
    env = dict(os.environ)
    env.update(
        {
            "MACHINA_ENV_FILE": str(tmp_path / "absent.env"),
            "DATABASE_URL": db_pool.dsn,
            "MACHINA_DATABASE_URL": db_pool.dsn,
        }
    )
    try:
        import asyncio

        def invoke(*args):
            return subprocess.run(
                [sys.executable, "-m", "machina", "growth", *args, "--session", str(sid)],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )

        result = await asyncio.to_thread(invoke, "show")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["rsi"]["revision"] == 2
        restored = await asyncio.to_thread(invoke, "rollback", "1")
        assert restored.returncode == 0, restored.stderr
        assert json.loads(restored.stdout)["restored"] is True
        assert (await store.get(owner))["notes"]["style:tone"] == "Be direct"
    finally:
        await db_pool.execute("DELETE FROM sessions WHERE id = $1", sid)
        await db_pool.execute("DELETE FROM agent_growth WHERE agent_id = $1", owner)
