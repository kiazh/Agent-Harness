"""Device polling preserves backoff while respecting the remaining deadline."""

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


def test_slow_down_interval_uses_bounded_sleep_pieces(monkeypatch):
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


@pytest.mark.parametrize("timeout, expected_polls", [(10.0, 1), (90.0, 1), (200.0, 2)])
def test_slow_down_does_not_poll_early_or_after_deadline(monkeypatch, timeout, expected_polls):
    clock = _FakeClock(start=0)
    monkeypatch.setattr(copilot_auth, "time", clock)
    polls = []
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    def handler(request):
        polls.append(clock.now)
        assert clock.now < timeout
        assert all(0 < value <= min(20.0, timeout - clock.now)
                   for value in request.extensions["timeout"].values())
        if len(polls) == 1:
            return httpx.Response(200, json={"error": "slow_down", "interval": 120})
        return httpx.Response(200, json={"access_token": "gho_device"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        if expected_polls == 1:
            with pytest.raises(CopilotAuthError) as caught:
                copilot_auth.device_flow_poll("device", interval=1, timeout=timeout,
                                              client=client, sleep=sleep)
            assert caught.value.code == "copilot_device_timeout"
        else:
            assert copilot_auth.device_flow_poll("device", interval=1, timeout=timeout,
                                                client=client, sleep=sleep) == "gho_device"
    assert len(polls) == expected_polls
    if len(polls) > 1:
        assert polls[1] - polls[0] >= 120
    assert all(0 < piece <= copilot_auth.DEVICE_POLL_MAX_WAIT for piece in sleeps)
    assert clock.now <= timeout


def test_poll_does_not_send_request_when_initial_wait_exhausts_budget(monkeypatch):
    clock = _FakeClock(start=0)
    monkeypatch.setattr(copilot_auth, "time", clock)
    polls = []

    def sleep(seconds):
        clock.now += seconds

    def handler(request):
        polls.append(request)
        return httpx.Response(200, json={"access_token": "gho_device"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CopilotAuthError) as caught:
            copilot_auth.device_flow_poll("device", interval=120, timeout=10,
                                          client=client, sleep=sleep)
    assert caught.value.code == "copilot_device_timeout"
    assert polls == []
    assert clock.now == 10
