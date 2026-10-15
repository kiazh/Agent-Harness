"""H10 reproduction: config saves max_tokens=65536, transport rejects it.

Run: .venv/bin/python docs/verification/proofs/holistic_fixes_2026-10-15/logs/repro_h10.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from machina.core.config import Config
from machina.core.exceptions import ValidationError
from machina.core.provider import OpenRouterProvider


def main() -> int:
    print("== H10: config/transport max_tokens disagreement ==")

    # 1. "Saving" 65536 succeeds: config.set accepts it and the file round-trips.
    cfg = Config()
    cfg.set("max_tokens", 65536)
    print(f"Config.set('max_tokens', 65536) -> ok  (cfg.max_tokens={cfg.max_tokens})")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        cfg.save(path)
        reloaded = Config.load(path)
        saved = reloaded.get("max_tokens")
        print(f"save + reload -> max_tokens={saved!r} (saved successfully)")
        assert saved == 65536

    # 2. A call through the common transport fails before reaching the provider.
    class _FakeClient:
        def __init__(self) -> None:
            self.posts = 0

        async def post(self, *args, **kwargs):  # pragma: no cover - must not run
            self.posts += 1
            raise AssertionError("HTTP POST must not happen after validation fails")

    client = _FakeClient()
    provider = OpenRouterProvider(api_key="test-key", model="openrouter/free")
    provider.client = client  # type: ignore[assignment]

    async def call() -> None:
        await provider.complete(
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=int(reloaded.get("max_tokens")),
        )

    try:
        asyncio.run(call())
    except ValidationError as exc:
        print(f"provider.complete(max_tokens=65536) -> ValidationError: {exc}")
        print(f"HTTP posts attempted: {client.posts}")
        print("RESULT: REPRODUCED — saved value is rejected at request time before provider contact")
        return 0
    print("RESULT: NOT REPRODUCED — provider accepted the saved value")
    return 1


if __name__ == "__main__":
    sys.exit(main())
