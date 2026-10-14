"""Gemini (Cloud Code Assist) — hermetic proof (2026-10-14), plus manual step.

No Gemini license is signed in on this machine, so this proof drives the real
``GeminiProvider`` against an ``httpx.MockTransport`` and a sandboxed HOME:

1. import path: a fake ``~/.gemini/oauth_creds.json`` is read read-only and
   imported into the Machina store (0600); gemini-cli's file is untouched;
2. the onboarding path: ``loadCodeAssist`` -> ``onboardUser`` -> operation
   poll resolves the Cloud Code Assist project, which is persisted on the
   store and reused by the stream request;
3. a streamed completion (Cloud Code Assist SSE, ``finishReason`` terminal);
4. the typed ``gemini_not_eligible`` failure with Google's reason codes and
   the consumer-tier deprecation note (what a free/Pro/Ultra account hits
   since 2026-06-18).

Manual step (needs a Code Assist Standard/Enterprise license): run
``machina auth gemini login`` (browser PKCE loopback) and then
``machina chat -p gemini -m gemini-2.5-pro``. Everything except acceptance by
Google's backend for a licensed account is verified here.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx

from machina.core.models import LLMResponse
from machina.core.provider.gemini import GeminiProvider
from machina.security import gemini_auth
from machina.security.gemini_auth import GEMINI_CLIENT_ID, GeminiAuthError

MESSAGES = [{"role": "user", "content": "Reply with exactly: machina gemini proof ok"}]


def _sse(*chunks: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps({'response': chunk})}\n\n" for chunk in chunks)
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


async def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="machina-gemini-proof-"))
    import os

    os.environ["HOME"] = str(home)
    os.environ.pop("MACHINA_GEMINI_AUTH_FILE", None)
    os.environ.pop("MACHINA_GEMINI_IMPORT_FILE", None)
    from machina.core.config import config

    config.machina_home = ""

    creds = home / ".gemini" / "oauth_creds.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(
        json.dumps(
            {
                "access_token": "cli-proof-access",
                "refresh_token": "cli-proof-refresh",
                "scope": gemini_auth.SCOPES,
                "expiry_date": int((time.time() + 3600) * 1000),
            }
        )
    )
    before = creds.read_bytes()

    print(
        "[auth] client_id is the gemini-cli OSS registration "
        f"({GEMINI_CLIENT_ID.split('-')[0]}…, installed-app secret documented non-secret)"
    )
    tokens = gemini_auth.import_from_gemini_cli()
    assert tokens is not None
    assert creds.read_bytes() == before
    print(
        f"[import] ~/.gemini/oauth_creds.json read-only; store={gemini_auth.auth_path()} mode=0600"
    )
    print(f"[import] status={json.dumps(gemini_auth.status(), sort_keys=True)}")

    paths: list[str] = []

    def onboard_handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1internal:loadCodeAssist":
            print("[wire] loadCodeAssist -> allowedTiers=[standard(isDefault)], no project yet")
            return httpx.Response(
                200,
                json={
                    "allowedTiers": [
                        {
                            "id": "standard",
                            "isDefault": True,
                            "userDefinedCloudaicompanionProject": False,
                        }
                    ]
                },
            )
        if request.url.path == "/v1internal:onboardUser":
            print("[wire] onboardUser(tierId=standard) -> operation in flight")
            return httpx.Response(200, json={"name": "operations/onboard-1", "done": False})
        if request.url.path == "/v1internal/operations/onboard-1":
            return httpx.Response(
                200,
                json={
                    "done": True,
                    "response": {"cloudaicompanionProject": {"id": "proof-project"}},
                },
            )
        body = json.loads(request.content)
        assert request.url.path == "/v1internal:streamGenerateContent"
        assert body["project"] == "proof-project"
        assert body["model"] == "gemini-2.5-pro"
        print(
            f"[wire] streamGenerateContent?alt=sse auth=Bearer <redacted> "
            f"project={body['project']} model={body['model']} "
            f"contents={len(body['request']['contents'])}"
        )
        return _sse(
            {"candidates": [{"content": {"parts": [{"text": "machina "}]}}]},
            {
                "candidates": [{"content": {"parts": [{"text": "gemini proof ok"}]}}],
                "modelVersion": "gemini-2.5-pro-001",
            },
            {
                "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
                "usageMetadata": {
                    "promptTokenCount": 8,
                    "candidatesTokenCount": 5,
                    "totalTokenCount": 13,
                },
            },
        )

    provider = GeminiProvider(
        model="gemini-2.5-pro", transport=httpx.MockTransport(onboard_handler)
    )
    provider.ONBOARD_POLL_DELAY = 0.0
    done: LLMResponse | None = None
    try:
        async for event in provider.stream_complete(MESSAGES, max_tokens=64):
            if event.type == "done":
                done = event.response
    finally:
        await provider.close()
    assert done is not None and done.content == "machina gemini proof ok"
    print(f"[stream] done model={done.model} content={done.content!r} usage={done.usage}")
    print(f"[onboard] wire order: {' -> '.join(paths)}")
    assert gemini_auth.load_tokens()["project"] == "proof-project"
    print("[onboard] project persisted on the Machina store (reused on later calls)")

    # Consumer-account reality: Google refuses ineligible tiers.
    def ineligible_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "allowedTiers": [],
                "ineligibleTiers": [
                    {"reasonCode": "INELIGIBLE_TIER", "reasonMessage": "consumer tier retired"}
                ],
            },
        )

    gemini_auth.save_tokens(
        {"access_token": "at", "refresh_token": "rt", "last_refresh": None}, None
    )
    provider = GeminiProvider(
        model="gemini-2.5-pro", transport=httpx.MockTransport(ineligible_handler)
    )
    try:
        async for _ in provider.stream_complete(MESSAGES):
            pass
        print("[fail] ineligible account did not raise")
        return 1
    except GeminiAuthError as error:
        assert error.code == "gemini_not_eligible"
        print(f"[typed] {error.code}: {error}")
    finally:
        await provider.close()

    print("[pass] hermetic Gemini proof ok (import + onboarding + stream + typed ineligibility)")
    print(
        "[manual] with a Code Assist Standard/Enterprise license: "
        "machina auth gemini login && machina chat -p gemini -m gemini-2.5-pro"
    )
    return 0


if __name__ == "__main__":
    print(f"started {time.strftime('%Y-%m-%d %H:%M:%S')} (python {sys.version.split()[0]})")
    raise SystemExit(asyncio.run(main()))
