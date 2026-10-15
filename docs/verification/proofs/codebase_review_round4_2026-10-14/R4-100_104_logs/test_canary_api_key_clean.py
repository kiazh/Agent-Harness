"""R4-104 canary: the sentinel used by test_api tests must not leak.

``MACHINA_API_KEY`` itself is present via ``.env``, so the observable leak
was the overwritten *value*: the old module-level assignment set
``test-key-123`` process-wide for every later test and subprocess. This
canary fails while that leak exists.

Run AFTER (or alongside) the leaking module in the same pytest process:

    .venv/bin/python -m pytest tests/test_api.py \
        docs/.../R4-100_104_logs/test_canary_api_key_clean.py -q
"""

from __future__ import annotations

import os

LEAKED_SENTINEL = "test-key-123"


def test_api_key_sentinel_not_leaked_into_other_tests():
    assert os.environ.get("MACHINA_API_KEY") != LEAKED_SENTINEL
