"""Live proof: Machina's generic `custom` provider against the Hermes local
OpenAI-compatible proxy (http://127.0.0.1:8645/v1).

Run from the repo root with the project venv:

    .venv/bin/python docs/verification/proofs/free_models_2026-10-14/proof_custom_live.py

The Hermes proxy (``hermes proxy start --provider nous``) forwards to the
operator's Nous Portal subscription and accepts any bearer token — the
Machina side runs keyless, exercising the exact "local endpoint, no key"
path the custom provider was built for. No secrets are printed.
"""

from __future__ import annotations

import asyncio
import json
import os

BASE_URL = "http://127.0.0.1:8645/v1"
# Primary id from the proxy's own /v1/models catalog; Hermes's configured
# default spells it with a ":free" suffix, so fall back to that too.
CANDIDATE_MODELS = ("stepfun/step-5-preview", "stepfun/step-5-preview:free")
PROMPT = "Reply with exactly: HERMES-PROOF-OK"

# Must be set before machina.core.config is imported; CUSTOM_BASE_URL is the
# legacy env name resolved by config.get(). No CUSTOM_API_KEY: keyless mode.
os.environ["CUSTOM_BASE_URL"] = BASE_URL
os.environ.pop("CUSTOM_API_KEY", None)
os.environ.pop("MACHINA_CUSTOM_API_KEY", None)

from machina.core.exceptions import ValidationError  # noqa: E402
from machina.core.provider import DirectProvider, get_provider  # noqa: E402


async def _run_model(model: str) -> dict:
    provider = get_provider("custom", model=model)
    try:
        assert isinstance(provider, DirectProvider)
        assert provider.api_key == "", "keyless run must not resolve a key"
        assert "authorization" not in provider.client.headers

        response = await provider.complete(
            messages=[{"role": "user", "content": PROMPT}],
            temperature=0.0,
            max_tokens=64,
        )
        text_events: list[str] = []
        done = None
        async for event in provider.stream_complete(
            messages=[{"role": "user", "content": PROMPT}],
            temperature=0.0,
            max_tokens=64,
        ):
            if event.type == "text":
                text_events.append(event.content or "")
            elif event.type == "done":
                done = event.response
        return {
            "ok": True,
            "model_requested": model,
            "complete": {
                "model": response.model,
                "content": response.content,
                "usage": response.usage,
            },
            "stream": {
                "model": done.model if done else None,
                "content": "".join(text_events),
                "usage": done.usage if done else None,
                "done_event": done is not None,
            },
        }
    finally:
        await provider.close()


async def main() -> int:
    errors: list[str] = []
    for model in CANDIDATE_MODELS:
        try:
            result = await _run_model(model)
        except Exception as error:  # noqa: BLE001 — proof script reports failures verbatim
            errors.append(f"{model}: {type(error).__name__}: {str(error)[:200]}")
            continue
        print(json.dumps(result, indent=2, default=str))
        return 0
    print(json.dumps({"ok": False, "errors": errors}, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
