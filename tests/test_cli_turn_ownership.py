"""R04/R05/R14 regression tests: headless CLI session ownership + terminal outcomes.

Converts the executable reproduction
``docs/verification/proofs/codebase_review_2026-10-14/cli.py`` (plus the R14
outcome path it exercises) into behavioral assertions:

* R04 — ``chat --session`` acquires the shared session-turn claim (renewed for
  the turn, released owner-matched) and rejects a session owned by another
  transport instead of calling ``agent.run_stream()`` under a foreign owner.
* R04b — mid-turn ownership loss cancels the task driving ``agent.run_stream``
  immediately (no delayed effect after loss), and generator cleanup runs
  before the claim is released and before the database closes.
* R05 — session deletion uses the mutation-claim protocol: a live turn or a
  foreign mutation claim refuses the delete; a free session is claimed,
  deleted, and released.
* R14 — failed/paused terminal responses are displayed (canonical content that
  never streamed included) and map to the documented exit statuses
  (``EXIT_FAILED`` = 1, ``EXIT_PAUSED`` = 3).
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

import machina.cli as cli
from machina.core import turns
from machina.core.models import AgentResponse, StreamEvent

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_local_claims():
    """The DB-less coordinator registry is process-global; keep tests isolated."""
    turns._local_reset_for_tests()
    yield
    turns._local_reset_for_tests()


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="openrouter",
        model="mock",
        title="audit",
    )


class _RecordingAgent:
    """Async-generator agent that records the session claim it ran under."""

    def __init__(self, events: list[StreamEvent] | None = None) -> None:
        self.events = list(events or [])
        self.calls: list[dict] = []
        self.provider = SimpleNamespace(model="mock")

    async def run_stream(self, session_id, message, verbose=False):
        self.calls.append({"session_id": session_id, "owner": turns._local_owner(session_id)})
        for event in self.events:
            yield event


def _stub_cli(monkeypatch: pytest.MonkeyPatch, session, agent) -> None:
    """Wire the CLI's one-shot runtime around *session* and *agent*."""
    monkeypatch.setattr(cli.db, "connect", AsyncMock())
    monkeypatch.setattr(cli.db, "close", AsyncMock())
    monkeypatch.setattr(cli.db, "initialize_runtime_schema", AsyncMock())
    monkeypatch.setattr(cli.session_manager, "get", AsyncMock(return_value=session))
    monkeypatch.setattr(
        "machina.core.agent_factory.build_agent_for_session",
        AsyncMock(return_value=agent),
    )
    monkeypatch.setattr("machina.core.agent_factory.close_agent_provider", AsyncMock())
    monkeypatch.setattr(cli, "drain_background_tasks", AsyncMock())


def _foreign_owner_run(session_id: uuid.UUID, kind: str = "turn"):
    """Return a ``cli._run`` replacement holding another transport's real claim.

    The claim is acquired (and released) inside the same event loop as the CLI
    invocation, exactly like the review reproduction: the CLI must observe an
    occupied session through the real coordinator, not a mock.
    """

    def run(coro):
        async def wrapped():
            if kind == "turn":
                token = await turns.try_begin_turn(session_id, turn_id="other-transport-turn")
            else:
                token = await turns.begin_mutation(session_id)
            assert token, "test setup could not acquire the foreign claim"
            try:
                return await coro
            finally:
                if kind == "turn":
                    await turns.end_turn(session_id, token)
                else:
                    await turns.end_mutation(session_id, token)

        return asyncio.run(wrapped())

    return run


# ─── R04: headless chat must own the turn ───────────────────────────────────


def test_chat_refuses_session_owned_by_another_transport(monkeypatch):
    session = _session()
    agent = _RecordingAgent([StreamEvent(type="text", content="must not run")])
    _stub_cli(monkeypatch, session, agent)
    monkeypatch.setattr(cli, "_run", _foreign_owner_run(session.id))

    result = runner.invoke(cli.app, ["chat", "hello", "--session", str(session.id), "--quiet"])

    flat = " ".join(result.output.split())
    assert result.exit_code == cli.EXIT_FAILED
    assert "busy" in flat, result.output
    assert agent.calls == [], "a foreign owner's claim must reject before the stream runs"


def test_chat_claims_turn_while_streaming_and_releases_it(monkeypatch):
    session = _session()
    agent = _RecordingAgent(
        [
            StreamEvent(type="text", content="Hello"),
            StreamEvent(
                type="done",
                response=AgentResponse(content="Hello", tokens_used=1, iterations=1),
            ),
        ]
    )
    _stub_cli(monkeypatch, session, agent)

    result = runner.invoke(cli.app, ["chat", "hello", "--session", str(session.id), "--quiet"])

    assert result.exit_code == cli.EXIT_OK, result.output
    owner = agent.calls[0]["owner"]
    assert owner is not None and owner["kind"] == "turn", "CLI must hold the shared turn claim"
    assert turns._local_owner(session.id) is None, "the claim is released when the run ends"
    assert result.output.count("Hello") == 1, "streamed answers are not printed twice"


