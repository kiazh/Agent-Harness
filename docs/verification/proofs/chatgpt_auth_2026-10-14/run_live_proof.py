#!/usr/bin/env python
"""Live proof: a real Machina turn on the ChatGPT subscription (Codex protocol).

Runs the *new* provider (`machina.core.provider.chatgpt.ChatGPTProvider`)
against the real ChatGPT backend, using the machine's existing Codex CLI
credentials. Evidence is written to ``live_completion.log`` next to this
script. Token values never reach stdout or the log: the request hook prints
headers with ``authorization`` redacted, and the provider never logs tokens.

Usage (from the repo root, with the repo venv):

    .venv/bin/python docs/verification/proofs/chatgpt_auth_2026-10-14/run_live_proof.py

Exit code 0 = both calls (text + tool) completed with a terminal
``response.completed``; 1 = honest failure (captured in the log).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

PROOF_DIR = Path(__file__).resolve().parent
LOG_PATH = PROOF_DIR / "live_completion.log"
MODEL = "gpt-6.1-sol"

# Make the repo importable when run as a plain script (not via pytest).
for candidate in (PROOF_DIR, *PROOF_DIR.parents):
    if (candidate / "pyproject.toml").is_file() and (candidate / "machina").is_dir():
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        break

import httpx  # noqa: E402

from machina.core.models import ToolDefinition  # noqa: E402
from machina.core.provider.chatgpt import ChatGPTProvider  # noqa: E402
from machina.security import chatgpt_auth  # noqa: E402

_LINES: list[str] = []


def emit(line: str = "") -> None:
    print(line)
    _LINES.append(line)


def redact_headers(headers) -> dict[str, str]:
    out = {k: v for k, v in headers.items()}
    if "authorization" in out:
        out["authorization"] = "Bearer <redacted>"
    return out


async def request_hook(request: httpx.Request) -> None:
    emit("--- request evidence (tokens redacted) ---")
    emit(f"{request.method} {request.url}")
    emit(json.dumps(redact_headers(request.headers), indent=2, sort_keys=True))
    try:
        body = json.loads(request.content)
    except Exception:
        body = {"<unparseable>": True}
    emit("body:")
    emit(json.dumps(body, indent=2))


_RESPONSE_EVIDENCE_HEADERS = (
    "content-type",
    "x-codex-active-limit",
    "x-codex-plan-type",
    "x-codex-primary-used-percent",
    "x-codex-secondary-used-percent",
    "x-oai-request-id",
)


async def response_hook(response: httpx.Response) -> None:
    evidence = {
        "status": response.status_code,
        "headers": {
            name: response.headers[name]
            for name in _RESPONSE_EVIDENCE_HEADERS
            if name in response.headers
        },
    }
    emit(f"--- response evidence --- {json.dumps(evidence, sort_keys=True)}")


async def run_text_call() -> bool:
    emit("=== call 1/2: plain text completion ===")
    provider = ChatGPTProvider(model=MODEL)
    provider.client = httpx.AsyncClient(
        base_url=ChatGPTProvider.BASE_URL,
        event_hooks={"request": [request_hook], "response": [response_hook]},
        timeout=httpx.Timeout(120.0, connect=15.0),
    )
    started = time.monotonic()
    text_parts: list[str] = []
    done = None
    try:
        async for event in provider.stream_complete(
            [
                {
                    "role": "user",
                    "content": "Reply with exactly one short sentence: machina chatgpt proof ok",
                }
            ]
        ):
            if event.type == "text":
                text_parts.append(event.content)
            elif event.type == "done":
                done = event.response
    finally:
        await provider.close()
    elapsed = time.monotonic() - started
    if done is None:
        emit("FAIL: stream ended without a done event")
        return False
    emit(f"[streamed text] {''.join(text_parts)!r}")
    emit(f"[terminal response] content={done.content!r}")
    emit(f"[terminal response] model={done.model}")
    emit(f"[usage] {json.dumps(done.usage, sort_keys=True)}")
    emit(f"[wall seconds] {elapsed:.2f}")
    return True


async def run_tool_call() -> bool:
    emit("")
    emit("=== call 2/2: tool-call streaming ===")
    provider = ChatGPTProvider(model=MODEL)
    provider.client = httpx.AsyncClient(
        base_url=ChatGPTProvider.BASE_URL,
        event_hooks={"request": [request_hook], "response": [response_hook]},
        timeout=httpx.Timeout(120.0, connect=15.0),
    )
    tools = [
        ToolDefinition(
            "get_weather",
            "Look up current weather for a city",
            {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        )
    ]
    done = None
    try:
        async for event in provider.stream_complete(
            [{"role": "user", "content": "What is the weather in Paris? Call get_weather."}],
            tools=tools,
        ):
            if event.type == "text":
                emit(f"[delta] {event.content!r}")
            elif event.type == "done":
                done = event.response
    finally:
        await provider.close()
    if done is None:
        emit("FAIL: stream ended without a done event")
        return False
    emit(f"[tool_calls] {json.dumps(done.tool_calls, indent=2)}")
    emit(f"[usage] {json.dumps(done.usage, sort_keys=True)}")
    return bool(done.tool_calls)


async def main() -> int:
    emit(f"# Live ChatGPT-account proof — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    emit(f"# provider module: {ChatGPTProvider.__module__}  model: {MODEL}")
    status = chatgpt_auth.status()
    emit(
        "# auth status (no token material): "
        + json.dumps({k: status[k] for k in ("signed_in", "source", "expired", "account_id")})
    )
    ok_text = False
    ok_tool = False
    try:
        ok_text = await run_text_call()
        ok_tool = await run_tool_call()
    except Exception as exc:  # honest failure evidence, never a traceback to console only
        import traceback

        emit(f"# EXCEPTION {type(exc).__name__}: {exc}")
        emit(traceback.format_exc())
    emit("")
    emit(
        f"# RESULT text_call={'PASS' if ok_text else 'FAIL'} tool_call={'PASS' if ok_tool else 'FAIL'}"
    )
    LOG_PATH.write_text("\n".join(_LINES) + "\n", encoding="utf-8")
    return 0 if (ok_text and ok_tool) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
