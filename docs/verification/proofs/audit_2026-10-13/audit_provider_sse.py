#!/usr/bin/env python
"""Audit finding: HTTP 200 SSE with one partial text frame and clean EOF (no
finish frame, no [DONE]) yields text followed by a successful 'done'."""
import asyncio
import sys
from pathlib import Path

REPO = Path("/Users/shadow/machina")
sys.path.insert(0, str(REPO))

import httpx

from machina.core.provider import PROVIDER_SPECS, DirectProvider


def handler(request: httpx.Request) -> httpx.Response:
    body = (
        b'data: {"model":"audit-model","choices":'
        b'[{"delta":{"content":"partial text"},"finish_reason":null}]}\n\n'
    )
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


async def main() -> None:
    provider = DirectProvider(spec=PROVIDER_SPECS["deepseek"], api_key="test-key",
                              model="audit-model")
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=provider.spec.base_url
    )
    events = []
    async for event in provider.stream_complete(
        [{"role": "user", "content": "hi"}], model="audit-model"
    ):
        events.append(event)
    print("event types:", [e.type for e in events])
    for event in events:
        if event.type == "text":
            print("text:", repr(event.content))
        if event.type == "done":
            print("done content:", repr(event.response.content))
    types = [e.type for e in events]
    confirmed = types == ["text", "done"]
    print("CLAIM CONFIRMED (clean EOF without finish frame reported as done)"
          if confirmed else "CLAIM NOT REPRODUCED")
    await provider.client.aclose()
    sys.exit(0 if confirmed else 1)


asyncio.run(main())
