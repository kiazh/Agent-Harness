"""Per-client HTTP request throttling for the API."""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, requests_per_minute: int | None = None) -> None:
        super().__init__(app)
        self.limit = (
            requests_per_minute
            if requests_per_minute is not None
            else int(os.environ.get("AGENT_HARNESS_HTTP_RATE_LIMIT", "120"))
        )
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def dispatch(self, request: Request, call_next):
        if request.url.path in {"/health", "/ready"} or self.limit <= 0:
            return await call_next(request)
        # Use the transport peer, never an untrusted forwarded header.
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        async with self._lock:
            calls = self._calls[client]
            while calls and now - calls[0] >= 60:
                calls.popleft()
            if len(calls) >= self.limit:
                return JSONResponse(
                    {"detail": "rate limit exceeded"},
                    status_code=429,
                    headers={"Retry-After": str(max(1, int(60 - (now - calls[0]))))},
                )
            calls.append(now)
            if len(self._calls) > 10_000:
                self._calls = defaultdict(
                    deque,
                    {
                        key: value
                        for key, value in self._calls.items()
                        if value and now - value[-1] < 60
                    },
                )
        return await call_next(request)
