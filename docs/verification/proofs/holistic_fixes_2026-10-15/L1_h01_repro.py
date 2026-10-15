"""L1 / H01 minimal reproduction: a resistant source finalizer blocks claim release.

Run with the repo venv (no database needed):

    .venv/bin/python docs/verification/proofs/holistic_fixes_2026-10-15/L1_h01_repro.py

Pre-fix observation: ``engine.close()`` does not complete within the probe
bound and ``turns.end_turn`` is never called — the source's finalizer
suppresses cancellation, so ``_close_source`` waits on it forever through
``await_joined`` (a bounded probe cannot use ``wait_for``, which would itself
wait for the cancellation-swallowing coroutine; it uses ``asyncio.wait``).
In a real transport the renewal loop keeps renewing the session claim while
this wait never returns.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

sys.path.insert(0, ".")

from machina.core import turns  # noqa: E402
from machina.core.models import StreamEvent  # noqa: E402
from machina.core.turn_engine import TurnEngine, TurnSpec  # noqa: E402

# Pre-fix the probe just has to show the never-settling wait (1s suffices);
# post-fix the default source bound is 5s, so the GREEN run raises this to 8s.
PROBE_SECONDS = float(os.environ.get("L1_H01_PROBE_SECONDS", "1.0"))


async def main() -> int:
    released: list[str] = []
    gate = asyncio.Event()

    async def fake_end_turn(session_id, token):
        released.append(token)
        return True

    turns.end_turn = fake_end_turn  # type: ignore[assignment]

    class HangingAgent:
        async def run_stream(self, session_id, user_message, verbose=False, **kwargs):
            try:
                # Long enough to pass the 512-char streaming redaction hold.
                yield StreamEvent(type="text", content="probe " * 120)
            finally:
                # Cancellation-resistant finalizer: keeps running until the
                # probe releases it (models a wedged provider aclose).
                while not gate.is_set():
                    try:
                        await asyncio.wait_for(gate.wait(), 0.01)
                    except (TimeoutError, asyncio.CancelledError):
                        continue

    engine = TurnEngine(
        TurnSpec(session_id=uuid.uuid4(), turn_id="l1-h01", token="owner-token", text="hi"),
        boundary="l1.h01.repro",
    )
    engine.attach(HangingAgent())

    stream = engine.stream()
    first = await anext(stream)
    assert first.kind == "delta", first

    closing = asyncio.create_task(engine.close())
    loop = asyncio.get_running_loop()
    started = loop.time()
    done, _ = await asyncio.wait({closing}, timeout=PROBE_SECONDS)
    elapsed = loop.time() - started
    outcome = "bounded" if closing in done else "hang"
    released_before_gate = list(released)
    gate.set()
    await asyncio.wait({closing}, timeout=1.0)
    try:
        await stream.aclose()
    except BaseException:
        pass

    print(f"close_outcome={outcome!r} probe_seconds={PROBE_SECONDS} elapsed={elapsed:.2f}s")
    print(f"claim_released_before_gate={released_before_gate!r}")
    if outcome == "hang" and not released_before_gate:
        print("REPRODUCED: source shutdown blocked claim release (H01)")
        return 0
    if outcome == "bounded" and released_before_gate:
        print("FIXED: source shutdown bounded; claim released before the finalizer settled")
        return 0
    print("NOT REPRODUCED")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
