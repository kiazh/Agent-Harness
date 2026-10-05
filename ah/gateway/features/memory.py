"""Memory-related feature handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ah.core.config import config
from ah.gateway.errors import INVALID_PARAMS, NOT_FOUND, RpcError
from ah.gateway.features._common import MEMORY_CATEGORIES, _int, _memory, _pending, _str, _uuid

if TYPE_CHECKING:
    from ah.gateway.server import Gateway


def _category(params: dict[str, Any], *, required: bool = False) -> str | None:
    value = _str(params, "category", required=required, max_len=20) or None
    if value is not None and value not in MEMORY_CATEGORIES:
        raise RpcError(INVALID_PARAMS, f"category must be one of: {', '.join(MEMORY_CATEGORIES)}")
    return value


async def memory_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """List memories."""
    gw.require_db()
    from ah.memory.store import memory_store

    agent_id = (await gw.get_session(params)).agent_id if params.get("sessionId") else None
    memories = await memory_store.search(
        agent_id=agent_id, category=_category(params), limit=_int(params, "limit", 20, 1, 500)
    )
    return {
        "memories": [_memory(m) for m in memories],
        "total": await memory_store.count(agent_id=agent_id),
    }


async def memory_search(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Search memories."""
    gw.require_db()
    from ah.memory.retriever import MemoryRetriever

    retriever = MemoryRetriever(top_k=_int(params, "limit", 10, 1, 100))
    session = await gw.get_session(params) if params.get("sessionId") else None
    found = await retriever.retrieve(
        query=_str(params, "query", max_len=500),
        category=_category(params),
        agent_id=session.agent_id if session else None,
        session_id=session.id if session else None,
    )
    return {"results": [_memory(r.memory, r.score) for r in found]}


async def memory_add(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Add a memory."""
    gw.require_db()
    from ah.memory.store import memory_store

    importance = params.get("importance", 0.7)
    if (
        not isinstance(importance, (int, float))
        or isinstance(importance, bool)
        or not 0 <= importance <= 1
    ):
        raise RpcError(INVALID_PARAMS, "importance must be a number between 0 and 1")
    session = await gw.get_session(params) if params.get("sessionId") else None
    memory = await memory_store.add(
        session_id=session.id if session else None,
        agent_id=session.agent_id if session else config.get("agent_id"),
        content=_str(params, "content", max_len=5000),
        category=_category(params) or "fact",
        importance=float(importance),
        explicitly_important=True,
    )
    return {"memory": _memory(memory)}


async def memory_share(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Deliver one owned memory through the recipient's identity gate."""
    gw.require_db()
    from ah.memory.shared_bus import shared_memory_bus

    session = await gw.get_session(params)
    try:
        receipt = await shared_memory_bus.deliver(
            _uuid(params, "id"),
            _str(params, "recipientAgent", max_len=128),
            publisher_agent=session.agent_id,
        )
    except PermissionError:
        raise RpcError(NOT_FOUND, "memory not found for this agent") from None
    except ValueError as exc:
        raise RpcError(INVALID_PARAMS, str(exc)) from None
    return {
        "sourceMemoryId": str(receipt.source_memory_id),
        "recipientAgent": receipt.recipient_agent,
        "targetMemoryId": str(receipt.target_memory_id) if receipt.target_memory_id else None,
        "status": receipt.status,
        "reason": receipt.reason,
    }


async def memory_forget(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Forget a memory."""
    gw.require_db()
    from ah.memory.store import memory_store

    memory_id = _uuid(params, "id")
    if params.get("sessionId"):
        agent_id = (await gw.get_session(params)).agent_id
        memory = await memory_store.get(memory_id)
        if memory is None or memory.agent_id != agent_id:
            raise RpcError(NOT_FOUND, "memory not found for this agent")
    return {"deleted": await memory_store.delete(memory_id)}


async def memory_pending(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """List pending memories."""
    gw.require_db()
    from ah.memory.approval import ApprovalStatus, memory_approval_gate

    pending = await memory_approval_gate.list_pending(
        status=ApprovalStatus.PENDING, limit=_int(params, "limit", 50, 1, 500)
    )
    return {"pending": [_pending(p) for p in pending]}


async def memory_approve(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Approve a pending memory."""
    gw.require_db()
    from ah.memory.approval import memory_approval_gate

    memory = await memory_approval_gate.approve(
        _uuid(params, "id"), review_note=_str(params, "note", required=False, max_len=500)
    )
    if memory is None:
        raise RpcError(NOT_FOUND, "pending memory not found or already reviewed")
    return {"memory": _memory(memory)}


async def memory_reject(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Reject a pending memory."""
    gw.require_db()
    from ah.memory.approval import memory_approval_gate

    ok = await memory_approval_gate.reject(
        _uuid(params, "id"), review_note=_str(params, "note", required=False, max_len=500)
    )
    if not ok:
        raise RpcError(NOT_FOUND, "pending memory not found or already reviewed")
    return {"rejected": True}


async def memory_approve_all(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Approve all pending memories."""
    gw.require_db()
    from ah.memory.approval import memory_approval_gate

    return {"count": await memory_approval_gate.approve_all()}


async def memory_reject_all(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Reject all pending memories."""
    gw.require_db()
    from ah.memory.approval import memory_approval_gate

    note = _str(params, "note", required=False, max_len=500)
    return {"count": await memory_approval_gate.reject_all(review_note=note)}


async def memory_share(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Deliver one owned memory through the recipient's identity gate."""
    gw.require_db()
    from ah.memory.shared_bus import shared_memory_bus

    session = await gw.get_session(params)
    try:
        receipt = await shared_memory_bus.deliver(
            _uuid(params, "id"),
            _str(params, "recipientAgent", max_len=128),
            publisher_agent=session.agent_id,
        )
    except PermissionError:
        raise RpcError(NOT_FOUND, "memory not found for this agent") from None
    except ValueError as exc:
        raise RpcError(INVALID_PARAMS, str(exc)) from None
    return {
        "sourceMemoryId": str(receipt.source_memory_id),
        "recipientAgent": receipt.recipient_agent,
        "targetMemoryId": str(receipt.target_memory_id) if receipt.target_memory_id else None,
        "status": receipt.status,
        "reason": receipt.reason,
    }


async def memory_stats(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Get memory statistics."""
    gw.require_db()
    from ah.memory.approval import memory_approval_gate
    from ah.memory.store import memory_store

    return {"approval": await memory_approval_gate.get_stats(), "total": await memory_store.count()}
