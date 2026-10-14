"""Copilot subscription — LIVE proof against api.githubcopilot.com (2026-10-14).

Runs the real ``CopilotProvider`` transport on this machine's own ``gh``
credential (read via ``gh auth token``; the gh store is never modified) and
records:

1. the import path: ``resolve_tokens`` imports the gh token into the Machina
   store (``~/.machina/auth/copilot.json``, 0600) and reports token-free status;
2. a live streaming completion through the real transport (SSE, terminal
   finish_reason + [DONE]);
3. a live tool-call completion (function-call accumulation);
4. the account's live model menu (``GET /models``) and the legacy
   ``copilot_internal/v2/token`` exchange answering HTTP 403 for
   non-first-party clients (why the provider does not use it).

No token value is printed or written into the log; ``Authorization`` is shown
redacted. Exit codes: 0 pass, 2 no gh credential (skip), 1 failure.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import time

import httpx

from machina.core.models import LLMResponse, ToolDefinition
from machina.core.provider.copilot import CopilotProvider
from machina.security import copilot_auth

MESSAGES = [
    {"role": "system", "content": "Be brief."},
    {"role": "user", "content": "Reply with exactly one short sentence: machina copilot proof ok"},
]
TOOLS = [
    ToolDefinition(
        "get_weather",
        "Look up current weather for a city",
        {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    )
]
TOOL_MESSAGES = [
    {"role": "user", "content": "What is the weather in Paris? Call get_weather."},
]

_TOKEN = subprocess.run(
    ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
).stdout.strip()


def _print(msg: str) -> None:
    assert _TOKEN not in msg, "a proof line tried to print the token"
    print(msg, flush=True)


def probe_models() -> None:
    headers = {
        "Authorization": f"Bearer {_TOKEN}",
        "User-Agent": "GitHubCopilotChat/0.26.7",
        "Copilot-Integration-Id": "vscode-chat",
        "Editor-Version": "vscode/1.104.1",
    }
    with httpx.Client(timeout=20) as client:
        resp = client.get("https://api.githubcopilot.com/models", headers=headers)
        _print(f"[wire] GET /models -> HTTP {resp.status_code}")
        if resp.status_code == 200:
            models = resp.json().get("data", [])
            names = sorted(str(m.get("id")) for m in models if isinstance(m, dict))
            _print(f"[wire] live model menu: {len(names)} models; sample={names[:6]}")
        # Legacy exchange: documented as blocked for non-first-party clients.
        legacy = client.get(
            "https://api.github.com/copilot_internal/v2/token",
            headers={
                "Authorization": f"token {_TOKEN}",
                "User-Agent": "GitHubCopilotChat/0.26.7",
                "Accept": "application/json",
                "Editor-Version": "vscode/1.104.1",
            },
        )
        _print(f"[wire] legacy copilot_internal/v2/token -> HTTP {legacy.status_code} (not used)")


async def run_stream(provider: CopilotProvider, messages, tools, label) -> LLMResponse | None:
    done: LLMResponse | None = None
    async for event in provider.stream_complete(messages, tools=tools, max_tokens=64):
        if event.type == "text":
            _print(f"[{label}] text delta: {event.content!r}")
        elif event.type == "done":
            done = event.response
    if done is None:
        _print(f"[{label}] no terminal response")
        return None
    _print(
        f"[{label}] done model={done.model} content={done.content!r} "
        f"tool_calls={len(done.tool_calls)} usage={json.dumps(done.usage, sort_keys=True)}"
    )
    if done.tool_calls:
        _print(f"[{label}] tool calls: {json.dumps(done.tool_calls, sort_keys=True)}")
    return done


async def main() -> int:
    if not _TOKEN or not _TOKEN.startswith(("gho_", "ghu_", "github_pat_")):
        _print("[skip] no usable gh credential on this machine (`gh auth login` first)")
        return 2
    _print(
        f"[auth] gh token present: prefix={_TOKEN[:4]} len={len(_TOKEN)} "
        f"fingerprint={hashlib.sha256(_TOKEN.encode()).hexdigest()[:16]} (value never printed)"
    )

    before = copilot_auth.status(probe_gh=False)
    _print(f"[auth] machina store before: {json.dumps(before, sort_keys=True)}")

    probe_models()

    provider = CopilotProvider(model="gpt-4o")
    try:
        # resolve_tokens performs the read-only gh import on first use.
        tokens = await provider._resolve_tokens()
        _print(
            f"[auth] resolved token family={tokens['token_family']} "
            f"account={tokens.get('account')} store={copilot_auth.auth_path()}"
        )
        after = copilot_auth.status()
        _print(f"[auth] machina store after: {json.dumps(after, sort_keys=True)}")
        if after.get("token_family") == "gho" and _TOKEN[:8] in json.dumps(after):
            _print("[fail] status leaked token material")
            return 1

        text = await run_stream(provider, MESSAGES, None, "text")
        tool = await run_stream(provider, TOOL_MESSAGES, TOOLS, "tool")
        if text is None or tool is None or not tool.tool_calls:
            _print("[fail] a live call did not complete as expected")
            return 1
        _print("[pass] live Copilot subscription calls completed through the provider")
        return 0
    finally:
        await provider.close()


if __name__ == "__main__":
    started = time.time()
    print(f"started {time.strftime('%Y-%m-%d %H:%M:%S')} (python {sys.version.split()[0]})")
    code = asyncio.run(main())
    print(f"exit={code} elapsed={time.time() - started:.1f}s")
    raise SystemExit(code)
