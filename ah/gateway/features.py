"""Gateway methods for every AgentHarness feature beyond chatting.

Each handler takes the running :class:`~ah.gateway.server.Gateway` and the
request params, and returns a JSON-serializable dict. They are registered into
the gateway's method table by :func:`register`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ah import services
from ah.core.config import DEFAULTS, SECRET_KEYS, config
from ah.core.context import context_manager
from ah.core.session import session_manager
from ah.gateway.errors import (
    INVALID_PARAMS,
    NOT_FOUND,
    OPERATION_FAILED,
    TURN_IN_PROGRESS,
    RpcError,
)
from ah.gateway.serializers import chunk_preview, session_to_dict

if TYPE_CHECKING:
    from ah.gateway.server import Gateway

__all__ = ["register", "coerce_config_value"]

Handler = Callable[["Gateway", dict[str, Any]], Awaitable[dict[str, Any]]]
MEMORY_CATEGORIES = ("preference", "decision", "fact", "event", "transient")


# ─── param helpers ────────────────────────────────────────────────────────────


def _str(params: dict[str, Any], key: str, *, required: bool = True, max_len: int = 10_000) -> str:
    value = params.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise RpcError(INVALID_PARAMS, f"{key} is required")
        return ""
    if not isinstance(value, str):
        raise RpcError(INVALID_PARAMS, f"{key} must be a string")
    value = value.strip()
    if len(value) > max_len:
        raise RpcError(INVALID_PARAMS, f"{key} is too long (max {max_len} characters)")
    return value


def _int(params: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = params.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise RpcError(INVALID_PARAMS, f"{key} must be an integer between {lo} and {hi}")
    return value


def _uuid(params: dict[str, Any], key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(params.get(key)))
    except (ValueError, TypeError):
        raise RpcError(INVALID_PARAMS, f"{key} must be a UUID") from None


def coerce_config_value(key: str, value: Any) -> Any:
    """Convert *value* to the type of ``DEFAULTS[key]``; raise RpcError if impossible."""
    default = DEFAULTS[key]
    try:
        if isinstance(default, bool):
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("true", "1", "yes", "on"):
                return True
            if text in ("false", "0", "no", "off"):
                return False
            raise ValueError
        if isinstance(default, int):
            return int(value)
        if isinstance(default, float):
            return float(value)
        return str(value)
    except (ValueError, TypeError):
        expected = type(default).__name__
        raise RpcError(INVALID_PARAMS, f"{key} must be a {expected}") from None


# ─── serializers ──────────────────────────────────────────────────────────────


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _memory(m: Any, score: float | None = None) -> dict[str, Any]:
    data = {
        "id": str(m.id),
        "content": m.content,
        "category": m.category,
        "importance": round(float(m.importance), 3),
        "accessCount": m.access_count,
        "createdAt": _iso(m.created_at),
        "sessionId": str(m.session_id) if m.session_id else None,
    }
    if score is not None:
        data["score"] = round(float(score), 4)
    return data


def _pending(p: Any) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "content": p.content,
        "category": p.category,
        "importance": round(float(p.importance), 3),
        "redactions": list(p.redactions),
        "status": str(p.status),
        "createdAt": _iso(p.created_at),
    }


def _skill(s: Any, content: bool = False) -> dict[str, Any]:
    data = {
        "name": s.name,
        "description": s.description,
        "triggers": list(s.triggers),
        "version": s.version,
        "sourceType": s.source_type,
        "usageCount": s.usage_count,
        "enabled": s.enabled,
    }
    if content:
        data["content"] = s.content
    return data


def _profile(p: Any) -> dict[str, Any]:
    return {
        "userId": p.user_id,
        "displayName": p.display_name,
        "preferences": p.preferences,
        "interactionCount": p.interaction_count,
        "topTopics": [{"topic": t, "count": c} for t, c in p.get_top_topics()],
        "updatedAt": _iso(p.updated_at),
    }


def _config_snapshot() -> dict[str, Any]:
    return {
        key: (bool(config.get(key)) if key in SECRET_KEYS else config.get(key)) for key in DEFAULTS
    }


# ─── sessions ─────────────────────────────────────────────────────────────────


async def session_fork(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    source = await gw.get_session(params)
    title = _str(params, "title", required=False, max_len=80) or None
    return {"session": session_to_dict(await session_manager.fork(source.id, title=title))}


async def session_delete(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    session = await gw.get_session(params)
    if gw.turn_running(session.id):
        raise RpcError(TURN_IN_PROGRESS, "stop the running reply before deleting this session")
    return {"deleted": await session_manager.delete(session.id)}


async def session_rename(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    session = await gw.get_session(params)
    await session_manager.set_title(session.id, _str(params, "title", max_len=80))
    return {"session": session_to_dict(await session_manager.get(session.id))}


async def session_set_goal(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    session = await gw.get_session(params)
    await session_manager.set_goal(session.id, _str(params, "goal", max_len=2000))
    return {"goal": (await session_manager.get(session.id)).goal}


async def session_search(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    found = await session_manager.search(
        _str(params, "query", max_len=200), limit=_int(params, "limit", 20, 1, 200)
    )
    return {"sessions": [session_to_dict(s) for s in found]}


async def session_export(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    return {"markdown": await services.export_markdown(await gw.get_session(params))}


# ─── context ──────────────────────────────────────────────────────────────────


async def context_get(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    session = await gw.get_session(params)
    chunks = await context_manager.get_chunks(session.id, limit=_int(params, "limit", 20, 1, 1000))
    return {
        "chunks": [
            {
                "type": c.chunk_type,
                "agent": c.agent_id,
                "tokens": c.token_count,
                "createdAt": _iso(c.created_at),
                "preview": chunk_preview(c),
            }
            for c in chunks
        ],
        "totalTokens": await context_manager.get_token_usage(session.id),
        "budget": session.context_budget,
        "goal": session.goal,
    }


async def context_compress(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    session = await gw.get_session(params)
    if gw.turn_running(session.id):
        raise RpcError(TURN_IN_PROGRESS, "stop the running reply before compressing")
    result = await services.compress_session(session, model=gw.model, provider=gw.provider)
    if result is None:
        return {"compressed": False}
    return {
        "compressed": True,
        "originalCount": result.original_count,
        "newCount": len(result.compressed_chunks),
        "originalTokens": result.original_tokens,
        "compressedTokens": result.compressed_tokens,
        "ratio": round(result.compression_ratio, 3),
        "method": result.method,
    }


# ─── memory ───────────────────────────────────────────────────────────────────


def _category(params: dict[str, Any], *, required: bool = False) -> str | None:
    value = _str(params, "category", required=required, max_len=20) or None
    if value is not None and value not in MEMORY_CATEGORIES:
        raise RpcError(INVALID_PARAMS, f"category must be one of: {', '.join(MEMORY_CATEGORIES)}")
    return value


async def memory_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.memory.store import memory_store

    memories = await memory_store.search(
        category=_category(params), limit=_int(params, "limit", 20, 1, 500)
    )
    return {"memories": [_memory(m) for m in memories], "total": await memory_store.count()}


async def memory_search(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.memory.retriever import MemoryRetriever

    retriever = MemoryRetriever(top_k=_int(params, "limit", 10, 1, 100))
    found = await retriever.retrieve(
        query=_str(params, "query", max_len=500), category=_category(params)
    )
    return {"results": [_memory(r.memory, r.score) for r in found]}


async def memory_add(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.memory.store import memory_store

    importance = params.get("importance", 0.7)
    if (
        not isinstance(importance, (int, float))
        or isinstance(importance, bool)
        or not 0 <= importance <= 1
    ):
        raise RpcError(INVALID_PARAMS, "importance must be a number between 0 and 1")
    session_id = _uuid(params, "sessionId") if params.get("sessionId") else None
    memory = await memory_store.add(
        session_id=session_id,
        agent_id=config.get("agent_id"),
        content=_str(params, "content", max_len=5000),
        category=_category(params) or "fact",
        importance=float(importance),
        explicitly_important=True,
    )
    return {"memory": _memory(memory)}


async def memory_forget(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.memory.store import memory_store

    return {"deleted": await memory_store.delete(_uuid(params, "id"))}


async def memory_pending(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.memory.approval import ApprovalStatus, memory_approval_gate

    pending = await memory_approval_gate.list_pending(
        status=ApprovalStatus.PENDING, limit=_int(params, "limit", 50, 1, 500)
    )
    return {"pending": [_pending(p) for p in pending]}


async def memory_approve(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.memory.approval import memory_approval_gate

    memory = await memory_approval_gate.approve(
        _uuid(params, "id"), review_note=_str(params, "note", required=False, max_len=500)
    )
    if memory is None:
        raise RpcError(NOT_FOUND, "pending memory not found or already reviewed")
    return {"memory": _memory(memory)}


async def memory_reject(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.memory.approval import memory_approval_gate

    ok = await memory_approval_gate.reject(
        _uuid(params, "id"), review_note=_str(params, "note", required=False, max_len=500)
    )
    if not ok:
        raise RpcError(NOT_FOUND, "pending memory not found or already reviewed")
    return {"rejected": True}


async def memory_approve_all(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.memory.approval import memory_approval_gate

    return {"count": await memory_approval_gate.approve_all()}


async def memory_reject_all(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.memory.approval import memory_approval_gate

    note = _str(params, "note", required=False, max_len=500)
    return {"count": await memory_approval_gate.reject_all(review_note=note)}


async def memory_stats(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.memory.approval import memory_approval_gate
    from ah.memory.store import memory_store

    return {"approval": await memory_approval_gate.get_stats(), "total": await memory_store.count()}


# ─── skills ───────────────────────────────────────────────────────────────────


def _registry():
    from ah.skills.registry import skill_registry

    skill_registry.load_all()
    return skill_registry


async def skills_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    return {"skills": [_skill(s) for s in sorted(_registry().list_skills(), key=lambda s: s.name)]}


async def skills_show(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    skill = _registry().get(_str(params, "name", max_len=200))
    if skill is None:
        raise RpcError(NOT_FOUND, f"skill {params.get('name')!r} not found")
    return {"skill": _skill(skill, content=True)}


async def skills_learn(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    triggers = params.get("triggers")
    if triggers is not None and (
        not isinstance(triggers, list) or not all(isinstance(t, str) for t in triggers)
    ):
        raise RpcError(INVALID_PARAMS, "triggers must be a list of strings")
    try:
        skill = services.learn_skill(
            _str(params, "source", max_len=2000),
            name=_str(params, "name", required=False, max_len=100) or None,
            description=_str(params, "description", required=False, max_len=500) or None,
            triggers=triggers,
        )
    except services.ServiceError as e:
        raise RpcError(OPERATION_FAILED, str(e)) from None
    return {"skill": _skill(skill)}


async def skills_delete(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    name = _str(params, "name", max_len=200)
    if not _registry().delete_skill(name):
        raise RpcError(NOT_FOUND, f"skill {name!r} not found")
    return {"deleted": True}


async def skills_curator(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    from ah.skills.registry import SkillCurator

    curator = SkillCurator(_registry())
    report = curator.get_health_report()
    report["unused"] = [s.name for s in curator.get_unused_skills()]
    report["stale"] = [s.name for s in curator.get_stale_skills(_int(params, "days", 30, 1, 3650))]
    return report


# ─── config, profiles, status ─────────────────────────────────────────────────


async def config_get(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    snapshot = _config_snapshot()
    snapshot["model"], snapshot["provider"] = gw.model, gw.provider
    return {"config": snapshot, "secrets": sorted(SECRET_KEYS)}


async def profile_get(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.memory.user_profile import user_profile_store

    profile = await user_profile_store.get_or_create(
        user_id=_str(params, "userId", max_len=200),
        display_name=_str(params, "displayName", required=False, max_len=200),
    )
    return {"profile": _profile(profile)}


async def profile_set(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.memory.user_profile import user_profile_store

    profile = await user_profile_store.get_or_create(user_id=_str(params, "userId", max_len=200))
    profile.set_preference(_str(params, "key", max_len=100), _str(params, "value", max_len=1000))
    updated = await user_profile_store.update_preferences(profile.id, profile.preferences)
    return {"profile": _profile(updated)}


async def profile_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.memory.user_profile import user_profile_store

    profiles = await user_profile_store.list_all(limit=_int(params, "limit", 50, 1, 500))
    return {"profiles": [_profile(p) for p in profiles]}


async def status(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    summary = await services.status_summary()
    summary["model"], summary["provider"] = gw.model, gw.provider
    return summary


# ─── agents (multi-agent) ───────────────────────────────────────────────────


def _agent(d: Any) -> dict[str, Any]:
    return d.to_dict()


async def agents_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.core.agent_def import agent_registry

    return {"agents": [_agent(a) for a in await agent_registry.list()]}


async def agents_save(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.core.agent_def import BUILTIN_AGENTS, AgentDef, agent_registry

    tools = params.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise RpcError(INVALID_PARAMS, "tools must be a list of strings")
    name = _str(params, "name", max_len=100)
    if name in BUILTIN_AGENTS:
        raise RpcError(INVALID_PARAMS, f"{name!r} is a built-in agent and cannot be saved")
    saved = await agent_registry.save(
        AgentDef(
            name=name,
            description=_str(params, "description", required=False, max_len=500),
            system_prompt=_str(params, "systemPrompt", required=False, max_len=10_000),
            tools=tools,
            model=_str(params, "model", required=False, max_len=200) or None,
            provider=_str(params, "provider", required=False, max_len=50) or None,
            max_iterations=_int(params, "maxIterations", 10, 1, 50),
        )
    )
    return {"agent": _agent(saved)}


async def agents_delete(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.core.agent_def import BUILTIN_AGENTS, agent_registry

    name = _str(params, "name", max_len=100)
    if name in BUILTIN_AGENTS:
        raise RpcError(INVALID_PARAMS, f"{name!r} is a built-in agent and cannot be deleted")
    if not await agent_registry.delete(name):
        raise RpcError(NOT_FOUND, f"agent {name!r} not found")
    return {"deleted": True}


def _delegation(r: Any) -> dict[str, Any]:
    return {
        "agent": r.agent,
        "task": r.task,
        "response": r.response,
        "sessionId": str(r.session_id),
        "tokens": r.tokens,
        "iterations": r.iterations,
        "status": r.status,
    }


def _steps(params: dict[str, Any]) -> list[tuple[str, str]]:
    raw = params.get("steps")
    if not isinstance(raw, list) or not raw:
        raise RpcError(INVALID_PARAMS, "steps must be a non-empty list of {agent, task}")
    steps: list[tuple[str, str]] = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("agent"), str)
            or not isinstance(item.get("task"), str)
        ):
            raise RpcError(INVALID_PARAMS, "each step needs a string 'agent' and 'task'")
        steps.append((item["agent"], item["task"]))
    return steps


async def agents_run(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Run delegations sequentially (default) or in parallel."""
    gw.require_db()
    from ah.core.orchestrator import AgentNotFoundError, orchestrator

    steps = _steps(params)
    mode = params.get("mode", "sequential")
    if mode not in ("sequential", "parallel"):
        raise RpcError(INVALID_PARAMS, "mode must be 'sequential' or 'parallel'")
    parent = _uuid(params, "sessionId") if params.get("sessionId") else None
    try:
        if mode == "parallel":
            results = await orchestrator.run_parallel(steps, parent_session_id=parent)
        else:
            results = await orchestrator.run_sequential(steps, parent_session_id=parent)
    except AgentNotFoundError as e:
        raise RpcError(NOT_FOUND, str(e)) from None
    serialized = []
    for (agent, task), result in zip(steps, results, strict=True):
        if isinstance(result, BaseException):
            serialized.append(
                {
                    "agent": agent,
                    "task": task,
                    "response": f"{type(result).__name__}: {result}",
                    "sessionId": None,
                    "tokens": 0,
                    "iterations": 0,
                    "status": "error",
                }
            )
        else:
            serialized.append(_delegation(result))
    return {"results": serialized}


