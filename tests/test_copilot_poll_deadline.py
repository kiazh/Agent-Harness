"""F3 evidence — ``device_flow_poll`` never sleeps past its device-flow deadline.

The access-token endpoint can reply ``slow_down`` with a server-supplied
``interval``. That value is untrusted input: before the fix it replaced the
per-poll wait outright with no clip to the remaining deadline and no cap, so
``sleep(wait)`` could block far past the documented ``DEFAULT_DEVICE_TIMEOUT``
(300 s) contract (finding F3,
``docs/verification/proofs/final_review_2026-10-15/line_security_permissions.md``).

These tests drive the poll with a stubbed endpoint (``httpx.MockTransport``)
and a fake monotonic clock, so the deadline is reached in virtual time with no
real sleeping. They pin two invariants:

* the per-poll wait is never the huge server ``interval`` and never exceeds the
  remaining device-flow budget;
* a huge server interval is capped by ``DEVICE_POLL_MAX_WAIT``.

Both still end in the existing typed timeout (``copilot_device_timeout``), so
the success/error path semantics and status output are unchanged.

RED before the fix: the huge interval is handed straight to ``sleep``.
GREEN after: the wait is clamped to the deadline and the cap.
"""

from __future__ import annotations

import httpx
import pytest

from machina.security import copilot_auth
from machina.security.copilot_auth import CopilotAuthError

HUGE_INTERVAL = 1_000_000_000.0


class _FakeClock:
    """Minimal stand-in for the ``time`` module: only ``monotonic`` is read."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


def _slow_down_client(interval: float = HUGE_INTERVAL) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"error": "slow_down", "interval": interval})
        )
    )


def test_slow_down_interval_is_clamped_to_remaining_deadline(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(copilot_auth, "time", clock)
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.now += seconds

    client = _slow_down_client()
    with pytest.raises(CopilotAuthError) as excinfo:
        copilot_auth.device_flow_poll(
            "dev-1", interval=0.01, timeout=2.0, client=client, sleep=fake_sleep
        )
    # Existing typed timeout is preserved; we did not sleep past the deadline.
    assert excinfo.value.code == "copilot_device_timeout"
    assert HUGE_INTERVAL not in sleeps
    assert sleeps, "expected at least one sleep before the deadline"
    assert sum(sleeps) <= 2.0 + 1e-9
    assert max(sleeps) <= 2.0 + 1e-9


def test_slow_down_interval_is_capped(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(copilot_auth, "time", clock)
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.now += seconds

    client = _slow_down_client()
    with pytest.raises(CopilotAuthError) as excinfo:
        copilot_auth.device_flow_poll(
            "dev-1", interval=0.01, timeout=300.0, client=client, sleep=fake_sleep
        )
    assert excinfo.value.code == "copilot_device_timeout"
    assert HUGE_INTERVAL not in sleeps
    # A sane per-poll cap (well below the 300 s budget) bounds every wait.
    assert max(sleeps) <= copilot_auth.DEVICE_POLL_MAX_WAIT + 1e-9
    assert sum(sleeps) <= 300.0 + 1e-9
