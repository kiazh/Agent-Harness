#!/usr/bin/env python
"""Audit finding: _child_span catches a body exception as setup failure when
no turn is active and yields twice -> RuntimeError masks the original error.
"""
import sys
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

from machina.observability import traces

print("tracing enabled:", traces.tracing_enabled(), "active turn:", traces.current_turn_id())

try:
    with traces._child_span("audit-kind", "audit-name", {}):
        raise ValueError("original body error")
except Exception as error:  # noqa: BLE001
    print(f"raised: {type(error).__name__}: {error}")
    doubled = type(error).__name__ == "RuntimeError" and "generator didn't stop" in str(error)
    print("CLAIM CONFIRMED (original ValueError became RuntimeError)"
          if doubled else "CLAIM NOT REPRODUCED")
    sys.exit(0 if doubled else 1)
else:
    print("no exception escaped; claim not reproduced")
    sys.exit(1)
