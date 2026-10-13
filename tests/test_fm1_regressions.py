"""FM1 regressions: turn-lifecycle cleanup, token gate, bounded resume, embedded auth.

Covers:
- W1: early-return turn paths (session not found / agent factory unavailable)
  must run the SAME cleanup as normal completion: stop+join renewal, remove the
  growth listener, pop ``_turn_ownership_lost``, discard ``_cancel_requested``,
  and emit the single terminal ``message.complete`` only AFTER claim release.
- W2: ``_turn_stages`` is retired with the turn (no unbounded growth).
- W4: a state-changing ``initialize`` requires the gateway token; the bare
  handshake stays free.
- W5: ``session.resume`` history is bounded like the REST endpoint (200).
- W8: the embedded HTTP gateway never inherits ``MACHINA_GATEWAY_TOKEN``
  (API-key auth already happened at the HTTP boundary).
- W7: startup orphan-reservation cleanup uses the documented 10-minute grace.
- N3: ``persist`` is parsed strictly ("false" is False, not truthy).
- N4: ``session.setModel`` validates against ``machina.core.provider.PROVIDERS``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from machina.core.config import config
from machina.core.models import AgentResponse, ContextChunk, Session, StreamEvent
from machina.gateway.errors import UNAUTHORIZED, RpcError
from machina.gateway.server import Gateway

# ─── helpers ────────────────────────────────────────────────────────────────


def _frames():
    frames: list = []
    return frames, frames.append


async def _rpc(gw: Gateway, frames: list, rid: int, method: str, params: dict | None = None):
    frame: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        frame["params"] = params
    await gw.handle_line(json.dumps(frame))
    return next(
        f for f in reversed(frames) if f.get("id") == rid and "method" not in f
    )


def _event_types(frames: list) -> list[str]:
    return [f["params"]["type"] for f in frames if f.get("method") == "event"]


class _OneShotAgent:
    _learning_tasks: tuple = ()

    async def run_stream(self, session_id, user_message, verbose=False, **_kw):
        yield StreamEvent(
            type="done",
            response=AgentResponse(content="ok", tool_calls=[], tokens_used=1, iterations=1),
        )


# ─── W1: early-return cleanup + terminal ordering ───────────────────────────


async def test_factory_unavailable_releases_before_terminal_and_leaks_nothing(monkeypatch):
    """W1 (factory path): release runs before the terminal event; no leaks.

    Pre-fix: ``complete()`` fired before the finally, the growth listener and
    ``_turn_ownership_lost`` entry leaked, and the ORDER was terminal→release.
    """
    import machina.core.agent as agent_module
    from machina.core import turns
    from machina.core.session import session_manager

    monkeypatch.delenv("MACHINA_GATEWAY_TOKEN", raising=False)
    session = Session(id=uuid.uuid4())
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))

    order: list[str] = []
    released: list = []

    async def fake_end_turn(session_id, token):
        released.append((session_id, token))
        order.append("release")
        return True

    monkeypatch.setattr(turns, "end_turn", fake_end_turn)
    monkeypatch.setattr(turns, "renew_turn", AsyncMock(return_value=True))

    added: list = []
    removed: list = []
    monkeypatch.setattr(agent_module, "add_growth_listener", lambda fn: added.append(fn))
    monkeypatch.setattr(agent_module, "remove_growth_listener", lambda fn: removed.append(fn))

    frames: list = []

    def write(frame: dict) -> None:
        params = frame.get("params") or {}
        if frame.get("method") == "event" and params.get("type") == "message.complete":
            order.append("terminal")
        frames.append(frame)

    # Force the fail-closed import branch: module present but poisoned.
    monkeypatch.setitem(sys.modules, "machina.core.agent_factory", None)

    gw = Gateway(write, owns_db=False)
    sid = str(session.id)
    turn_id = "w1-factory-turn"
    gw._turn_tokens[sid] = "w1-owner-token"
    gw._cancel_requested.add(turn_id)

    await gw._run_turn(session.id, turn_id, "hello")

    assert released == [(session.id, "w1-owner-token")], "claim release ran exactly once"
    assert order == ["release", "terminal"], f"terminal must follow release: {order}"
    assert _event_types(frames).count("message.complete") == 1
    assert "turn.ownership_lost" not in _event_types(frames)
    assert turn_id not in gw._turn_ownership_lost, "ownership-lost entry leaked"
    assert turn_id not in gw._cancel_requested, "cancel flag leaked"
    assert gw._turn_tokens.get(sid) is None, "owner token leaked"
    assert len(added) == 1 and removed == added, "growth listener leaked"


async def test_session_not_found_early_return_cleans_up_and_terminates(monkeypatch):
    """W1 (session path): still one terminal, but cleanup state is dropped.

    Pre-fix: the growth listener, ``_turn_ownership_lost`` entry and
    ``_cancel_requested`` flag all leaked because the return skipped the
    post-finally cleanup block.
    """
    import machina.core.agent as agent_module
    from machina.core.session import session_manager

    monkeypatch.delenv("MACHINA_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=None))

    added: list = []
    removed: list = []
    monkeypatch.setattr(agent_module, "add_growth_listener", lambda fn: added.append(fn))
    monkeypatch.setattr(agent_module, "remove_growth_listener", lambda fn: removed.append(fn))

    frames, write = _frames()
    gw = Gateway(write, owns_db=False)
    turn_id = "w1-missing-turn"
    gw._cancel_requested.add(turn_id)

    await gw._run_turn(uuid.uuid4(), turn_id, "hello")

    kinds = _event_types(frames)
    assert kinds.count("message.complete") == 1
    assert "error" in kinds, "the missing session must still surface an error"
    assert "turn.ownership_lost" not in kinds
    assert turn_id not in gw._turn_ownership_lost
    assert turn_id not in gw._cancel_requested
    assert len(added) == 1 and removed == added


async def test_release_failure_drops_listener_without_terminal(monkeypatch):
    """W1 (release path): when release fails there is no terminal, but the
    always-run cleanup still drops the growth listener / ownership entry.

    Pre-fix: the ``_release_failed`` return skipped ``remove_growth_listener``
    (leak) and the ownership-lost pop lived in a block the early return never
    reached.
    """
    import machina.core.agent as agent_module
    from machina.core import turns
    from machina.core.session import session_manager

    monkeypatch.delenv("MACHINA_GATEWAY_TOKEN", raising=False)
    session = Session(id=uuid.uuid4())
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))

    async def broken_end_turn(_session_id, _token):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(turns, "end_turn", broken_end_turn)
    monkeypatch.setattr(turns, "renew_turn", AsyncMock(return_value=True))

    added: list = []
    removed: list = []
    monkeypatch.setattr(agent_module, "add_growth_listener", lambda fn: added.append(fn))
    monkeypatch.setattr(agent_module, "remove_growth_listener", lambda fn: removed.append(fn))

    frames, write = _frames()
    gw = Gateway(write, agent_factory=lambda _m, _p: _OneShotAgent(), owns_db=False)
    sid = str(session.id)
    turn_id = "w1-release-failed"
    gw._turn_tokens[sid] = "w1-stuck-token"

    await gw._run_turn(session.id, turn_id, "hello")

    kinds = _event_types(frames)
    assert "turn.cleanup_pending" in kinds
    assert "message.complete" not in kinds, "terminal must not claim an unreleased turn"
    assert turn_id not in gw._turn_ownership_lost
    assert turn_id not in gw._cancel_requested
    assert len(added) == 1 and removed == added, "growth listener leaked on release failure"
    # The owner token is preserved for DB reconciliation / TTL expiry.
    assert gw._turn_tokens.get(sid) == "w1-stuck-token"


# ─── W2: stage tracker retirement ───────────────────────────────────────────


async def test_retired_turns_drop_stage_tracker(monkeypatch):
    """W2: ``retired()`` pops ``_turn_stages`` so it cannot grow forever."""
    from machina import services
    from machina.core.session import session_manager

    monkeypatch.delenv("MACHINA_GATEWAY_TOKEN", raising=False)
    session = Session(id=uuid.uuid4())
    monkeypatch.setattr(session_manager, "get_fresh", AsyncMock(return_value=session))
    monkeypatch.setattr(services, "maybe_auto_compact", AsyncMock())

    gw = Gateway(lambda _frame: None, agent_factory=lambda _m, _p: _OneShotAgent(), owns_db=False)
    await gw._run_turn_with_timeout(session.id, "w2-turn", "hello")
    for _ in range(3):
        await asyncio.sleep(0)

    assert "w2-turn" not in gw._turn_stages
    assert "w2-turn" not in gw._turn_progress
    assert gw._turn_stages == {}


# ─── W4: state-changing initialize requires the token ───────────────────────


async def test_stateful_initialize_requires_token_but_bare_handshake_is_free(monkeypatch):
    """W4: bare initialize authenticates; model/provider/mode need the token.

    Pre-fix: ``initialize`` was transport-exempt AND unchecked, so an
    unauthenticated caller could set model/provider/execution_mode (incl.
    full).
    """
    monkeypatch.delenv("MACHINA_GATEWAY_TOKEN", raising=False)
    frames, write = _frames()
    gw = Gateway(write, owns_db=False)
    gw._db_ready = True
    gw._auth_token = "init-secret"
    before_model, before_provider = gw.model, gw.provider

    # Bare handshake stays free.
    response = await _rpc(gw, frames, 1, "initialize", {"protocolVersion": 1})
    assert "result" in response, response

    # State change without a token: rejected and mutates nothing.
    response = await _rpc(gw, frames, 2, "initialize", {"model": "attacker/model"})
    assert response.get("error", {}).get("code") == UNAUTHORIZED, response
    assert gw.model == before_model

    # State change with a wrong token: rejected.
    response = await _rpc(gw, frames, 3, "initialize", {"provider": "ollama", "token": "nope"})
    assert response.get("error", {}).get("code") == UNAUTHORIZED, response
    assert gw.provider == before_provider

    # Correct token: accepted.
    response = await _rpc(
        gw,
        frames,
        4,
        "initialize",
        {"model": "fine/model", "provider": "ollama", "token": "init-secret"},
    )
    assert "result" in response, response
    assert gw.model == "fine/model"
    assert gw.provider == "ollama"


# ─── W5: bounded resume history ─────────────────────────────────────────────


async def test_session_resume_history_is_bounded_like_rest(monkeypatch):
    """W5: resume history is capped at the newest 200 chunks.

    Pre-fix: ``get_transcript_chunks`` (no LIMIT) was fully serialized into
    the response.
    """
    from machina.core.context import context_manager
    from machina.core.session import session_manager

    monkeypatch.delenv("MACHINA_GATEWAY_TOKEN", raising=False)
    session = Session(id=uuid.uuid4())
    # Newest first: index 0 is "m-299", index 299 is "m-0".
    chunks = [
        ContextChunk(
            id=uuid.uuid4(),
            session_id=session.id,
            agent_id="harness",
            chunk_type="user_message",
            payload={"content": f"m-{299 - index}"},
        )
        for index in range(300)
    ]
    monkeypatch.setattr(session_manager, "get", AsyncMock(return_value=session))
    monkeypatch.setattr(
        context_manager, "get_transcript_chunks", AsyncMock(return_value=chunks)
    )

    gw = Gateway(lambda _frame: None, owns_db=False)
    gw._db_ready = True
    result = await gw._session_resume({"sessionId": str(session.id)})

    history = result["history"]
    assert len(history) == 200, f"resume returned {len(history)} history entries"
    assert history[0]["content"] == "m-100", "the OLDEST retained entry is wrong"
    assert history[-1]["content"] == "m-299", "the newest entry must be retained"


# ─── W8: embedded HTTP gateway never inherits the stdio token ───────────────


async def test_embedded_rpc_gateway_ignores_gateway_env_token(monkeypatch):
    """W8: /api/v1/rpc already passed API-key auth; env token must not 401 it.

    Pre-fix: ``_RpcGateway`` constructed ``Gateway`` which read
    ``MACHINA_GATEWAY_TOKEN`` from the environment, so every RPC call
    (initialize excepted) answered 401.
    """
    from machina.api.app import _RpcGateway
    from machina.gateway.errors import DATABASE_UNAVAILABLE

    monkeypatch.setenv("MACHINA_GATEWAY_TOKEN", "stdio-only-token")
    rpc = _RpcGateway()
    rpc._gateway._db_ready = False

    assert rpc._gateway._auth_token is None, "embedded gateway inherited the stdio token"

    with pytest.raises(RpcError) as excinfo:
        await rpc.call("session.list", {})
    assert excinfo.value.code == DATABASE_UNAVAILABLE
    assert excinfo.value.code != UNAUTHORIZED


async def test_embedded_rpc_gateway_initialize_does_not_re_enable_env_token(monkeypatch):
    """W8: even a late env token is not picked up by an embedded initialize."""
    import machina.db.connection as db_mod
    from machina.api.app import _RpcGateway
    from machina.core.usage import usage_store

    monkeypatch.delenv("MACHINA_GATEWAY_TOKEN", raising=False)
    rpc = _RpcGateway()
    rpc._gateway._db_ready = False
    # Token appears after construction (e.g. another component loads .env).
    monkeypatch.setenv("MACHINA_GATEWAY_TOKEN", "late-stdio-token")
    monkeypatch.setenv("MACHINA_GATEWAY_NO_SCHEDULER", "1")
    monkeypatch.setattr(db_mod.db, "connect", AsyncMock())
    monkeypatch.setattr(db_mod.db, "initialize_runtime_schema", AsyncMock())
    monkeypatch.setattr(
        usage_store, "cleanup_orphaned_reservations", AsyncMock(return_value=0)
    )

    result = await rpc.call("initialize", {"protocolVersion": 2, "model": "embedded-model"})

    assert result["model"] == "embedded-model"
    assert rpc._gateway._auth_token is None


# ─── W7: startup reaps only stale reservations ──────────────────────────────


async def test_gateway_startup_reaps_only_stale_reservations(monkeypatch):
    """W7: startup must pass the documented 10-minute grace.

    Pre-fix: default ``older_than_minutes=0`` reaped every 'reserved' row,
    including in-flight calls owned by other worker processes.
    """
    import machina.db.connection as db_mod
    from machina.core.usage import usage_store

    monkeypatch.delenv("MACHINA_GATEWAY_TOKEN", raising=False)
    monkeypatch.setenv("MACHINA_GATEWAY_NO_SCHEDULER", "1")
    cleanup = AsyncMock(return_value=0)
    monkeypatch.setattr(usage_store, "cleanup_orphaned_reservations", cleanup)
    monkeypatch.setattr(db_mod.db, "connect", AsyncMock())
    monkeypatch.setattr(db_mod.db, "initialize_runtime_schema", AsyncMock())

    gw = Gateway(lambda _frame: None, owns_db=False)
    gw._db_ready = False
    await gw._initialize({})

    cleanup.assert_awaited_once_with(older_than_minutes=10)


# ─── N3: strict persist parsing at both call sites ──────────────────────────


async def test_config_set_persist_false_string_is_not_truthy(monkeypatch):
    """N3: ``persist: "false"`` must not be coerced to True."""
    calls: list = []
    monkeypatch.setattr(config, "set", lambda key, value, persist=False: calls.append(persist))

    gw = Gateway(lambda _frame: None, owns_db=False)
    await gw._config_set({"key": "theme", "value": "dark", "persist": "false"})

    assert calls == [False], f'persist="false" parsed as {calls}'


async def test_mode_set_persist_false_string_is_not_truthy(monkeypatch):
    """N3: the mode feature had the same truthiness bug."""
    from machina.gateway.features.mode import mode_set

    calls: list = []
    monkeypatch.setattr(config, "set", lambda key, value, persist=False: calls.append(persist))

    gw = SimpleNamespace(require_db=lambda: None)
    result = await mode_set(gw, {"mode": "workspace", "scope": "global", "persist": "false"})

    assert result["mode"] == "workspace"
    assert calls == [False], f'persist="false" parsed as {calls}'


async def test_persist_garbage_is_rejected(monkeypatch):
    """N3: non-boolean persist values fail loudly instead of coercing."""
    from machina.gateway.features.mode import mode_set

    gw = SimpleNamespace(require_db=lambda: None)
    with pytest.raises(RpcError):
        await mode_set(gw, {"mode": "workspace", "scope": "global", "persist": "maybe"})


# ─── N4: provider registry is the single source of truth ────────────────────


async def test_session_set_model_uses_provider_registry(monkeypatch):
    """N4: the handler must read PROVIDERS, not a hardcoded copy.

    Pre-fix: a provider present in PROVIDERS but absent from the stale
    hardcoded tuple was rejected.
    """
    from machina.gateway.features import sessions as feature

    session = SimpleNamespace(
        id=uuid.uuid4(), agent_id="harness", model=None, provider="testprov"
    )
    set_model = AsyncMock()
    monkeypatch.setattr(feature.session_manager, "set_model", set_model)
    monkeypatch.setattr(feature.session_manager, "get", AsyncMock(return_value=session))
    monkeypatch.setattr(feature, "PROVIDERS", ("testprov",), raising=False)
    gw = SimpleNamespace(
        require_db=lambda: None, get_session=AsyncMock(return_value=session)
    )

    result = await feature.session_set_model(
        gw, {"sessionId": str(session.id), "provider": "testprov"}
    )
    assert result["provider"] == "testprov"
    set_model.assert_awaited_once_with(session.id, model=None, provider="testprov")

    # A provider the registry does not know stays rejected.
    with pytest.raises(RpcError):
        await feature.session_set_model(
            gw, {"sessionId": str(session.id), "provider": "openai"}
        )
