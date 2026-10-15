"""R5-QR route proof: full-UUID memory.forget/share/approve resolve directly.

The UI bypasses the newest-N window only because the backend handlers accept
a bare UUID and enforce ownership themselves. This probe drives the REAL
gateway handlers with stub stores and asserts:
  - memory_forget honors a full UUID with no window/list step, and rejects
    foreign-agent IDs with NOT_FOUND (ownership check);
  - memory_share forwards a full UUID to the identity-gated bus, mapping
    PermissionError to NOT_FOUND;
  - memory_approve forwards a full UUID to the approval gate (no pending-list
    step);
  - non-UUID ids are rejected with INVALID_PARAMS at the route.
"""

import asyncio
import sys
import types
import uuid
from unittest.mock import AsyncMock

from machina.gateway.errors import INVALID_PARAMS, NOT_FOUND, RpcError
from machina.gateway.features import memory as mem


class Session:
    def __init__(self, agent_id):
        self.agent_id = agent_id
        self.id = uuid.uuid4()


class FakeGW:
    def __init__(self, agent_id="agent-owner"):
        self._agent_id = agent_id

    def require_db(self):
        pass

    async def get_session(self, params):
        return Session(self._agent_id)


def install_stub(module_name, attr, stub):
    mod = sys.modules.get(module_name)
    if mod is None:
        mod = types.ModuleType(module_name)
        sys.modules[module_name] = mod
    setattr(mod, attr, stub)
    return stub


async def main():
    gw = FakeGW()
    OLD = "aaaaaaaa-1111-2222-3333-444444444444"

    # memory.forget: owned full UUID, no window involved.
    store = types.SimpleNamespace(
        get=AsyncMock(return_value=types.SimpleNamespace(id=uuid.UUID(OLD), agent_id="agent-owner")),
        delete=AsyncMock(return_value=True),
    )
    install_stub("machina.memory.store", "memory_store", store)
    try:
        import machina.memory.store as store_mod

        store_mod.memory_store = store
    except Exception:
        pass
    out = await mem.memory_forget(gw, {"sessionId": "s", "id": OLD})
    assert out == {"deleted": True}, out
    store.get.assert_awaited_once_with(uuid.UUID(OLD))
    store.delete.assert_awaited_once_with(uuid.UUID(OLD))
    print("forget owned full-UUID: deleted=True, no window lookup exists on this route")

    # memory.forget: foreign agent -> NOT_FOUND (ownership check).
    store.get = AsyncMock(return_value=types.SimpleNamespace(id=uuid.UUID(OLD), agent_id="someone-else"))
    try:
        await mem.memory_forget(gw, {"sessionId": "s", "id": OLD})
    except RpcError as exc:
        assert exc.code == NOT_FOUND, exc.code
        print(f"forget foreign-agent full-UUID: NOT_FOUND ({exc.message})")
    else:
        raise AssertionError("foreign-agent forget must raise NOT_FOUND")

    # memory.forget: garbage id -> INVALID_PARAMS.
    try:
        await mem.memory_forget(gw, {"sessionId": "s", "id": "deadbeef"})
    except RpcError as exc:
        assert exc.code == INVALID_PARAMS, exc.code
        print(f"forget non-UUID: INVALID_PARAMS ({exc.message})")
    else:
        raise AssertionError("non-UUID forget must raise INVALID_PARAMS")

    # memory.share: full UUID forwarded to the identity-gated bus.
    receipt = types.SimpleNamespace(
        source_memory_id=uuid.UUID(OLD),
        recipient_agent="teammate",
        target_memory_id=None,
        status="delivered",
        reason=None,
    )
    bus = types.SimpleNamespace(deliver=AsyncMock(return_value=receipt))
    install_stub("machina.memory.shared_bus", "shared_memory_bus", bus)
    try:
        import machina.memory.shared_bus as bus_mod

        bus_mod.shared_memory_bus = bus
    except Exception:
        pass
    out = await mem.memory_share(gw, {"sessionId": "s", "id": OLD, "recipientAgent": "teammate"})
    assert out["status"] == "delivered", out
    bus.deliver.assert_awaited_once()
    print("share full-UUID: forwarded to identity-gated bus, no window lookup")

    # memory.share: bus PermissionError -> NOT_FOUND.
    bus.deliver = AsyncMock(side_effect=PermissionError("nope"))
    try:
        await mem.memory_share(gw, {"sessionId": "s", "id": OLD, "recipientAgent": "teammate"})
    except RpcError as exc:
        assert exc.code == NOT_FOUND, exc.code
        print(f"share foreign full-UUID: NOT_FOUND ({exc.message})")
    else:
        raise AssertionError("foreign share must raise NOT_FOUND")

    # memory.approve: full UUID forwarded directly (no pending-list step).
    approved = types.SimpleNamespace(
        id=uuid.UUID(OLD), content="c", category="fact", importance=0.5,
        access_count=0, created_at=None, session_id=None,
    )
    gate = types.SimpleNamespace(approve=AsyncMock(return_value=approved))
    install_stub("machina.memory.approval", "memory_approval_gate", gate)
    try:
        import machina.memory.approval as approval_mod

        approval_mod.memory_approval_gate = gate
    except Exception:
        pass
    out = await mem.memory_approve(gw, {"id": OLD})
    assert out["memory"]["id"] == OLD, out
    gate.approve.assert_awaited_once()
    print("approve full-UUID: forwarded to approval gate, no pending-window lookup")

    print("R5-QR route proof: ALL OK")


asyncio.run(main())
