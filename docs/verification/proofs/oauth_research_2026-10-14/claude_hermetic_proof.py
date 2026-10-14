"""Claude Pro/Max — hermetic proof (2026-10-14), plus the manual login step.

No Claude subscription is signed in on this machine, so this proof drives the
real ``ClaudeProvider`` (production client, headers, auth flow) against an
``httpx.MockTransport`` and a sandboxed HOME:

1. import path: a fake ``~/.claude/.credentials.json`` is read read-only and
   imported into the Machina store (0600); Claude Code's file is untouched
   (bytes + mtime verified);
2. the OAuth identity headers the provider sends (captured redacted);
3. 401 -> refresh -> replay-once through the real auth flow (refresh is a
   scripted stand-in that updates the store; the token-endpoint wire shape is
   covered by tests/test_claude_auth.py);
4. a streamed completion (Anthropic SSE) ending in ``message_stop``.

Manual step (needs a real subscription): ``claude`` login is not required —
run ``machina auth claude login`` (browser PKCE loopback or ``--manual`` code
paste) and the Claude Code OAuth flow returns real tokens; then
``machina chat -p claude -m claude-sonnet-4-5`` exercises the same transport
against api.anthropic.com. Everything except that live call is verified here.
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
from machina.core.provider.claude import CLAUDE_CODE_UA, OAUTH_BETAS, ClaudeProvider
from machina.security import claude_auth

MESSAGES = [{"role": "user", "content": "Reply with exactly: machina claude proof ok"}]


def _message_stream(*deltas: str) -> httpx.Response:
    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    body = sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_proof",
                "model": "claude-sonnet-4-5",
                "usage": {"input_tokens": 9},
            },
        },
    )
    body += sse(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )
    for delta in deltas:
        body += sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": delta},
            },
        )
    body += sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    body += sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 4},
        },
    )
    body += sse("message_stop", {"type": "message_stop"})
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


async def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="machina-claude-proof-"))
    import os

    os.environ["HOME"] = str(home)
    os.environ.pop("MACHINA_CLAUDE_AUTH_FILE", None)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    from machina.core.config import config

    config.machina_home = ""
    claude_auth._read_claude_code_keychain = lambda: None  # no real Keychain access

    creds = home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "cc-proof-access",
                    "refreshToken": "cc-proof-refresh",
                    "expiresAt": int((time.time() + 3600) * 1000),
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        )
    )
    before = creds.read_bytes()
    before_mtime = creds.stat().st_mtime_ns

    tokens = claude_auth.import_from_claude_code()
    print("[import] source=~/.claude/.credentials.json (read-only)")
    print(f"[import] store={claude_auth.auth_path()} mode=0600")
    print(f"[import] status={json.dumps(claude_auth.status(), sort_keys=True)}")
    assert tokens is not None and tokens["subscription_type"] == "max"
    assert creds.read_bytes() == before and creds.stat().st_mtime_ns == before_mtime
    print("[import] Claude Code file untouched (bytes + mtime unchanged)")

    attempts: list[str] = []
    refreshed: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        attempts.append("Bearer <redacted>" if auth else "<missing>")
        print(
            f"[wire] POST {request.url.path} auth={attempts[-1]} "
            f"beta={request.headers.get('anthropic-beta')} "
            f"ua={request.headers.get('user-agent')} x-app={request.headers.get('x-app')} "
            f"x-api-key={'present' if request.headers.get('x-api-key') else 'absent'}"
        )
        if len(attempts) == 1:
            return httpx.Response(401, json={"error": {"type": "authentication_error"}})
        return _message_stream("machina ", "claude ", "proof ok")

    def fake_refresh(tokens, *, path=None, **kwargs):
        refreshed.append(dict(tokens))
        updated = dict(tokens)
        updated["access_token"] = "cc-proof-refreshed"
        updated["last_refresh"] = "proof"
        claude_auth.save_tokens(updated, path)
        print("[refresh] 401 -> store refresh (scripted stand-in; wire shape proven in tests)")
        return claude_auth.load_tokens(path)

    claude_auth.refresh = fake_refresh  # the auth flow resolves through this seam
    provider = ClaudeProvider(model="claude-sonnet-4-5", transport=httpx.MockTransport(handler))
    done: LLMResponse | None = None
    try:
        async for event in provider.stream_complete(MESSAGES, max_tokens=64):
            if event.type == "done":
                done = event.response
    finally:
        await provider.close()

    assert done is not None
    print(f"[stream] done model={done.model} content={done.content!r} usage={done.usage}")
    assert done.content == "machina claude proof ok"
    assert attempts == ["Bearer <redacted>", "Bearer <redacted>"]
    assert len(refreshed) == 1
    assert OAUTH_BETAS == "claude-code-20250219,oauth-2025-04-20"
    assert CLAUDE_CODE_UA.startswith("claude-code/")
    print("[pass] hermetic Claude proof ok (import + identity + 401 refresh + stream)")
    print(
        "[manual] sign in for real with: machina auth claude login  (or --import-claude / --manual)"
    )
    return 0


if __name__ == "__main__":
    print(f"started {time.strftime('%Y-%m-%d %H:%M:%S')} (python {sys.version.split()[0]})")
    raise SystemExit(asyncio.run(main()))
