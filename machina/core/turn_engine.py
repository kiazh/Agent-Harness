"""Shared turn engine (W2): one agent-stream relay and one bounded cleanup tail.

The gateway (``machina.gateway.server.Gateway._run_turn``) and the SSE route
(``machina.api.routes.streaming._stream_session``) currently implement the same
turn semantics twice. This module is the single implementation both transports
delegate to (adoption is staged; nothing imports this module yet):

- :meth:`TurnEngine.stream` relays ``agent.run_stream`` into transport-agnostic
  :class:`TurnEvent`s, owning the boundary-safe streaming redactor, tool
  start/complete pairing (stable-id match, FIFO fallback, turn-scoped fallback
  ids), tool-result redaction + truncation, token accounting, and final
  capture. Resume continuations (``spec.resume``) iterate
  ``resume_action_stream``; the transport owns the ``resume_approval``
  contextvar (see :meth:`TurnEngine.stream`).
- :meth:`TurnEngine.flush_tail` / :meth:`TurnEngine.build_terminal` own the
  exactly-once withheld-tail contract (AH-AUDIT-027) and the canonical terminal
  payload (text/tokens/iterations/toolCalls/needsApproval).
- :meth:`TurnEngine.close` owns the ordered, bounded cleanup copied from the
  gateway's audited finally pyramids: join optional background work, close the
  owned provider, stop renewal, shielded bounded claim release, join renewal.

What deliberately stays in the transports: claim *begin* (timing and error
surfaces differ), frame serialization (JSON-RPC notifications vs SSE data
lines), the approval policy (interactive handler vs headless resume), the
cancel/timeout supervisors, tracing, and growth listeners. Transport-specific
effects plug in through :class:`TurnHooks`; diagnostics keys are namespaced by
the ``boundary`` string passed at construction (e.g. ``server._run_turn`` or
``app.create_app``) and are never hardcoded here.

Deliberate semantics mirrored from the audited transports:

- Claim release is shielded and bounded; a cancellation delivered while the
  release is in flight is consumed so the claim cannot be orphaned (RF4-C1).
- The renewal task is stopped before the release and joined after it; a
  shutdown cancellation delivered while joining a *still-running* renewal task
  propagates, while the expected self-cancelled join is absorbed (W7).
- A cancellation delivered during the optional background join still runs
  provider close + release before propagating (the transports' outer-finally
  guarantee).
- ``close()`` never raises for ordinary failures; every fallback is reported
  through ``record_failure`` under the transport's boundary.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from machina.core import agent_factory as _agent_factory
from machina.core import turns as _turns
from machina.core.background_tasks import join_agent_background_tasks
from machina.core.runtime import runtime_services
from machina.memory.redaction import StreamingSecretRedactor, redact_secrets, redact_value
from machina.observability.diagnostics import record_failure

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_TOOL_RESULT_CHARS",
    "OWNERSHIP_LOST_REASON",
    "TurnClose",
    "TurnEngine",
    "TurnEvent",
    "TurnHooks",
    "TurnSpec",
]

#: Tool-result payload cap. ``machina.gateway.server`` defines the same bound;
#: the wiring step re-exports this one so both transports share a single value.
MAX_TOOL_RESULT_CHARS = 4000

#: Loss reason handed to :meth:`TurnHooks.on_ownership_lost`; the
#: ``turn.ownership_lost`` frame carries it verbatim on both transports.
OWNERSHIP_LOST_REASON = "claim_renewal_failed"


class TurnHooks(Protocol):
    """Per-transport side effects the engine calls during a turn.

    ``on_stage`` receives the shared stage vocabulary (``cleanup_started``,
    ``joining_optional_work``, ``closing_owned_clients``, ``releasing_ownership``)
    for hang diagnostics. ``on_ownership_lost`` must stop owned effects *before*
    doing anything else — the shared renewal primitive guards a raising hook,
    but the transport's cancel is the critical effect.
    """

    def on_stage(self, stage: str) -> None: ...

    def on_ownership_lost(self, reason: str) -> None: ...


class _NullHooks:
    def on_stage(self, stage: str) -> None:
        return None

    def on_ownership_lost(self, reason: str) -> None:
        return None


@dataclass(slots=True)
class TurnSpec:
    """What the engine needs to run one turn.

    ``token`` is the owner token returned by ``turns.try_begin_turn`` — claim
    *begin* stays in the transport, which passes the token in. ``resume`` is
    the SSE approval continuation ``(record, arguments)``; ``None`` runs a
    normal prompt. Supervisor concerns (compute deadline, prompt cancellation)
    stay in the transport: the engine carries no deadline of its own.
    """

    session_id: uuid.UUID
    turn_id: str
    token: str | None = None
    text: str = ""
    images: list[dict[str, str]] | None = None
    verbose: bool = False
    resume: tuple[dict, dict] | None = None


@dataclass(slots=True)
class TurnEvent:
    """One semantic stream event, before any transport serialization.

    ``kind`` is one of ``delta`` (text), ``tool_start``, ``tool_complete``,
    ``usage``, ``needs_approval``, ``done`` (the stream's terminal marker), or
    ``approval_resumed`` (resume streams only). Payload keys match the
    canonical frame fields: text; id/name/args;
    id/name/result/truncated/isError/outcome; tokens; request_id/operation/
    proposal.
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TurnClose:
    """Outcome of :meth:`TurnEngine.close`.

    ``release_failed`` means ownership is unknown — the release raised, or it
    did not settle within ``release_timeout_s`` (including after a consumed
    mid-release cancellation): the caller must not emit a terminal completion
    claiming the turn is unowned. ``cancelled`` / ``timed_out`` are the
    transport's classifications, echoed back for terminal payloads.
    """

    cancelled: bool = False
    timed_out: bool = False
    ownership_lost: bool = False
    release_failed: bool = False
    final: Any | None = None
    tokens: int = 0


class TurnEngine:
    """Per-turn coordinator; construct one engine per turn, call close() once."""

    def __init__(
        self,
        spec: TurnSpec,
        hooks: TurnHooks | None = None,
        *,
        boundary: str = "turn_engine",
        renew_interval_s: float = 30.0,
        join_timeout: float = 1.7,
        settle_grace: float = 0.35,
        release_timeout_s: float = 5.0,
        renewal_join_timeout_s: float = 5.0,
        max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS,
    ) -> None:
        self.spec = spec
        self._hooks: TurnHooks = hooks if hooks is not None else _NullHooks()
        self._boundary = boundary
        self._renew_interval_s = renew_interval_s
        self._join_timeout = join_timeout
        self._settle_grace = settle_grace
        self._release_timeout_s = release_timeout_s
        self._renewal_join_timeout_s = renewal_join_timeout_s
        self._max_tool_result_chars = max_tool_result_chars
        self._agent: Any | None = None
        self._redactor = StreamingSecretRedactor()
        self._tail_flushed = False
        self._final: Any | None = None
        self._tokens = 0
        self._tool_count = 0
        self._open_tools: dict[str, str] = {}
        self._loss_event = asyncio.Event()
        self._renew_task: asyncio.Task | None = None
        # Retained when a bounded release cannot settle; see _release_claim.
        self._release_task: asyncio.Task | None = None

    # ─── wiring ────────────────────────────────────────────────────────────

    def attach(self, agent: Any) -> None:
        """Bind the built agent whose ``run_stream`` this turn relays."""
        self._agent = agent

    def start_renewal(self, *, ownership_lost: asyncio.Event | None = None) -> asyncio.Task | None:
        """Start managed claim renewal (shared W3 primitive); idempotent.

        Losing the claim sets :attr:`ownership_lost` and calls
        ``hooks.on_ownership_lost``. The transport builds from this whether the
        loss cancels an owned task (gateway) or an active stream step (SSE).
        A transport that already publishes a per-turn loss event (gateway map /
        SSE active-step race) passes it in so the primitive and its readers
        share ONE event; standalone callers get a fresh one.
        """
        if self._renew_task is not None and not self._renew_task.done():
            return self._renew_task
        if ownership_lost is not None:
            self._loss_event = ownership_lost
        owner = _turns.TurnOwnership(
            self.spec.session_id,
            self.spec.turn_id,
            self.spec.token,
            ownership_lost=self._loss_event,
        )
        self._renew_task = owner.start_renewal(
            interval_s=self._renew_interval_s, on_lost=self._on_ownership_lost
        )
        return self._renew_task

    def _on_ownership_lost(self) -> None:
        # The transport's on_lost cancels owned work FIRST; the shared renewal
        # loop already guards a raising callback (W3), but report either way.
        try:
            self._hooks.on_ownership_lost(OWNERSHIP_LOST_REASON)
        except Exception as exc:
            self._record("ownership_lost_hook", exc)

    @property
    def ownership_lost(self) -> asyncio.Event:
        """Loss event shared with the renewal primitive (transport readers)."""
        return self._loss_event

    @property
    def final(self) -> Any | None:
        """The ``AgentResponse`` from the last ``done`` event, else None."""
        return self._final

    @property
    def tokens(self) -> int:
        """Tokens accumulated from ``token_usage`` events (raw stream counter)."""
        return self._tokens

    @property
    def tool_count(self) -> int:
        return self._tool_count

    @property
    def outstanding_release(self) -> asyncio.Task | None:
        """Release task retained after a bounded release could not settle.

        ``close(release_failed=True)`` does not mean the claim is lost — the
        retained task may still release it. It is observed (its outcome is
        never a silent orphan) and exposed here for reconciliation.
        """
        return self._release_task

    @property
    def pauses(self) -> list[dict[str, Any]]:
        """Typed approval pauses from the final response (AH-AUDIT-022).

        Only real cards surface: non-dict entries and entries without a
        ``request_id`` are dropped. Both transports build their pause frames
        and terminal ``needsApproval`` list from this one extraction.
        """
        try:
            from machina.protocol import approval_summary

            return [
                approval_summary(pause)
                for pause in (getattr(self._final, "needs_approval", None) or [])
                if isinstance(pause, dict) and pause.get("request_id")
            ]
        except Exception as exc:
            self._record("pauses", exc)
            return []

    # ─── relay ─────────────────────────────────────────────────────────────

    async def stream(self) -> AsyncIterator[TurnEvent]:
        """Relay the agent stream as semantic events; safe to consume once.

        Raises ``RuntimeError`` when no agent is attached. Cancellation is the
        transports' concern (owned-task cancel for the gateway, ``_owned_step``
        deadline/loss race for SSE): the relay itself holds no timeout.

        The ``resume_approval`` contextvar for resume continuations is owned
        by the calling transport: it must be set in the context that *drives*
        the stream. Drivers that run each ``__anext__`` in its own task (the
        SSE route's ``_owned_step``) need the value present before those tasks
        are created, so a contextvar set inside this generator would not
        survive across steps.
        """
        agent = self._agent
        if agent is None:
            raise RuntimeError("TurnEngine.attach(agent) must be called before stream()")
        if self.spec.resume is not None:
            from machina.core.approval_resume import resume_action_stream

            record, arguments = self.spec.resume
            yield TurnEvent(
                "approval_resumed",
                {
                    "request_id": str(record.get("request_id", "")),
                    "origin_turn_id": str(record.get("turn_id", "")),
                },
            )
            source = resume_action_stream(agent, self.spec.session_id, record, arguments)
        else:
            stream_kwargs = {"images": self.spec.images} if self.spec.images else {}
            source = agent.run_stream(
                self.spec.session_id, self.spec.text, verbose=self.spec.verbose, **stream_kwargs
            )
        async for event in source:
            translated = self._translate(event)
            if translated is not None:
                yield translated

    def _translate(self, event: Any) -> TurnEvent | None:
        """Map one agent ``StreamEvent`` to its semantic counterpart."""
        event_type = getattr(event, "type", "")
        if event_type == "text":
            # Boundary-safe streaming redaction: only the safe frontier is
            # emitted; the withheld tail flushes via flush_tail()/terminal.
            safe = self._redactor.feed(event.content or "")
            if not safe:
                return None
            return TurnEvent("delta", {"text": safe})
        if event_type == "tool_call":
            self._tool_count += 1
            # AH-020: prefer the agent's stable call id so out-of-order
            # completions pair correctly; fall back to the turn counter.
            tool_id = (
                getattr(event, "tool_call_id", "") or f"{self.spec.turn_id}-{self._tool_count}"
            )
            self._open_tools[tool_id] = event.tool_name
            return TurnEvent(
                "tool_start",
                {
                    "id": tool_id,
                    "name": event.tool_name,
                    "args": redact_value(event.tool_args),
                },
            )
        if event_type == "tool_result":
            match_id = self._pair_tool(event)
            result = redact_secrets(str(event.tool_result)).text
            limit = self._max_tool_result_chars
            truncated = len(result) > limit
            payload: dict[str, Any] = {
                "id": match_id if match_id else f"{self.spec.turn_id}-{self._tool_count}",
                "name": event.tool_name,
                "result": result[:limit],
                "truncated": truncated,
                "isError": result.startswith("Error:"),
            }
            if event.tool_outcome:
                payload["outcome"] = event.tool_outcome
            return TurnEvent("tool_complete", payload)
        if event_type == "token_usage":
            self._tokens += event.tokens_used
            return TurnEvent("usage", {"tokens": self._tokens})
        if event_type == "needs_approval":
            # Typed pause marker (AH-AUDIT-030). SSE forwards it as a typed
            # frame; the gateway surfaces pauses through its approval handler
            # and ignores these stream markers.
            return TurnEvent(
                "needs_approval",
                {
                    "request_id": str(getattr(event, "content", "") or ""),
                    "operation": str(getattr(event, "tool_name", "") or ""),
                    "proposal": redact_value(dict(getattr(event, "tool_args", None) or {})),
                },
            )
        if event_type == "done":
            # The stream's terminal marker: transports capture the final
            # exactly here (progress snapshot / deferred terminal frame), so
            # the relay must surface it rather than swallowing it.
            self._final = event.response
            return TurnEvent("done")
        logger.warning("Unknown stream event type: %s", event_type or "?")
        return None

    def _pair_tool(self, event: Any) -> str | None:
        """Pair a result with its open call: stable id, else oldest same-name."""
        result_id = getattr(event, "tool_call_id", None) or getattr(event, "id", None)
        match_id: str | None = None
        if result_id is not None and str(result_id) in self._open_tools:
            match_id = str(result_id)
        else:
            for open_id, open_name in self._open_tools.items():
                if open_name == event.tool_name:
                    match_id = open_id
                    break
        if match_id is not None:
            self._open_tools.pop(match_id, None)
        return match_id

    # ─── terminal ──────────────────────────────────────────────────────────

    def flush_tail(self) -> str:
        """Redacted withheld tail, exactly once; "" on every later call.

        Callers emit the returned text as the final delta before the terminal
        event (concatenated display then equals the canonical answer once).
        Redaction failure fails closed (no raw tail is ever returned).
        """
        if self._tail_flushed:
            return ""
        self._tail_flushed = True
        try:
            tail = self._redactor.flush()
        except Exception as exc:
            self._record("flush_tail", exc)
            return ""
        if not tail:
            return ""
        try:
            return redact_secrets(tail).text
        except Exception as exc:
            self._record("flush_tail_redact", exc)
            return ""

    def build_terminal(self, *, cancelled: bool = False) -> dict[str, Any]:
        """Canonical terminal payload (message.complete / done fields).

        ``text`` is the fully redacted final content exactly once; when there
        is no final (error/empty paths) the withheld tail becomes the text —
        via the exactly-once :meth:`flush_tail`, so a caller that already
        emitted the tail as a delta gets "" and no duplication. Token
        accounting prefers the agent's final figures and falls back to the
        accumulated stream counter.
        """
        final = self._final
        text = ""
        try:
            text = redact_secrets(final.content).text if final is not None else ""
        except Exception as exc:
            self._record("terminal_redact", exc)
            text = ""
        if final is None:
            tail = self.flush_tail()
            if tail:
                text = tail
        payload: dict[str, Any] = {
            "text": text,
            "tokens": (final.tokens_used or self._tokens) if final is not None else self._tokens,
            "iterations": final.iterations if final is not None else 0,
            "toolCalls": len(final.tool_calls) if final is not None else 0,
            "cancelled": cancelled,
        }
        pauses = self.pauses
        if pauses:
            payload["needsApproval"] = pauses
        return payload

    # ─── cleanup ───────────────────────────────────────────────────────────

    async def close(self, *, cancelled: bool = False, timed_out: bool = False) -> TurnClose:
        """Bounded, ordered cleanup; call exactly once after the stream.

        Order (mirrors gateway 1066-1172): notification hooks ->
        join optional background work -> close the owned provider -> stop
        renewal -> shielded bounded release -> join renewal. Ordinary failures
        (including a failed claim release) are reported and returned in
        :class:`TurnClose`, never raised. A cancellation delivered during the
        optional-work join still runs provider close + release before it
        propagates; one delivered during the release itself is consumed
        (RF4-C1); one delivered while joining a still-running renewal task
        propagates (W7).
        """
        self._stage("cleanup_started")
        agent = self._agent
        try:
            if agent is not None:
                self._stage("joining_optional_work")
                try:
                    await join_agent_background_tasks(
                        agent, timeout=self._join_timeout, settle_grace=self._settle_grace
                    )
                except Exception as exc:
                    self._record("join_optional", exc)
        finally:
            try:
                self._stage("closing_owned_clients")
                if agent is not None:
                    try:
                        await _agent_factory.close_agent_provider(agent)
                    except (TimeoutError, asyncio.CancelledError):
                        logger.warning(
                            "Provider close timed out for session %s", self.spec.session_id
                        )
                    except Exception:
                        logger.exception(
                            "Could not close turn provider for session %s", self.spec.session_id
                        )
            finally:
                release_failed = await self._release_claim()
        return TurnClose(
            cancelled=cancelled,
            timed_out=timed_out,
            ownership_lost=self._loss_event.is_set(),
            release_failed=release_failed,
            final=self._final,
            tokens=self._tokens,
        )

    async def _release_claim(self) -> bool:
        """Stop renewal, bounded release, join renewal. True = failed.

        The configured ``release_timeout_s`` bounds EVERY wait on the release,
        including the first: an unbounded first wait would let a hung release
        outlive the bound and change the transport's terminal decision. A
        cancellation delivered while the release is in flight is consumed once
        (RF4-C1) and the bounded re-wait settles the release; if the release
        still cannot settle (timeout, second cancellation, or a remote failure)
        ownership is unproven — the outstanding task is retained and observed,
        and the caller gets ``True`` so transports emit ``turn.cleanup_pending``
        instead of a normal completion while the claim may still be held. A
        remote failure raising from the release marks unknown ownership too.
        """
        self._stage("releasing_ownership")
        renew_task = self._renew_task
        if renew_task is not None and not renew_task.done():
            renew_task.cancel()
        release_failed = False
        if self.spec.token:
            release = asyncio.ensure_future(_turns.end_turn(self.spec.session_id, self.spec.token))
            release_failed = await self._await_release(release)
        if renew_task is not None:
            # W7: a cancellation delivered to the turn while tearing down must
            # propagate, but the expected self-cancelled join is absorbed.
            if not renew_task.done():
                renew_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(renew_task), timeout=self._renewal_join_timeout_s
                )
            except asyncio.CancelledError:
                if not renew_task.cancelled():
                    raise
            except (TimeoutError, Exception):
                if not renew_task.done():
                    renew_task.cancel()
        return release_failed

    async def _await_release(self, release: asyncio.Task) -> bool:
        """Bounded wait(s) on the shielded release; True = ownership unproven."""
        try:
            await asyncio.wait_for(asyncio.shield(release), timeout=self._release_timeout_s)
            return False
        except asyncio.CancelledError:
            # Cancellation delivered mid-release (RF4-C1): consume it once and
            # give the same release the shared bound again. A second
            # cancellation is uncertainty too: fail closed instead of
            # continuing cleanup as if the claim were released.
            try:
                await asyncio.wait_for(asyncio.shield(release), timeout=self._release_timeout_s)
                return False
            except asyncio.CancelledError:
                return self._retain_release(release, "cancelled again")
            except TimeoutError:
                return self._retain_release(release, "did not settle within bound")
            except Exception as exc:
                self._record("release", exc)
                return self._retain_release(release, "raised")
        except TimeoutError:
            return self._retain_release(release, "did not settle within bound")
        except Exception as exc:
            self._record("release", exc)
            return True

    def _retain_release(self, release: asyncio.Task, why: str) -> bool:
        """Keep an unsettled release observed for reconciliation; True = failed."""
        self._release_task = release
        release.add_done_callback(self._observe_release)
        logger.warning("turn release %s; claim falls back to TTL expiry", why)
        return True

    @staticmethod
    def _observe_release(task: asyncio.Task) -> None:
        """Consume a retained release's outcome so it is never a silent orphan."""
        if not task.cancelled():
            task.exception()

    def schedule_auto_compact(self) -> None:
        """Fire-and-forget threshold compaction after ownership is released.

        The caller decides the skip predicate (release failure, cancelled
        stream); the task is runtime-tracked so shutdown joins it before DB
        closure (AH-AUDIT-025).
        """
        try:
            from machina import services as _services

            task = asyncio.create_task(_services.maybe_auto_compact(self.spec.session_id))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            try:
                runtime_services.track(task)
            except Exception as exc:
                self._record("compact_track", exc)
        except Exception as exc:
            self._record("compact_schedule", exc)

    # ─── diagnostics ───────────────────────────────────────────────────────

    def _stage(self, stage: str) -> None:
        try:
            self._hooks.on_stage(stage)
        except Exception as exc:
            self._record("stage", exc)

    def _record(self, site: str, exc: BaseException) -> None:
        record_failure(f"{self._boundary}.{site}", exc)