async def agents_history(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.core.orchestrator import orchestrator

    session = await gw.get_session(params)
    return {
        "messages": await orchestrator.history(session.id, limit=_int(params, "limit", 50, 1, 500))
    }


# ─── scheduler (jobs) ────────────────────────────────────────────────────────


async def jobs_create(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.core.scheduler import DEFAULT_HEARTBEAT_PROMPT, job_store

    session = await gw.get_session(params)
    kind = params.get("kind", "interval")
    if kind not in ("heartbeat", "interval", "cron"):
        raise RpcError(INVALID_PARAMS, "kind must be 'heartbeat', 'interval', or 'cron'")
    prompt = (
        _str(params, "prompt", required=kind == "interval", max_len=5000)
        or DEFAULT_HEARTBEAT_PROMPT
    )
    try:
        job = await job_store.create(
            name=_str(params, "name", required=False, max_len=100) or f"{kind} job",
            kind=kind,
            session_id=session.id,
            prompt=prompt,
            interval_seconds=_int(params, "intervalSeconds", 300, 10, 86_400),
            agent_name=_str(params, "agent", required=False, max_len=100) or "harness",
            cron_expression=_str(
                params, "cronExpression", required=kind == "cron", max_len=100
            ) or None,
        )
    except ValueError as e:
        raise RpcError(INVALID_PARAMS, str(e)) from None
    return {"job": job.to_dict()}


async def jobs_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.core.scheduler import job_store

    session_id = _uuid(params, "sessionId") if params.get("sessionId") else None
    jobs = await job_store.list(session_id=session_id, limit=_int(params, "limit", 100, 1, 500))
    return {"jobs": [j.to_dict() for j in jobs]}


async def jobs_set_enabled(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.core.scheduler import job_store

    enabled = params.get("enabled")
    if not isinstance(enabled, bool):
        raise RpcError(INVALID_PARAMS, "enabled must be a boolean")
    job = await job_store.set_enabled(_uuid(params, "id"), enabled)
    if job is None:
        raise RpcError(NOT_FOUND, "job not found")
    return {"job": job.to_dict()}


async def jobs_delete(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from ah.core.scheduler import job_store

    if not await job_store.delete(_uuid(params, "id")):
        raise RpcError(NOT_FOUND, "job not found")
    return {"deleted": True}


METHODS: dict[str, Handler] = {
    "session.fork": session_fork,
    "session.delete": session_delete,
    "session.rename": session_rename,
    "session.setGoal": session_set_goal,
    "session.search": session_search,
    "session.export": session_export,
    "context.get": context_get,
    "context.compress": context_compress,
    "memory.list": memory_list,
    "memory.search": memory_search,
    "memory.add": memory_add,
    "memory.forget": memory_forget,
    "memory.pending": memory_pending,
    "memory.approve": memory_approve,
    "memory.reject": memory_reject,
    "memory.approveAll": memory_approve_all,
    "memory.rejectAll": memory_reject_all,
    "memory.stats": memory_stats,
    "skills.list": skills_list,
    "skills.show": skills_show,
    "skills.learn": skills_learn,
    "skills.delete": skills_delete,
    "skills.curator": skills_curator,
    "config.get": config_get,
    "profile.get": profile_get,
    "profile.set": profile_set,
    "profile.list": profile_list,
    "status": status,
    "agents.list": agents_list,
    "agents.save": agents_save,
    "agents.delete": agents_delete,
    "agents.run": agents_run,
    "agents.history": agents_history,
    "jobs.create": jobs_create,
    "jobs.list": jobs_list,
    "jobs.setEnabled": jobs_set_enabled,
    "jobs.delete": jobs_delete,
}


def register(gateway: Gateway) -> None:
    """Add every feature method to *gateway*'s dispatch table."""
    for name, handler in METHODS.items():
        gateway.add_method(name, lambda params, h=handler: h(gateway, params))