# ─── R04b: ownership loss cancels the active stream immediately ─────────────


class _DelayedEffectAgent:
    """Streams with a delayed effect that must not survive ownership loss.

    The live generator is retained on the instance (as real transports keep a
    handle for cancellation), so its ``finally`` cannot pass the ordering
    checks by accident: without an explicit close it only finalizes when the
    loop shuts down — after claim release and database closure, exactly the
    P1 finding.
    """

    def __init__(self) -> None:
        self.provider = SimpleNamespace(model="mock")
        self.timeline: list[str] = []
        self.effects: list[str] = []
        self.owner_at_close: dict | None = None
        self.closed = False
        self._live_stream = None

    def run_stream(self, session_id, message, verbose=False):
        self._live_stream = self._stream(session_id)
        return self._live_stream

    async def _stream(self, session_id):
        try:
            # Long enough that the failing renewal (interval below) signals
            # loss while this delay is pending: the effect after it must
            # never run once ownership is gone.
            await asyncio.sleep(0.5)
            self.effects.append("delayed effect after ownership loss")
            yield StreamEvent(type="text", content="must not stream")
        finally:
            self.closed = True
            self.owner_at_close = turns._local_owner(session_id)
            self.timeline.append("generator.close")


def _lost_renewal_run(monkeypatch, session, agent) -> tuple:
    """Invoke ``chat`` with a renewal that loses ownership on every tick."""
    _stub_cli(monkeypatch, session, agent)
    monkeypatch.setattr(cli, "CLI_TURN_RENEW_INTERVAL_SECONDS", 0.01)
    renew_calls: list[dict] = []

    async def failing_renew(session_id, token):
        renew_calls.append({"session_id": session_id, "token": token})
        return False

    monkeypatch.setattr(turns, "renew_turn", failing_renew)
    real_end_turn = turns.end_turn

    async def recording_end_turn(session_id, token):
        agent.timeline.append("release")
        return await real_end_turn(session_id, token)

    monkeypatch.setattr(turns, "end_turn", recording_end_turn)

    async def recording_db_close():
        agent.timeline.append("db.close")

    monkeypatch.setattr(cli.db, "close", recording_db_close)

    result = runner.invoke(cli.app, ["chat", "hello", "--session", str(session.id), "--quiet"])
    return result, renew_calls


def test_chat_ownership_loss_cancels_active_work_before_delayed_effect(monkeypatch):
    """P1: loss must cancel the running stream, not wait for the next yield."""
    session = _session()
    agent = _DelayedEffectAgent()

    result, renew_calls = _lost_renewal_run(monkeypatch, session, agent)

    assert renew_calls, "the failing renewal must have been consulted (loss path fired)"
    assert agent.effects == [], (
        "a delayed effect must never execute after ownership was lost — the CLI "
        "must cancel the task driving agent.run_stream instead of flag-checking "
        "only on the next streamed yield"
    )
    assert agent.closed, "the stream generator is finalized"
    assert agent.owner_at_close is not None and agent.owner_at_close.get("kind") == "turn", (
        "generator cleanup runs while the CLI still holds the turn claim"
    )
    assert turns._local_owner(session.id) is None, "the claim is still released"
    assert result.exit_code == cli.EXIT_FAILED
    flat = " ".join(result.output.split())
    assert "ownership was lost" in flat, result.output
    assert "must not stream" not in flat


def test_chat_ownership_loss_closes_generator_before_release_and_db_close(monkeypatch):
    """P1: cleanup ordering — generator closed before claim release/db close."""
    session = _session()
    agent = _DelayedEffectAgent()

    _lost_renewal_run(monkeypatch, session, agent)

    assert agent.timeline == ["generator.close", "release", "db.close"], (
        "on loss the generator must be closed before the claim is released and "
        "before the database closes (pre-fix it finalized after db.close)"
    )


# ─── R05: deletion must claim a mutation slot ───────────────────────────────


def test_delete_refuses_session_with_live_turn(monkeypatch):
    session = _session()
    delete_calls: list[uuid.UUID] = []

    async def fake_delete(session_id):
        delete_calls.append(session_id)
        return True

    _stub_cli(monkeypatch, session, _RecordingAgent())
    monkeypatch.setattr(cli.session_manager, "delete", fake_delete)
    monkeypatch.setattr(cli, "_run", _foreign_owner_run(session.id, kind="turn"))

    result = runner.invoke(cli.app, ["delete", str(session.id), "--force"])

    assert result.exit_code == cli.EXIT_FAILED
    assert "active turn" in " ".join(result.output.split())
    assert delete_calls == [], "destructive delete must not run under a foreign turn"


