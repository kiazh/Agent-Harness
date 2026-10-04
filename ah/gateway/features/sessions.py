"""Session-related feature handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ah import services
from ah.core.context import context_manager
from ah.core.session import session_manager
from ah.core.session_recall import SessionRecall
from ah.gateway.errors import TURN_IN_PROGRESS, RpcError
from ah.gateway.serializers import chunk_preview, json_safe_payload, session_to_dict

if TYPE_CHECKING:
    from ah.gateway.server import Gateway

from ah.gateway.features._common import _int, _str, _uuid


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


async def session_recall(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Find transcript evidence belonging to the current session's agent."""
    gw.require_db()
    session = await gw.get_session(params)
    hits = await SessionRecall().discover(
        session.agent_id,
        _str(params, "query", max_len=200),
        limit=_int(params, "limit", 20, 1, 100),
    )
    return {
        "hits": [
            {
                "sessionId": str(hit.session_id),
                "chunkId": str(hit.chunk_id) if hit.chunk_id else None,
                "title": hit.title,
                "source": hit.source,
                "preview": hit.preview,
                "score": hit.score,
                "occurredAt": hit.occurred_at.isoformat(),
            }
            for hit in hits
        ]
    }


async def session_recall_window(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Read a bounded window without trusting client-supplied agent scope."""
    gw.require_db()
    session = await gw.get_session(params)
    messages = await SessionRecall().window(
        session.agent_id,
        _uuid(params, "targetSessionId"),
        _uuid(params, "chunkId"),
        before=_int(params, "before", 5, 0, 20),
        after=_int(params, "after", 5, 0, 20),
    )
    return {
        "messages": [
            {
                "sessionId": str(message.session_id),
                "chunkId": str(message.chunk_id),
                "type": message.chunk_type,
                "source": message.source,
                "payload": json_safe_payload(message.payload),
                "occurredAt": message.occurred_at.isoformat(),
            }
            for message in messages
        ]
    }


async def session_export(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    return {"markdown": await services.export_markdown(await gw.get_session(params))}


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
                "createdAt": c.created_at.isoformat() if c.created_at else None,
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
