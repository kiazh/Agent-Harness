"""Human management of learned personality and actual memory-policy feedback."""

from __future__ import annotations

from ah.gateway.errors import INVALID_PARAMS, RpcError
from ah.gateway.features._common import _str, _uuid
from ah.memory.growth import growth_store, require_growth_enabled


def _require_enabled():
    try:
        require_growth_enabled()
    except ValueError as exc:
        raise RpcError(INVALID_PARAMS, str(exc)) from None


async def growth_get(gw, params):
    gw.require_db()
    session = await gw.get_session(params)
    return {
        "growth": await growth_store.get(session.agent_id),
        "episodes": await growth_store.episodes(session.agent_id, session.id),
        "rsi": await growth_store.rsi_status(session.agent_id),
    }


async def growth_teach(gw, params):
    _require_enabled()
    gw.require_db()
    session = await gw.get_session(params)
    try:
        notes = await growth_store.teach(
            session.agent_id,
            _str(params, "kind", max_len=20),
            _str(params, "key", max_len=64),
            _str(params, "content", max_len=500),
        )
    except ValueError as exc:
        raise RpcError(INVALID_PARAMS, str(exc)) from None
    return {"notes": notes}


async def growth_feedback(gw, params):
    _require_enabled()
    gw.require_db()
    session = await gw.get_session(params)
    try:
        accepted = await growth_store.feedback(
            session.agent_id, _uuid(params, "episodeId"), params.get("reward"), session.id
        )
    except ValueError as exc:
        raise RpcError(INVALID_PARAMS, str(exc)) from None
    return {"accepted": accepted}


async def growth_forget(gw, params):
    gw.require_db()
    session = await gw.get_session(params)
    return {"removed": await growth_store.forget(session.agent_id, _str(params, "key", max_len=90))}


async def growth_install_policy(gw, params):
    _require_enabled()
    gw.require_db()
    session = await gw.get_session(params)
    try:
        await growth_store.install_policy(session.agent_id, params.get("policy"))
    except ValueError as exc:
        raise RpcError(INVALID_PARAMS, str(exc)) from None
    return {"installed": True}


async def growth_rollback(gw, params):
    """A human can restore a retained revision, even with learning disabled."""
    gw.require_db()
    session = await gw.get_session(params)
    try:
        restored = await growth_store.rollback(session.agent_id, params.get("revision"))
    except ValueError as exc:
        raise RpcError(INVALID_PARAMS, str(exc)) from None
    return {"restored": restored}
