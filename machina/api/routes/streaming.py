"""SSE streaming for session turns (moved verbatim from app.py).

Shared by the sessions router (prompt/chat) and the approvals router
(approval resume): both stream agent events as Server-Sent Events through
:func:`_stream_session`.
"""

from __future__ import annotations

import asyncio
import contextvars
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
from machina.core.config import config
from machina.core.exceptions import StaleOwnershipError
from machina.core.execution_context import begin_turn_scope, end_turn_scope
from machina.core.models import StreamEvent
from machina.core.session import session_manager
from machina.core.turn_engine import TurnEngine, TurnSpec
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
    # R4-084: the wire carries the canonical 12-hex turn id (the same shape
    # the stdio gateway generates, ``uuid4().hex[:12]``) so transports can
    # correlate one turn; the internal owner token ("turn:<id>:<nonce>")
    # never leaves this module.
    from machina.core.turns import try_begin_turn as _try_begin

    turn_id = uuid.uuid4().hex[:12]
    turn_token = await _try_begin(sid, turn_id=turn_id)
    if not turn_token:
        raise HTTPException(409, "a turn is already running for this session")

    # AH-AUDIT-013: managed renewal while the stream owns the turn
    # (compute only here — headless SSE never waits on humans, so no
    # approval accounting is needed on this path). Created lazily on
    # first iteration so an unconsumed stream never renews forever;
    # stopped when the generator exits; crash expiry recovery preserved.
    _renew_state: dict[str, Any] = {}
    # F07: one shared execution Context for the whole turn's step tasks. The
    # agent sets growth-episode / RSI ContextVars while preparing the turn
    # (inside the first step's task); because asyncio Tasks copy the creator's
    # context, a fresh task+context per `__anext__` hid those writes from later
    # steps and from the done handler that runs `_complete_growth` — growth
    # episodes never completed. Driving every step task in one shared Context
    # keeps the writes visible for the rest of the turn. Same principle as
    # R4-010: the driver that creates the step tasks owns the ContextVar
    # lifetime. Armed per-turn (empty until the drive loop starts).
    _step_context: dict[str, contextvars.Context] = {}
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
        # F07: run steps in the turn's shared Context once armed so ContextVars
        # the agent writes during preparation survive across steps. Before the
        # drive loop arms it (the agent build step) keeps the default per-task
        # context copy.
        context = _step_context.get("ctx")
        if context is None:
            task = asyncio.ensure_future(awaitable)
        else:
            task = asyncio.get_running_loop().create_task(awaitable, context=context)
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

    def frame(data: dict[str, Any]) -> str:
        if req.protocolVersion == 2:
            from machina.protocol import canonical_http_event

            data = canonical_http_event(data, session_id, turn_id)
        return f"data: {json.dumps(data)}\n\n"

    async def event_stream() -> AsyncGenerator[str, None]:
        agent = None
        stream = None
        terminal_data = None
        resume_token = None
        _stream_cancelled = False
        _release_failed = False
        _usage_emitted = 0
        _stage = "turn_started"

        def _record_stage(stage: str) -> None:
            nonlocal _stage
            _stage = stage
            logger.debug("Session %s stage: %s", session_id, stage)

        class _TurnHooks:
            def on_stage(_self, stage: str) -> None:
                _record_stage(stage)

            def on_ownership_lost(_self, reason: str) -> None:
                active = _renew_state.get("active")
                if active is not None:
                    active.cancel()

        # W2: the shared engine owns boundary-safe streaming redaction (the
        # withheld tail flushes exactly once before the terminal), tool
        # pairing (stable id, FIFO fallback, 4000-char truncation), token
        # accounting, final capture, the renewal loop, and the bounded
        # cleanup tail; this generator only serializes semantic events.
        # Renewal starts here — on first iteration, not at response
        # construction — so an unconsumed stream never renews forever; crash
        # expiry recovery is preserved.
        engine = TurnEngine(
            TurnSpec(
                session_id=sid,
                turn_id=turn_id,
                token=turn_token,
                text=req.text,
                verbose=req.verbose,
                resume=resume,
            ),
            _TurnHooks(),
            boundary="app.create_app",
            renew_interval_s=_renew_interval,
        )

        async def close_turn():
            from machina.core.background_tasks import await_joined

            async def finish():
                try:
                    if stream is not None:
                        await stream.aclose()
                finally:
                    closed = await engine.close(cancelled=_stream_cancelled)
                return closed

            return await await_joined(finish())

        engine.start_renewal(ownership_lost=_ownership_lost)
        # R4-010: driver-owned turn scope — every `_owned_step` task runs in a
        # context that carries this scope (the stream steps share the armed
        # context below; the agent-build step copies this one), so the scope is
        # visible to all steps and the reset stays in the context that created
        # the token.
        #
        # R4-064 follow-up (stopped here, deliberately): no OTel span around
        # this drive loop. An async generator can be resumed by one task and
        # finalized by another (per-step `_owned_step` tasks here; Starlette
        # closing the response on disconnect), and opentelemetry's context
        # detach fails + logs in the foreign context. Unlike the turn-mode
        # reset there is no tolerant detach available for spans, so this
        # driver owns no span; gateway/CLI (single-task drivers) carry the
        # stream span. Evidence: proofs/codebase_review_round4_2026-10-14/
        # R4-063_064_observability.md.
        turn_scope_token = begin_turn_scope()
        try:
            from machina.permissions.broker import set_approval_handler

            set_approval_handler(None, principal="http", turn_id=turn_id)
            yield frame({"type": "turn.started", "sessionId": session_id, "turnId": turn_id})
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
            if resume is not None:
                # Transport-owned resume contextvar (the engine does not own
                # it): set in THIS context so every `_owned_step` task copies
                # the value into the step that executes the reviewed action.
                from machina.permissions.broker import resume_approval_var

                resume_token = resume_approval_var.set(str(resume[0].get("request_id", "")))
            engine.attach(agent)
            # F07: arm the shared step Context after the transport-owned resume
            # ContextVar is set, so every agent step (and the done handler) sees
            # it together with the growth/RSI state the agent writes while
            # preparing. Copied from THIS driver context, which already owns the
            # turn scope entered above.
            _step_context["ctx"] = contextvars.copy_context()
            stream = engine.stream()
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("turn timed out")
                try:
                    turn_event = await _owned_step(stream.__anext__(), remaining)
                except StopAsyncIteration:
                    break
                if turn_event.kind == "delta":
                    # The engine emitted only the safe redaction prefix.
                    data = _serialize_event(
                        StreamEvent(type="text", content=turn_event.payload["text"]),
                        _pre_redacted=True,
                    )
                    yield frame(data)
                elif turn_event.kind == "approval_resumed":
                    yield frame(
                        {
                            "type": "approval.resumed",
                            "requestId": turn_event.payload["request_id"],
                            "sessionId": session_id,
                            "turnId": turn_id,
                            "originTurnId": turn_event.payload["origin_turn_id"],
                        }
                    )
                elif turn_event.kind == "tool_start":
                    yield frame(
                        _serialize_event(
                            StreamEvent(
                                type="tool_call",
                                tool_name=turn_event.payload["name"],
                                tool_args=turn_event.payload["args"],
                                tool_call_id=turn_event.payload["id"],
                            )
                        )
                    )
                elif turn_event.kind == "tool_complete":
                    payload = turn_event.payload
                    yield frame(
                        _serialize_event(
                            StreamEvent(
                                type="tool_result",
                                tool_name=payload["name"],
                                tool_result=payload["result"],
                                tool_outcome=payload.get("outcome", ""),
                                tool_call_id=payload["id"],
                            ),
                            # P10: carry the engine's real failure flag on the
                            # canonical wire; the v1 compatibility shape has no
                            # isError field, so only v2 requests forward it.
                            tool_is_error=(
                                payload.get("isError", False) if req.protocolVersion == 2 else None
                            ),
                        )
                    )
                elif turn_event.kind == "needs_approval":
                    # Stream pause marker (AH-AUDIT-030): typed and
                    # actionable, never a generic error. Continuation frames
                    # from the done response are emitted after the loop.
                    yield frame(
                        _serialize_event(
                            StreamEvent(
                                type="needs_approval",
                                content=turn_event.payload["request_id"],
                                tool_name=turn_event.payload["operation"],
                                tool_args=turn_event.payload["proposal"],
                            )
                        )
                    )
                elif turn_event.kind == "usage":
                    # Transport shape preserved: SSE usage frames carry the
                    # per-call count while the engine reports the running
                    # total (the wire event keeps its historical semantics).
                    cumulative = turn_event.payload["tokens"]
                    per_call = cumulative - _usage_emitted
                    _usage_emitted = cumulative
                    yield frame(
                        _serialize_event(StreamEvent(type="token_usage", tokens_used=per_call))
                    )
                elif turn_event.kind == "done":
                    if engine.final is None:
                        # Defensive parity: a done without a response still
                        # terminates the stream with the bare done frame.
                        terminal_data = {"type": "done"}
                        continue
                    # AH-AUDIT-027: the withheld tail is flushed exactly once
                    # as a delta before the terminal; the done payload carries
                    # the canonical fully-redacted concatenation, never a raw
                    # tail.
                    tail = engine.flush_tail()
                    if tail:
                        data = _serialize_event(
                            StreamEvent(type="text", content=tail),
                            _pre_redacted=True,
                        )
                        yield frame(data)
                    # AH-AUDIT-030: typed needs_approval SSE output with
                    # request/session/turn binding and the sanitized proposal.
                    # A pause is actionable and distinct from failure; resume
                    # revalidates under a fresh fenced claim (never by
                    # continuing stale generator state).
                    for pause in engine.pauses:
                        pause_event = StreamEvent(
                            type="needs_approval",
                            content=str(pause.get("requestId", "")),
                            tool_name=str(pause.get("operation", "")),
                            tool_args={
                                "requestId": str(pause.get("requestId", "")),
                                "sessionId": str(pause.get("sessionId", "") or session_id),
                                "operation": str(pause.get("operation", "")),
                                "target": str(pause.get("target", "")),
                                "status": str(pause.get("status", "pending")),
                            },
                        )
                        data = _serialize_event(pause_event)
                        if req.protocolVersion == 2:
                            from machina.permissions.store import get_approval
                            from machina.protocol import approval_fields

                            record = await get_approval(str(pause["requestId"]))
                            if record is None:
                                raise RuntimeError("approval proposal is unavailable")
                            data = {
                                "type": "needs_approval",
                                **_redact_value(approval_fields(record)),
                            }
                        yield frame(data)
                    terminal_payload = engine.build_terminal(cancelled=False)
                    terminal_data = {
                        "type": "done",
                        "content": terminal_payload["text"],
                        "tokens": terminal_payload["tokens"],
                        "iterations": terminal_payload["iterations"],
                        "toolCalls": terminal_payload["toolCalls"],
                    }
                    if "needsApproval" in terminal_payload:
                        terminal_data["needsApproval"] = terminal_payload["needsApproval"]
                else:
                    logger.warning("Unknown turn event kind: %s", turn_event.kind)
        except TimeoutError:
            logger.warning("Prompt stream timed out for session %s", session_id)
            error_data = {"type": "error", "message": "turn timed out"}
            yield frame(error_data)
        except StaleOwnershipError:
            yield frame({"type": "turn.ownership_lost", "sessionId": session_id, "turnId": turn_id})
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
            if isinstance(sys.exc_info()[1], (GeneratorExit, asyncio.CancelledError)):
                _stream_cancelled = True
            end_turn_scope(turn_scope_token)
            _record_stage("cleanup_started")
            if resume_token is not None:
                from machina.permissions.broker import resume_approval_var

                try:
                    resume_approval_var.reset(resume_token)
                except ValueError:
                    # Same as the turn scope above: an async-generator close
                    # can run in a different task context; the resumed
                    # request context is already gone.
                    pass
            from machina.core.background_tasks import await_joined

            async def cleanup():
                active = _renew_state.get("active")
                if active is not None and not active.done():
                    active.cancel()
                    await asyncio.wait({active}, timeout=0.1)
                if active is not None and not active.done():
                    from machina.core.runtime import runtime_services

                    async def retire_execution():
                        try:
                            await await_joined(active)
                        except asyncio.CancelledError:
                            pass
                        except Exception as error:
                            record_failure("app.retire_execution", error)
                        await close_turn()

                    runtime_services.track(active)
                    runtime_services.track(asyncio.create_task(retire_execution()))
                    return None
                return await close_turn()

            close = await await_joined(cleanup())
            if close is None:
                if not _stream_cancelled:
                    yield frame(
                        {
                            "type": "turn.cleanup_pending",
                            "sessionId": session_id,
                            "turnId": turn_id,
                        }
                    )
            else:
                _release_failed = close.release_failed
                # H-03: Auto-compaction is fire-and-forget background work.
                # Never blocks [DONE] or holds the stream open.
                # AH-AUDIT-025: tracked by the shared runtime for bounded
                # shutdown joining before DB closure.
                if not _stream_cancelled and not _release_failed:
                    _record_stage("compaction_maintenance")
                    engine.schedule_auto_compact()
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
