"""LIVE proof: a real completion through a user-added named provider.

Assumes ``machina providers add hermes-live http://127.0.0.1:8645/v1 --no-key``
already ran against the Hermes loopback proxy. Resolves the provider through
the factory (not a hand-built client), reports runtime readiness, runs one
real non-streaming completion, and writes ``named_provider_completion.json``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
PREFERRED_MODEL = "stepfun/step-5-preview:free"


async def main() -> int:
    import os

    from machina.core import runtime
    from machina.core.provider import get_provider
    from machina.security import providers as store

    entry = store.get_provider_entry("hermes-live")
    if entry is None:
        print("hermes-live is not registered; run the CLI add first")
        return 1
    cached = entry.get("models") or []
    print(f"store: {len(cached)} cached models for hermes-live (env={entry['api_key_env']})")

    # Runtime readiness for the selected provider. Config precedence puts the
    # environment first, so select via the same env var the runtime reads.
    previous_provider = os.environ.get("MACHINA_PROVIDER")
    os.environ["MACHINA_PROVIDER"] = "hermes-live"
    try:
        report = runtime._detect_providers()
    finally:
        if previous_provider is None:
            os.environ.pop("MACHINA_PROVIDER", None)
        else:
            os.environ["MACHINA_PROVIDER"] = previous_provider
    readiness = {
        "chat_provider": report["chat_provider"],
        "chat_provider_reason": report["chat_provider_reason"],
        "chat_provider_selected": report["chat_provider_selected"],
    }
    print(f"runtime readiness: {readiness}")

    model = (
        PREFERRED_MODEL
        if any(row.get("id") == PREFERRED_MODEL for row in cached)
        else cached[0]["id"]
    )
    provider = get_provider("hermes-live", model=model)
    try:
        print(
            f"factory resolved: {type(provider).__name__} base={provider.spec.base_url} model={provider.model} key={len(provider.api_key)}B"
        )
        response = await provider.complete(
            [
                {
                    "role": "user",
                    "content": "Reply with exactly MACHINA-NAMED-PROOF-OK and nothing else.",
                }
            ],
            max_tokens=256,
        )
    finally:
        await provider.close()

    result = {
        "ok": "MACHINA-NAMED-PROOF-OK" in (response.content or ""),
        "provider": "hermes-live",
        "model": response.model,
        "content": response.content,
        "usage": response.usage,
        "readiness": readiness,
        "keyless": provider.api_key == "",
    }
    print(json.dumps(result, indent=2))
    (OUT / "named_provider_completion.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
