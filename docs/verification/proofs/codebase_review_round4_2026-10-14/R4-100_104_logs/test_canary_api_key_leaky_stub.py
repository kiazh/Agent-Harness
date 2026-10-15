"""R4-104 canary-discrimination probe (evidence only).

Reproduces the pre-fix import-time leak (module-level ``os.environ[...] =``
with the test sentinel) so the clean canary's mechanism is proven to detect
it. This file is EXPECTED TO FAIL when run; it is never part of tests/.
"""

from __future__ import annotations

import os

os.environ["MACHINA_API_KEY"] = "test-key-123"  # the old test_api.py import side effect


def test_canary_detects_import_time_leak():
    assert os.environ.get("MACHINA_API_KEY") != "test-key-123"
