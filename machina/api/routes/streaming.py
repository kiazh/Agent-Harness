"""SSE streaming for session turns (moved verbatim from app.py).

Shared by the sessions router (prompt/chat) and the approvals router
(approval resume): both stream agent events as Server-Sent Events through
:func:`_stream_session`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from machina.api.routes.models import PromptRequest
from machina.api.routes.shared import _redact_value, _serialize_event
from machina.core.background_tasks import join_agent_background_tasks
from machina.core.config import config
from machina.core.exceptions import StaleOwnershipError
from machina.core.models import StreamEvent
from machina.core.session import session_manager
from machina.memory.redaction import StreamingSecretRedactor
from machina.observability.diagnostics import record_failure

logger = logging.getLogger(__name__)


async def _stream_session(
    session_id: str, req: PromptRequest, *, resume: tuple[dict, dict] | None = None
) -> StreamingResponse:
    """Stream agent events as Server-Sent Events."""
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(400, "invalid session ID") from None
    # AH-AUDIT-019: execution-critical read bypasses the display cache.
    session = await session_manager.get_fresh(sid)
    if session is None:
        raise HTTPException(404, "session not found")
    # Shared coordinator: owner-token claim BEFORE SSE headers so a
    # conflicting turn gets a clean 409 instead of an interleaved stream.
    # Fails closed: claim errors propagate, never concurrent execution.
    from machina.core.turns import end_turn as _end_turn
    from machina.core.turns import try_begin_turn as _try_begin

    turn_token = await _try_begin(sid)
    if not turn_token:
        raise HTTPException(409, "a turn is already running for this session")

    # AH-AUDIT-013: managed renewal while the stream owns the turn
    # (compute only here — headless SSE never waits on humans, so no
    # approval accounting is needed on this path). Created lazily on
    # first iteration so an unconsumed stream never renews forever;
    # stopped when the generator exits; crash expiry recovery preserved.
    _renew_state: dict[str, Any] = {}
    _ownership_lost = asyncio.Event()
    # The renewal interval is owned by machina.api.app (kept there as a
    # patch target). Read it off the loaded app module instead of
    # importing it: a function-level import would add a deferred-import
    # census entry and a module-level one would cycle back into app.
    # Resolved per stream so monkeypatching the app attribute keeps
    # working after the router split.
    _app_module = sys.modules.get("machina.api.app")
    _renew_interval = getattr(_app_module, "CLAIM_RENEW_INTERVAL_SECONDS", 30.0)

    async def _owned_step(awaitable, timeout: float):
        task = asyncio.ensure_future(awaitable)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        _renew_state["active"] = task
        lost = asyncio.create_task(_ownership_lost.wait())
        try:
            done, _ = await asyncio.wait(
                {task, lost}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if _ownership_lost.is_set():
                task.cancel()
                raise StaleOwnershipError("REST turn ownership lost")
            if task not in done:
                task.cancel()
                raise TimeoutError("turn timed out")
            return task.result()
        finally:
            lost.cancel()
            if task.done():
                _renew_state.pop("active", None)

    async def _ensure_renewal() -> None:
        if _renew_state.get("task") is not None:
            return
        from machina.core.turns import TurnOwnership as _TurnOwnership

        owner = _TurnOwnership(
            sid, str(turn_token or ""), turn_token, ownership_lost=_ownership_lost
        )

        def _on_lost() -> None:
            active = _renew_state.get("active")
            if active is not None:
                active.cancel()

        _renew_state["task"] = owner.start_renewal(interval_s=_renew_interval, on_lost=_on_lost)

    async def _stop_renewal() -> None:
        _renew_task = _renew_state.pop("task", None)
        if _renew_task is not None and not _renew_task.done():
            _renew_task.cancel()
        if _renew_task is not None:
            try:
                await asyncio.gather(_renew_task, return_exceptions=True)
            except (asyncio.CancelledError, Exception) as _boundary_error:
                # Optional fallback preserves the primary outcome; report no payload.
                record_failure("app.create_app", _boundary_error)

    def frame(data: dict[str, Any]) -> str:
        if req.protocolVersion == 2:
            from machina.protocol import canonical_http_event

            data = canonical_http_event(data, session_id, turn_token)
        return f"data: {json.dumps(data)}\n\n"

    async def event_stream() -> AsyncGenerator[str, None]:
        agent = None
        stream = None
        terminal_data = None
        resume_token = None
        _stream_cancelled = False
        _release_failed = False
        _stage = "turn_started"
        await _ensure_renewal()

        def _record_stage(stage: str) -> None:
            nonlocal _stage
            _stage = stage
            logger.debug("Session %s stage: %s", session_id, stage)

        # Boundary-safe streaming redaction: hold back a tail across
        # deltas so a secret split at any boundary never leaks (AH-001).
        # Final flush redacts the remainder; the done event carries the
        # fully-redacted concatenation, never the raw accumulation.
        stream_redactor = StreamingSecretRedactor()
        try:
            from machina.permissions.broker import set_approval_handler

            set_approval_handler(None, principal="http", turn_id=turn_token)
            yield frame({"type": "turn.started", "sessionId": session_id, "turnId": turn_token})
            # AH-022: definition-aware factory so specialist sessions
            # cannot bypass allowed_tools/persona via direct HTTP.
            from machina.core.agent_factory import build_agent_for_session

            _record_stage("building_agent")
            agent = await _owned_step(build_agent_for_session(session), 300)
            _record_stage("waiting_for_provider")
            # Total turn timeout: a per-anext wait_for against a fixed
            # deadline so a hung provider cannot hold the stream forever.
            try:
                timeout = float(config.get("turn_timeout") or 300)
            except (TypeError, ValueError):
                timeout = 300.0
            deadline = asyncio.get_running_loop().time() + timeout
            if resume is None:
                stream = agent.run_stream(sid, req.text, verbose=req.verbose)
            else:
                from machina.core.approval_resume import resume_action_stream
                from machina.permissions.broker import resume_approval_var

                record, arguments = resume
                resume_token = resume_approval_var.set(record["request_id"])
                yield frame(
                    {
                        "type": "approval.resumed",
                        "requestId": record["request_id"],
                        "sessionId": session_id,
                        "turnId": turn_token,
                        "originTurnId": record["turn_id"],
                    }
                )
                stream = resume_action_stream(agent, sid, record, arguments)
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("turn timed out")
                try:
                    event = await _owned_step(stream.__anext__(), remaining)
                except StopAsyncIteration:
                    break
                if event.type == "text":
                    # Emit only the safe prefix; the tail stays buffered
                    # until more deltas arrive or flush() runs at done.
                    safe = stream_redactor.feed(event.content or "")
                    if safe:
                        data = _serialize_event(
                            StreamEvent(type="text", content=safe),
                            _pre_redacted=True,
                        )
                        yield frame(data)
                    continue
                if event.type == "done" and event.response is not None:
                    tail = stream_redactor.flush()
                    if tail:
                        # The tail was withheld from earlier deltas; emit
                        # it now (redacted) before the terminal event.
                        data = _serialize_event(
                            StreamEvent(type="text", content=tail),
                            _pre_redacted=True,
                        )
                        yield frame(data)
                    # AH-AUDIT-030: typed needs_approval SSE output with
                    # request/session/turn binding and the sanitized
                    # proposal. A pause is actionable and distinct from
                    # failure; resume revalidates under a fresh fenced
                    # claim (never by continuing stale generator state).
                    for pause in getattr(event.response, "needs_approval", None) or []:
                        if not isinstance(pause, dict) or not pause.get("request_id"):
                            continue
                        pause_event = StreamEvent(
                            type="needs_approval",
                            content=str(pause.get("request_id", "")),
                            tool_name=str(pause.get("operation", "")),
                            tool_args={
                                "requestId": str(pause.get("request_id", "")),
                                "sessionId": str(pause.get("session_id", "") or session_id),
                                "operation": str(pause.get("operation", "")),
                                "target": str(pause.get("target", "")),
                                "status": str(pause.get("status", "pending")),
                            },
                        )
                        data = _serialize_event(pause_event)
                        if req.protocolVersion == 2:
                            from machina.permissions.store import get_approval
                            from machina.protocol import approval_fields

                            record = await get_approval(str(pause["request_id"]))
                            if record is None:
                                raise RuntimeError("approval proposal is unavailable")
                            data = {
                                "type": "needs_approval",
                                **_redact_value(approval_fields(record)),
                            }
                        yield frame(data)
                data = _serialize_event(event)
                if event.type == "done":
                    terminal_data = data
                else:
                    yield frame(data)
        except TimeoutError:
            logger.warning("Prompt stream timed out for session %s", session_id)
            error_data = {"type": "error", "message": "turn timed out"}
            yield frame(error_data)
        except StaleOwnershipError:
            yield frame(
                {"type": "turn.ownership_lost", "sessionId": session_id, "turnId": turn_token}
            )
        except Exception:
            logger.exception("Prompt stream failed for session %s", session_id)
            error_data = {"type": "error", "message": "prompt failed; check server logs"}
            yield frame(error_data)
        except GeneratorExit:
            _stream_cancelled = True
            raise
        except asyncio.CancelledError:
            # Client disconnect / server shutdown: bounded cleanup below,
            # then propagate (never yield DONE here — the consumer is gone
            # and swallowing cancellation would strand the task).
            _stream_cancelled = True
            raise
        finally:
            _record_stage("cleanup_started")
            if resume_token is not None:
                from machina.permissions.broker import resume_approval_var

                resume_approval_var.reset(resume_token)
            active = _renew_state.get("active")
            if active is not None and not active.done():
                active.cancel()
                await asyncio.wait({active}, timeout=0.1)
            if active is not None and not active.done():
                from machina.core.runtime import runtime_services

                async def retire_execution() -> None:
                    # Keep ownership until the actual operation unwinds.
                    # Runtime shutdown observes a bound and quarantines this
                    # observer too; it cannot safely release a live execution.
                    while not active.done():
                        try:
                            await asyncio.shield(active)
                        except asyncio.CancelledError:
                            continue
                        except Exception as exc:
                            record_failure("app.retire_execution", exc)
                            break
                    try:
                        if stream is not None:
                            await stream.aclose()
                        if agent is not None:
                            # Consolidation shares the provider; join it (bounded)
                            # before this retire-path close, like every other path.
                            await join_agent_background_tasks(agent, timeout=1.0)
                            from machina.core.agent_factory import close_agent_provider

                            await close_agent_provider(agent)
                    finally:
                        await _stop_renewal()
                        await _end_turn(sid, turn_token)

                runtime_services.track(active)
                runtime_services.track(asyncio.create_task(retire_execution()))
                if not _stream_cancelled:
                    yield frame(
                        {
                            "type": "turn.cleanup_pending",
                            "sessionId": session_id,
                            "turnId": turn_token,
                        }
                    )
            else:
                # H-01/H-02/H-03/H-06: Bounded cleanup contract.
                # 1. Learning tasks: bounded join, then cancel leftovers.
                # 2. Provider close: bounded with timeout.
                # 3. Claim release: independent outer finally, always runs.
                # 4. Auto-compaction: fire-and-forget, never blocks [DONE].
                try:
                    if agent is not None:
                        _record_stage("joining_optional_work")
                        # Shared helper: bounded, settles before propagating, and
                        # timeout + settle_grace stays under the turn-completion
                        # deadline even for cancellation-resistant tasks.
                        await join_agent_background_tasks(agent, timeout=1.7, settle_grace=0.35)
                except (TimeoutError, Exception) as _boundary_error:
                    # Optional fallback preserves the primary outcome; report no payload.
                    record_failure("app.create_app", _boundary_error)
                finally:
                    # Claim release in independent outer finally so provider
                    # close errors or cancellation cannot skip it (H-02).
                    try:
                        _record_stage("closing_owned_clients")
                        # AH-023: close only owned providers, bounded.
                        if agent is not None:
                            try:
                                from machina.core.agent_factory import close_agent_provider

                                await close_agent_provider(agent)
                            except (TimeoutError, asyncio.CancelledError):
                                logger.warning(
                                    "Provider close timed out for session %s", session_id
                                )
                            except Exception:
                                logger.exception(
                                    "Could not close prompt provider for session %s",
                                    session_id,
                                )
                    finally:
                        _record_stage("releasing_ownership")
                        # AH-AUDIT-013: stop renewal during release.
                        try:
                            await _stop_renewal()
                        except (asyncio.CancelledError, Exception) as _boundary_error:
                            # Optional fallback preserves the primary outcome; report no payload.
                            record_failure("app.create_app", _boundary_error)
                        if turn_token:
                            try:
                                await _end_turn(sid, turn_token)
                            except Exception as _boundary_error:
                                _release_failed = True
                                # Unknown ownership must not produce a terminal event.
                                record_failure("app.create_app", _boundary_error)
                    # H-03: Auto-compaction is fire-and-forget background work.
                    # Never blocks [DONE] or holds the stream open.
                    # AH-AUDIT-025: tracked by the shared runtime for bounded
                    # shutdown joining before DB closure.
                    if not _stream_cancelled and not _release_failed:
                        _record_stage("compaction_maintenance")
                        try:
                            from machina import services as _services
                            from machina.core.runtime import runtime_services as _runtime

                            task = asyncio.create_task(_services.maybe_auto_compact(sid))
                            task.add_done_callback(
                                lambda t: t.exception() if not t.cancelled() else None
                            )
                            try:
                                _runtime.track(task)
                            except Exception as _boundary_error:
                                # Optional fallback preserves the primary outcome; report no payload.
                                record_failure("app.create_app", _boundary_error)
                        except Exception as _boundary_error:
                            # Optional fallback preserves the primary outcome; report no payload.
                            record_failure("app.create_app", _boundary_error)
                if not _stream_cancelled:
                    if _release_failed:
                        yield frame(
                            {
                                "type": "turn.cleanup_pending",
                                "reason": "ownership release unavailable",
                            }
                        )
                    else:
                        _record_stage("answer_complete")
                        if terminal_data is not None:
                            yield frame(terminal_data)
                        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "X-AH-Protocol-Version": str(req.protocolVersion),
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
