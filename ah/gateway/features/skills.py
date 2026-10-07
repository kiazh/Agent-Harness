"""Skills-related feature handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ah import services
from ah.gateway.errors import INVALID_PARAMS, NOT_FOUND, OPERATION_FAILED, RpcError
from ah.gateway.features._common import _int, _skill, _str, _uuid

if TYPE_CHECKING:
    from ah.gateway.server import Gateway


def _registry():
    from ah.skills.registry import skill_registry

    skill_registry.load_all()
    return skill_registry


async def skills_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """List all skills."""
    gw.require_db()
    return {"skills": [_skill(s) for s in sorted(_registry().list_skills(), key=lambda s: s.name)]}


async def skills_show(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Show a skill by name."""
    gw.require_db()
    skill = _registry().get(_str(params, "name", max_len=200))
    if skill is None:
        raise RpcError(NOT_FOUND, f"skill {params.get('name')!r} not found")
    return {"skill": _skill(skill, content=True)}


async def skills_learn(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Learn a new skill from a file, URL, or existing skill."""
    import asyncio

    gw.require_db()
    triggers = params.get("triggers")
    if triggers is not None and (
        not isinstance(triggers, list) or not all(isinstance(t, str) for t in triggers)
    ):
        raise RpcError(INVALID_PARAMS, "triggers must be a list of strings")
    try:
        # 5.8: the sync fetch/parse path runs off-loop so URL retrieval never
        # blocks the gateway event loop.
        skill = await asyncio.to_thread(
            services.learn_skill,
            _str(params, "source", max_len=2000),
            name=_str(params, "name", required=False, max_len=100) or None,
            description=_str(params, "description", required=False, max_len=500) or None,
            triggers=triggers,
        )
    except services.ServiceError as e:
        raise RpcError(OPERATION_FAILED, str(e)) from None
    return {"skill": _skill(skill)}


async def skills_delete(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    name = _str(params, "name", max_len=200)
    if not _registry().delete_skill(name):
        raise RpcError(NOT_FOUND, f"skill {name!r} not found")
    return {"deleted": True}


async def skills_curator(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.skills.registry import SkillCurator

    curator = SkillCurator(_registry())
    report = curator.get_health_report()
    report["unused"] = [s.name for s in curator.get_unused_skills()]
    report["stale"] = [s.name for s in curator.get_stale_skills(_int(params, "days", 30, 1, 3650))]
    return report


async def learning_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """List learning proposals."""
    gw.require_db()
    from ah.skills.learning import learning_reviewer

    session = await gw.get_session(params)
    return {
        "reviews": await learning_reviewer.list_reviews(
            session.agent_id, limit=_int(params, "limit", 50, 1, 100)
        )
    }


async def learning_approve(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Approve a learning proposal."""
    gw.require_db()
    from ah.skills.learning import learning_reviewer

    session = await gw.get_session(params)
    try:
        review = await learning_reviewer.approve(_uuid(params, "id"), agent_id=session.agent_id)
    except (LookupError, PermissionError):
        raise RpcError(NOT_FOUND, "learning proposal not found for this agent") from None
    except (ValueError, FileExistsError) as exc:
        raise RpcError(OPERATION_FAILED, str(exc)) from None
    return {"review": review}


async def learning_reject(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Reject a learning proposal."""
    gw.require_db()
    from ah.skills.learning import learning_reviewer

    session = await gw.get_session(params)
    try:
        review = await learning_reviewer.reject(_uuid(params, "id"), agent_id=session.agent_id)
    except (LookupError, PermissionError):
        raise RpcError(NOT_FOUND, "learning proposal not found for this agent") from None
    return {"review": review}
