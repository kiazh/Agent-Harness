"""H23 post-fix verification: /effort is effective under an env shadow.

Run: .venv/bin/python docs/verification/proofs/holistic_fixes_2026-10-15/logs/verify_h23.py
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
    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        print(f"{'PASS' if condition else 'FAIL'}: {label}")
        if not condition:
            failures.append(label)

    from machina.core.config import config
    from machina.gateway.features import providers_feature as feature

    # Requested change under a pre-existing environment value.
    os.environ["MACHINA_REASONING_EFFORT"] = "high"
    out = asyncio.run(feature.models_set_effort(_gw(), {"effort": "low"}))
    check(out["effort"] == "low", "echo reports the requested effort")
    check(
        config.get("reasoning_effort") == "low",
        "effective effort equals the echo (env shadow synced)",
    )
    check(
        os.environ["MACHINA_REASONING_EFFORT"] == "low",
        "MACHINA_REASONING_EFFORT now carries the requested value",
    )

    # Clearing to provider default must stay effective, including empty env.
    out = asyncio.run(feature.models_set_effort(_gw(), {"effort": "off"}))
    check(out["effort"] is None, "off echo reports provider default")
    check(
        config.get("reasoning_effort") == "",
        "off is effective even though env previously carried 'high'",
    )

    # Restore the process env for callers of this script.
    os.environ.pop("MACHINA_REASONING_EFFORT", None)

    if failures:
        print(f"RESULT: FAILURES — {failures}")
        return 1
    print("RESULT: FIX VERIFIED — the echo is the effective setting under an env shadow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
