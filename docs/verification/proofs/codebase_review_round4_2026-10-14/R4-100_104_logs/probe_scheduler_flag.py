"""R4-104 env evidence probes (evidence only; never collected as tests).

1. ``first-test`` check: by the time the very first test sets up, the
   suite-owned ``MACHINA_GATEWAY_NO_SCHEDULER`` flag must already be present
   (previously it appeared only after some Harness construction -> order
   dependent, and leaked for the rest of the session).
2. ``unconfigure`` check: the session fixture must restore the environment
   it found before pytest exits.

Usage:
    PYTHONPATH=.../R4-100_104_logs .venv/bin/python -m pytest -p probe_scheduler_flag \
        tests/test_gateway_features.py -q
"""

from __future__ import annotations

import os

_first_seen = {"checked": False}
_flag_name = "MACHINA_GATEWAY_NO_SCHEDULER"


def pytest_runtest_call(item) -> None:
    # Runs after collection AND after fixture setup for this item.
    if _first_seen["checked"]:
        return
    _first_seen["checked"] = True
    assert os.environ.get(_flag_name) == "1", (
        "MACHINA_GATEWAY_NO_SCHEDULER missing at the first test setup: "
        "the suite-owned flag is not set from session start"
    )


def pytest_unconfigure(config) -> None:
    assert os.environ.get(_flag_name) is None, (
        "MACHINA_GATEWAY_NO_SCHEDULER leaked past session teardown: "
        "the conftest fixture did not restore the environment"
    )
