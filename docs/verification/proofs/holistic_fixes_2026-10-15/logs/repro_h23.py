"""H23 reproduction: /effort reports success while MACHINA_REASONING_EFFORT shadows.

Run: .venv/bin/python docs/verification/proofs/holistic_fixes_2026-10-15/logs/repro_h23.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace


def _gw() -> SimpleNamespace:
    return SimpleNamespace(
        require_db=lambda: None,
        provider="openai",
        model="gpt-4o",
        opened_session_id=lambda: None,
    )


def main() -> int:
    print("== H23: /effort echo vs environment precedence ==")
    from machina.gateway.features import providers_feature as feature

    # Case 1: MACHINA_REASONING_EFFORT=high shadows a requested "low".
    os.environ["MACHINA_REASONING_EFFORT"] = "high"
    from machina.core.config import config

    out = asyncio.run(feature.models_set_effort(_gw(), {"effort": "low"}))
    effective = config.get("reasoning_effort")
    print(f"request /effort low -> response effort={out['effort']!r}")
    print(f"config.get('reasoning_effort') -> {effective!r} (env MACHINA_REASONING_EFFORT=high)")
    reproduced = out["effort"] == "low" and effective == "high"
    print(f"case1 shadowed-success: {'REPRODUCED' if reproduced else 'not reproduced'}")

    # Case 2: an explicitly empty env value also wins over an in-memory set.
    os.environ["MACHINA_REASONING_EFFORT"] = ""
    config.set("reasoning_effort", "high")
    effective_empty = config.get("reasoning_effort")
    print(f"config.set('reasoning_effort','high') with env='' -> get()={effective_empty!r}")
    reproduced = reproduced and effective_empty == ""

    if reproduced:
        print("RESULT: REPRODUCED — the echo claims the change while calls keep the env value")
        return 0
    print("RESULT: NOT REPRODUCED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