def test_delete_refuses_session_with_foreign_mutation_claim(monkeypatch):
    session = _session()
    delete_calls: list[uuid.UUID] = []

    async def fake_delete(session_id):
        delete_calls.append(session_id)
        return True

    _stub_cli(monkeypatch, session, _RecordingAgent())
    monkeypatch.setattr(cli.session_manager, "delete", fake_delete)
    monkeypatch.setattr(cli, "_run", _foreign_owner_run(session.id, kind="mutation"))

    result = runner.invoke(cli.app, ["delete", str(session.id), "--force"])

    assert result.exit_code == cli.EXIT_FAILED
    assert "busy" in " ".join(result.output.split())
    assert delete_calls == [], "destructive delete must not run under a foreign mutation claim"


def test_delete_claims_mutation_and_releases_it(monkeypatch):
    session = _session()
    observed: list[dict | None] = []

    async def fake_delete(session_id):
        observed.append(turns._local_owner(session_id))
        return True

    _stub_cli(monkeypatch, session, _RecordingAgent())
    monkeypatch.setattr(cli.session_manager, "delete", fake_delete)

    result = runner.invoke(cli.app, ["delete", str(session.id), "--force"])

    assert result.exit_code == cli.EXIT_OK, result.output
    assert "deleted" in result.output
    assert observed and observed[0] is not None and observed[0]["kind"] == "mutation"
    assert turns._local_owner(session.id) is None, "the mutation claim is released"


# ─── R14: failed/paused terminal outcomes are visible with exit statuses ─────


def test_chat_failed_outcome_exits_failed_with_diagnostic(monkeypatch):
    session = _session()
    agent = _RecordingAgent(
        [
            StreamEvent(
                type="done",
                response=AgentResponse(
                    content="PROVIDER FAILURE MUST BE VISIBLE",
                    outcome="failed",
                    tokens_used=0,
                    iterations=1,
                ),
            )
        ]
    )
    _stub_cli(monkeypatch, session, agent)

    result = runner.invoke(cli.app, ["chat", "hello", "--session", str(session.id), "--quiet"])

    assert result.exit_code == cli.EXIT_FAILED
    assert "PROVIDER FAILURE MUST BE VISIBLE" in result.output, "terminal diagnostic is shown"
    assert "failed" in result.output.lower()
    assert str(session.id) in result.output, "the session id remains discoverable"


def test_chat_paused_outcome_exits_paused_with_approval_info(monkeypatch):
    session = _session()
    agent = _RecordingAgent(
        [
            StreamEvent(
                type="done",
                response=AgentResponse(
                    content="Waiting for approval.",
                    needs_approval=[
                        {
                            "request_id": "req-1",
                            "operation": "process.exec",
                            "target": "/tmp/x",
                        }
                    ],
                    tokens_used=0,
                    iterations=1,
                ),
            )
        ]
    )
    _stub_cli(monkeypatch, session, agent)

    result = runner.invoke(cli.app, ["chat", "hello", "--session", str(session.id), "--quiet"])

    flat = " ".join(result.output.split())
    assert result.exit_code == cli.EXIT_PAUSED
    assert cli.EXIT_PAUSED != cli.EXIT_FAILED and cli.EXIT_PAUSED != cli.EXIT_OK
    assert "paused for approval" in flat, result.output
    assert "req-1" in flat and "process.exec" in flat and "/tmp/x" in flat


def test_chat_prints_canonical_content_never_streamed(monkeypatch):
    session = _session()
    agent = _RecordingAgent(
        [
            StreamEvent(
                type="done",
                response=AgentResponse(
                    content="Silent canonical answer", tokens_used=0, iterations=1
                ),
            )
        ]
    )
    _stub_cli(monkeypatch, session, agent)

    result = runner.invoke(cli.app, ["chat", "hello", "--session", str(session.id), "--quiet"])

    assert result.exit_code == cli.EXIT_OK, result.output
    assert "Silent canonical answer" in result.output
    assert result.output.count("Silent canonical answer") == 1


def test_chat_help_documents_exit_statuses():
    result = runner.invoke(cli.app, ["chat", "--help"])
    assert result.exit_code == 0
    assert "Exit status" in result.output


# ─── R4-017: _run keeps the caller's loop (SIGINT cancellability) ───────────


def test_run_uses_the_calling_threads_loop_and_supports_fallbacks():
    """`_run` must run the coroutine on the caller's loop (main thread).

    That is what puts asyncio's first-SIGINT cancellation contract in
    charge of Ctrl+C for `machina chat`. Callers already inside a running
    event loop must keep the worker fallback, and non-main threads (where
    asyncio cannot install signal handlers) must keep working. The
    subprocess-level SIGINT proof lives in tests/test_cli_turn_e2e.py.
    """
    import threading

    async def identify():
        return threading.current_thread() is threading.main_thread(), 42

    assert cli._run(identify()) == (True, 42), "main-thread runs use the caller's loop"

    off_thread: list[tuple[bool, int]] = []

    def worker() -> None:
        off_thread.append(cli._run(identify()))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=30)
    assert off_thread and off_thread[0][1] == 42, "non-main-thread callers still work"

    async def inside_loop():
        return cli._run(identify())

    assert asyncio.run(inside_loop())[1] == 42, "running-loop callers keep the fallback"
