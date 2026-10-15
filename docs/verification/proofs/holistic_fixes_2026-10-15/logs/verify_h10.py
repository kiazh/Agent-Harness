"""H10 post-fix verification: config and transport share one max_tokens policy.

Run: .venv/bin/python docs/verification/proofs/holistic_fixes_2026-10-15/logs/verify_h10.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from machina.core.config import MAX_TOKENS, Config, validate_value
from machina.core.exceptions import ValidationError
from machina.core.provider.transport import _validate_params


def main() -> int:
    failures: list[str] = []

    def check(condition: bool, label: str) -> None:
        print(f"{'PASS' if condition else 'FAIL'}: {label}")
        if not condition:
            failures.append(label)

    check(MAX_TOKENS == 32768, f"single ceiling constant exported: MAX_TOKENS={MAX_TOKENS}")

    # Save side: the reproduction value is rejected before any write/mutation.
    cfg = Config()
    try:
        cfg.set("max_tokens", 65536)
        check(False, "Config.set('max_tokens', 65536) is rejected")
    except ValueError as exc:
        check(
            "max_tokens must be in range 1..32768" in str(exc),
            f"Config.set rejection message: {exc}",
        )
    check(cfg.max_tokens == 4096, "rejected set did not mutate the value")

    # Boundary agreement: both surfaces accept the ceiling, reject ceiling+1.
    check(validate_value("max_tokens", MAX_TOKENS) == MAX_TOKENS, "config accepts the ceiling")
    try:
        validate_value("max_tokens", MAX_TOKENS + 1)
        check(False, "config rejects ceiling+1")
    except ValueError:
        check(True, "config rejects ceiling+1")
    _validate_params(0.7, MAX_TOKENS)
    check(True, "transport accepts the ceiling")
    try:
        _validate_params(0.7, MAX_TOKENS + 1)
        check(False, "transport rejects ceiling+1")
    except ValidationError:
        check(True, "transport rejects ceiling+1")

    # Load side: a stale file value above the ceiling is never activated.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.write_text("max_tokens: 65536\n", encoding="utf-8")
        loaded = Config.load(path)
        check(loaded.get("max_tokens") == 4096, "stale file value above ceiling keeps the default")

    if failures:
        print(f"RESULT: FAILURES — {failures}")
        return 1
    print("RESULT: FIX VERIFIED — one shared ceiling; save and request time agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
