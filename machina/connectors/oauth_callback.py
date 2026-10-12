"""Literal-loopback OAuth callback listener: exact-path, once-only delivery.

Binds only 127.0.0.1. Reserved (inert) until armed with state/issuer.
Wrong state/path/Host never consumes the legitimate attempt.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
from urllib.parse import parse_qsl, urlparse

from machina.connectors.oauth_models import (
    OAUTH_CALLBACK_MAX_BYTES,
    OAUTH_CALLBACK_MAX_PARAMS,
    OAUTH_CALLBACK_MAX_SOCKETS,
    OAUTH_CALLBACK_PARAM_MAX,
    OAUTH_CALLBACK_READ_SECONDS,
    OAuthCallbackResult,
    validate_callback_path,
)

_SUCCESS_HTML = (
    b"<!doctype html><html><head><meta charset=utf-8>"
    b"<meta http-equiv=Cache-Control content=no-store></head>"
    b"<body><p>Authorization received. You may close this window.</p></body></html>"
)
_ERROR_HTML = (
    b"<!doctype html><html><head><meta charset=utf-8>"
    b"<meta http-equiv=Cache-Control content=no-store></head>"
    b"<body><p>Authorization request invalid. You may close this window.</p></body></html>"
)

_HEADERS = (
    "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
    "Content-Length: {n}\r\nCache-Control: no-store\r\n"
    "Referrer-Policy: no-referrer\r\n"
    "Content-Security-Policy: default-src 'none'; frame-ancestors 'none'\r\n"
    "Connection: close\r\n\r\n"
)


def _response(body: bytes) -> bytes:
    return _HEADERS.format(n=len(body)).encode() + body


class LoopbackOAuthCallback:
    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._port = 0
        self._path = f"/oauth/callback/{secrets.token_urlsafe(16)}"
        self._state = ""
        self._issuer = ""
        self._require_iss = False
        self._armed = False
        self._result: asyncio.Future[OAuthCallbackResult] | None = None
        self._consumed = False
        self._invalid_count = 0
        self._sockets = 0
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def reserve(
        cls, *, port: int, deadline: float, path: str | None = None
    ) -> LoopbackOAuthCallback:
        self = cls()
        if path is not None:
            validate_callback_path(path)
            self._path = path
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("callback reservation deadline expired")
        try:
            async with asyncio.timeout(min(max(0.0, remaining), 5.0)):
                self._server = await asyncio.start_server(
                    self._handle, host="127.0.0.1", port=port, limit=OAUTH_CALLBACK_MAX_BYTES
                )
        except OSError as exc:
            raise OSError(f"callback port unavailable: {port}") from exc
        sockets = self._server.sockets or []
        if sockets:
            self._port = sockets[0].getsockname()[1]
        else:
            self._port = port
        self._result = asyncio.get_running_loop().create_future()
        return self

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._port}{self._path}"

    @property
    def port(self) -> int:
        return self._port

    def arm(self, *, state: str, issuer: str, require_iss: bool) -> None:
        self._state = state
        self._issuer = issuer
        self._require_iss = require_iss
        self._armed = True

    async def wait(self, *, deadline: float) -> OAuthCallbackResult:
        assert self._result is not None
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("authorization expired")
        try:
            async with asyncio.timeout(remaining):
                return await asyncio.shield(self._result)
        except TimeoutError as exc:
            raise TimeoutError("authorization expired") from exc

    async def aclose(self) -> None:
        self._closed = True
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        if self._result is not None and not self._result.done():
            self._result.cancel()

    def _validate_host(self, host: str | None) -> bool:
        if not host:
            return False
        # Reject duplicate Host headers upstream; here require exact literal.
        return host.strip().lower() == f"127.0.0.1:{self._port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closed:
            writer.close()
            return
        if self._sockets >= OAUTH_CALLBACK_MAX_SOCKETS:
            writer.close()
            return
        self._sockets += 1
        try:
            try:
                async with asyncio.timeout(OAUTH_CALLBACK_READ_SECONDS):
                    raw = await reader.readuntil(b"\r\n\r\n")
            except (TimeoutError, asyncio.LimitOverrunError, asyncio.IncompleteReadError):
                writer.close()
                return
            if len(raw) > OAUTH_CALLBACK_MAX_BYTES:
                writer.write(_response(_ERROR_HTML))
                await writer.drain()
                writer.close()
                return
            try:
                head = raw.decode("latin-1")
            except ValueError:
                writer.close()
                return
            lines = head.split("\r\n")
            request_line = lines[0] if lines else ""
            parts = request_line.split(" ")
            if len(parts) != 3 or parts[0] != "GET":
                writer.write(_response(_ERROR_HTML))
                await writer.drain()
                writer.close()
                return
            target = parts[1]
            headers: dict[str, list[str]] = {}
            for line in lines[1:]:
                if not line:
                    continue
                if ":" not in line:
                    writer.write(_response(_ERROR_HTML))
                    await writer.drain()
                    writer.close()
                    return
                name, _, value = line.partition(":")
                headers.setdefault(name.strip().lower(), []).append(value.strip())
            hosts = headers.get("host", [])
            if len(hosts) != 1 or not self._validate_host(hosts[0]):
                self._invalid(invalid=True)
                writer.write(_response(_ERROR_HTML))
                await writer.drain()
                writer.close()
                return
            parsed = urlparse(target)
            if parsed.path != self._path:
                self._invalid(invalid=True)
                writer.write(_response(_ERROR_HTML))
                await writer.drain()
                writer.close()
                return
            params = parse_qsl(parsed.query, keep_blank_values=True)
            if len(params) > OAUTH_CALLBACK_MAX_PARAMS:
                self._invalid(invalid=True)
                writer.write(_response(_ERROR_HTML))
                await writer.drain()
                writer.close()
                return
            multi: dict[str, list[str]] = {}
            for key, value in params:
                if len(key) > 256 or len(value) > OAUTH_CALLBACK_PARAM_MAX:
                    self._invalid(invalid=True)
                    writer.write(_response(_ERROR_HTML))
                    await writer.drain()
                    writer.close()
                    return
                multi.setdefault(key, []).append(value)
            for key, values in multi.items():
                if key in ("state", "code", "error", "iss") and len(values) != 1:
                    self._invalid(invalid=True)
                    writer.write(_response(_ERROR_HTML))
                    await writer.drain()
                    writer.close()
                    return
            if not self._armed:
                writer.write(_response(_ERROR_HTML))
                await writer.drain()
                writer.close()
                return
            state = (multi.get("state") or [""])[0]
            code = (multi.get("code") or [""])[0]
            error = (multi.get("error") or [""])[0]
            iss = (multi.get("iss") or [""])[0]
            if not state or not hmac.compare_digest(state, self._state):
                self._invalid(invalid=True)
                writer.write(_response(_ERROR_HTML))
                await writer.drain()
                writer.close()
                return
            if iss:
                if iss != self._issuer:
                    self._invalid(invalid=True)
                    writer.write(_response(_ERROR_HTML))
                    await writer.drain()
                    writer.close()
                    return
            elif self._require_iss:
                self._invalid(invalid=True)
                writer.write(_response(_ERROR_HTML))
                await writer.drain()
                writer.close()
                return
            if code and error:
                self._invalid(invalid=True)
                writer.write(_response(_ERROR_HTML))
                await writer.drain()
                writer.close()
                return
            async with self._lock:
                if self._consumed:
                    writer.write(_response(_ERROR_HTML))
                    await writer.drain()
                    writer.close()
                    return
                self._consumed = True
                assert self._result is not None
                if error:
                    if not self._result.done():
                        self._result.set_result(
                            OAuthCallbackResult(ok=False, error=error, reason="oauth_cancelled")
                        )
                elif code:
                    if not self._result.done():
                        self._result.set_result(
                            OAuthCallbackResult(
                                ok=True, code=code, state=state, issuer=self._issuer
                            )
                        )
                else:
                    self._invalid(invalid=True)
                    writer.write(_response(_ERROR_HTML))
                    await writer.drain()
                    writer.close()
                    self._consumed = False
                    return
            writer.write(_response(_SUCCESS_HTML))
            await writer.drain()
            writer.close()
        finally:
            self._sockets -= 1

    def _invalid(self, *, invalid: bool) -> None:
        if invalid:
            self._invalid_count += 1
