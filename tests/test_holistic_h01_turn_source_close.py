"""H01 (holistic review 2026-10-15): source shutdown must be bounded.

Pre-fix, ``TurnEngine._close_source`` cancelled the active driver and called
``source.aclose()`` through the unbounded ``await_joined``; a
cancellation-resistant source finalizer left ``stream.aclose()`` /
``engine.close()`` waiting forever, so the session claim was never released
while its renewal loop kept renewing it. The standalone reproduction in
``docs/verification/proofs/holistic_fixes_2026-10-15/L1_h01_repro.py`` shows
the hang and the missing ``end_turn``.

These pins drive the same resistant finalizer through the public engine
interface: cleanup must settle within the configured source bound, the
unresolved work must be quarantined (never silently orphaned), and ownership
release must run independently of source completion.
"""

from __future__ import annotations

import asyncio
import uuid

from machina.core import turns
from machina.core.models import StreamEvent
from machina.core.turn_engine import TurnEngine, TurnSpec

# Long enough to clear the 512-char streaming-redaction hold, so the first
# ``anext`` really yields a delta (matches transport consumption).
_PROBE_TEXT = "probe " * 120


def _spec(**overrides) -> TurnSpec:
    base: dict = {
        "session_id": uuid.uuid4(),
        "turn_id": "h01-turn",
        "token": "owner-token",
        "text": "hello",
    }
    base.update(overrides)
    return TurnSpec(**base)


async def _resist_cancellation_until(release: asyncio.Event, entered: asyncio.Event) -> None:
    """Finalizer that absorbs cancellation until the probe releases it."""
    entered.set()
    while not release.is_set():
        try:
            await asyncio.wait_for(release.wait(), 0.01)
        except (TimeoutError, asyncio.CancelledError):
            continue


async def test_stream_and_engine_close_release_claim_with_resistant_finalizer(monkeypatch):
    """A finalizer that cannot settle must not block ``close()``/the release."""
    released: list[str] = []
    release = asyncio.Event()
    finalizer_entered = asyncio.Event()
    finalizer_done = asyncio.Event()

    async def fake_end_turn(session_id, token):
        released.append(token)
        return True

    monkeypatch.setattr(turns, "end_turn", fake_end_turn)

    class Agent:
        async def run_stream(self, session_id, user_message, verbose=False, **kwargs):
            try:
                yield StreamEvent(type="text", content=_PROBE_TEXT)
            finally:
                try:
                    await _resist_cancellation_until(release, finalizer_entered)
                finally:
                    finalizer_done.set()

    engine = TurnEngine(_spec(), boundary="test.h01", source_close_timeout_s=0.05)
    engine.attach(Agent())
    stream = engine.stream()
    first = await anext(stream)
    assert first.kind == "delta"

    # Transport order (gateway/SSE): close the stream first, then the engine.
    pending: list[asyncio.Task] = []
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        stream_close = asyncio.create_task(stream.aclose())
        pending.append(stream_close)
        done, _ = await asyncio.wait({stream_close}, timeout=1.0)
        assert stream_close in done, "stream close must not wait forever on a resistant finalizer"

        engine_close = asyncio.create_task(engine.close())
        pending.append(engine_close)
        done, _ = await asyncio.wait({engine_close}, timeout=1.0)
        assert engine_close in done, "engine close must not wait forever on a resistant finalizer"
        elapsed = loop.time() - started

        report = engine_close.result()
        assert report.release_failed is False
        assert released == ["owner-token"], "claim release must run before source completion"
        assert elapsed < 0.9, f"cleanup must be bounded, took {elapsed:.2f}s"
        assert finalizer_entered.is_set()
        assert not finalizer_done.is_set(), (
            "the resistant finalizer is quarantined, not silently dropped"
        )
    finally:
        release.set()
        if pending:
            await asyncio.wait(pending, timeout=1.0)
        await asyncio.wait_for(finalizer_done.wait(), 1.0)


async def test_engine_close_releases_claim_when_active_driver_ignores_cancellation(monkeypatch):
    """A driver that absorbs cancellation while awaiting the source must not block close."""
    released: list[str] = []
    release = asyncio.Event()
    entered = asyncio.Event()

    async def fake_end_turn(session_id, token):
        released.append(token)
        return True

    monkeypatch.setattr(turns, "end_turn", fake_end_turn)

    class Agent:
        async def run_stream(self, session_id, user_message, verbose=False, **kwargs):
            await _resist_cancellation_until(release, entered)
            yield StreamEvent(type="text", content="late")

    engine = TurnEngine(_spec(), boundary="test.h01-driver", source_close_timeout_s=0.05)
    engine.attach(Agent())
    stream = engine.stream()
    step = asyncio.create_task(anext(stream))
    await asyncio.wait_for(entered.wait(), 1.0)

    pending: list[asyncio.Task] = [step]
    try:
        engine_close = asyncio.create_task(engine.close())
        pending.append(engine_close)
        done, _ = await asyncio.wait({engine_close}, timeout=1.0)
        assert engine_close in done, "close must bound the driver join"

        report = engine_close.result()
        assert report.release_failed is False
        assert released == ["owner-token"], "claim release must not wait on the resistant driver"
    finally:
        release.set()
        await asyncio.wait(pending, timeout=1.0)
        await stream.aclose()


async def test_aclose_failure_is_reported_and_release_still_runs(monkeypatch):
    """An ordinary source-close failure must not raise out of close() or skip release."""
    released: list[str] = []
    recorded: list[str] = []

    async def fake_end_turn(session_id, token):
        released.append(token)
        return True

    monkeypatch.setattr(turns, "end_turn", fake_end_turn)
    monkeypatch.setattr(
        "machina.core.turn_engine.record_failure", lambda site, exc: recorded.append(site)
    )

    class Agent:
        async def run_stream(self, session_id, user_message, verbose=False, **kwargs):
            try:
                yield StreamEvent(type="text", content=_PROBE_TEXT)
            finally:
                raise RuntimeError("finalizer boom")

    engine = TurnEngine(_spec(), boundary="test.h01-error")
    engine.attach(Agent())
    stream = engine.stream()
    first = await anext(stream)
    assert first.kind == "delta"

    await stream.aclose()  # must report, never raise
    report = await engine.close()

    assert report.release_failed is False
    assert released == ["owner-token"]
    assert "test.h01-error.source_close" in recorded
