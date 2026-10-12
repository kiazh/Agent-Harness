"""Local OAuth issuer + protected real-SDK MCP fixture (loopback only).

One ASGI app on an owned ephemeral loopback socket: OAuth metadata,
dynamic registration, authorization, token, revocation, plus a real MCP
server (SDK wire traffic) behind access-token enforcement.

Fault switches (in-test knobs, never live providers):
    require_registration (default True), registration_auth_method,
    missing_pkce (omit S256 advertisement), plain_only_pkce,
    no_registration_endpoint, incompatible_registration (return none-incompatible),
    scope_widening (grant broader scope than requested),
    no_expiry (omit expires_in), malformed_token (non-JSON token body),
    invalid_grant_refresh (refresh returns invalid_grant),
    lost_exchange_response (consume code, then drop the response),
    lost_refresh_response (consume rotating refresh, then drop),
    suspend_exchange (event gate held while config changes),
    revoke_on_mutate (server-side revocation after one counted effect),
    auth_failure_status (401 default, or 403 insufficient_scope),
    drop_after_effect, timeout_after_effect (client-side timeout, unknown),
    revocation_mode ("ok" | "unsupported" | "failed").

Each fixture binds literal loopback, has a per-test UUID name, receives no
real keys, and stops in ``finally`` via :meth:`OAuthFixture.stop`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import socket
import urllib.parse
from typing import Any


class OAuthFixture:
    def __init__(self, name: str = "") -> None:
        import uuid as _uuid

        self.name = name or f"fixture-{_uuid.uuid4().hex[:8]}"
        self.counters = {
            "authorize": 0,
            "exchange": 0,
            "refresh": 0,
            "register": 0,
            "revoke": 0,
            "tool_calls": 0,
            "mutate_effects": 0,
        }
        self.switches: dict[str, Any] = {
            "require_registration": True,
            "registration_auth_method": "none",
            "missing_pkce": False,
            "plain_only_pkce": False,
            "no_registration_endpoint": False,
            "incompatible_registration": False,
            "scope_widening": False,
            "no_expiry": False,
            "malformed_token": False,
            "invalid_grant_refresh": False,
            "lost_exchange_response": False,
            "lost_refresh_response": False,
            "revoke_on_mutate": False,
            "auth_failure_status": 401,
            "drop_after_effect": False,
            "timeout_after_effect": False,
            "revocation_mode": "ok",
        }
        self.suspend_exchange = asyncio.Event()
        self.suspend_exchange.set()
        self._codes: dict[str, dict[str, Any]] = {}
        self._access: dict[str, dict[str, Any]] = {}
        self._refresh: dict[str, dict[str, Any]] = {}
        self._clients: dict[str, dict[str, Any]] = {}
        self._preregistered: dict[str, str] = {}  # client_id -> secret
        self._preregistered_redirects: dict[str, list[str]] = {}
        self.port = 0
        self._sock: socket.socket | None = None
        self._server = None
        self._task: asyncio.Task | None = None
        self.base = ""
        self.issuer = ""
        self.endpoint = ""

    # ── setup ───────────────────────────────────────────────────
    def preregister(self, client_id: str, *, redirect_uri: str, secret: str = "") -> None:
        self._preregistered[client_id] = secret
        self._preregistered_redirects[client_id] = [redirect_uri]

    async def start(self) -> OAuthFixture:
        from mcp.server.mcpserver import MCPServer

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(100)
        self._sock.setblocking(False)
        self.port = self._sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.issuer = f"{self.base}/auth"
        self.endpoint = f"{self.base}/mcp"
        server = MCPServer(name=f"fixture-{self.name}")
        fixture = self

        @server.tool()
        async def echo(text: str) -> str:
            """Echo text back."""
            fixture.counters["tool_calls"] += 1
            return text

        @server.tool()
        async def mutate(payload: str = "") -> str:
            """Side-effecting probe: counts, then fails per switches."""
            fixture.counters["tool_calls"] += 1
            fixture.counters["mutate_effects"] += 1
            if fixture.switches["revoke_on_mutate"]:
                # Server-side revocation after the effect: the NEXT transport
                # request fails with a real HTTP auth status (no replay).
                fixture._access.clear()
                return f"mutated:{payload}"
            if fixture.switches["drop_after_effect"]:
                await asyncio.sleep(12)
                return "dropped"
            if fixture.switches["timeout_after_effect"]:
                await asyncio.sleep(12)
                return "timeout"
            return f"mutated:{payload}"

        mcp_app = server.streamable_http_app()
        app = self._build_app(mcp_app)
        import uvicorn

        config = uvicorn.Config(app, fd=self._sock.fileno(), host="127.0.0.1", log_level="warning")
        self._server = uvicorn.Server(config)
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._server.serve())
        for _ in range(100):
            if self._server.started:
                break
            await asyncio.sleep(0.05)
        if not self._server.started:
            raise RuntimeError("oauth fixture failed to start")
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._server = None
        self._task = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ── app ─────────────────────────────────────────────────────
    def _build_app(self, mcp_app):
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
        from starlette.routing import Route

        fixture = self

        def _prm_payload():
            return {
                "resource": fixture.endpoint,
                "authorization_servers": [fixture.issuer],
                "scopes_supported": ["read", "write"],
            }

        def _server_meta():
            meta: dict[str, Any] = {
                "issuer": fixture.issuer,
                "authorization_endpoint": f"{fixture.issuer}/authorize",
                "token_endpoint": f"{fixture.issuer}/token",
                "response_types_supported": ["code"],
                "authorization_response_iss_parameter_supported": True,
            }
            if fixture.switches["missing_pkce"]:
                pass
            elif fixture.switches["plain_only_pkce"]:
                meta["code_challenge_methods_supported"] = ["plain"]
            else:
                meta["code_challenge_methods_supported"] = ["S256"]
            if not fixture.switches["no_registration_endpoint"]:
                meta["registration_endpoint"] = f"{fixture.issuer}/register"
            if fixture.switches["revocation_mode"] != "unsupported":
                meta["revocation_endpoint"] = f"{fixture.issuer}/revoke"
            return meta

        async def _well_known(request: Request):
            return JSONResponse(_server_meta())

        async def _prm(request: Request):
            return JSONResponse(_prm_payload())

        async def _register(request: Request):
            fixture.counters["register"] += 1
            try:
                body = await request.json()
            except ValueError:
                return JSONResponse({"error": "invalid_request"}, status_code=400)
            redirects = body.get("redirect_uris") or []
            if fixture.switches["incompatible_registration"]:
                return JSONResponse(
                    {"client_id": "x", "token_endpoint_auth_method": "client_secret_basic"},
                    status_code=200,
                )
            if not redirects or not all(r.startswith("http://127.0.0.1:") for r in redirects):
                return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
            if body.get("token_endpoint_auth_method", "none") != "none":
                return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
            client_id = f"dyn-{secrets.token_hex(6)}"
            fixture._clients[client_id] = {"redirects": redirects}
            return JSONResponse({"client_id": client_id, "token_endpoint_auth_method": "none"})

        async def _authorize(request: Request):
            fixture.counters["authorize"] += 1
            params = dict(request.query_params)
            client_id = params.get("client_id", "")
            redirect = params.get("redirect_uri", "")
            resource = params.get("resource", "")
            challenge = params.get("code_challenge", "")
            method = params.get("code_challenge_method", "")
            state = params.get("state", "")
            scope = params.get("scope", "")
            if client_id not in fixture._clients and client_id not in fixture._preregistered:
                return JSONResponse({"error": "unauthorized_client"}, status_code=400)
            known = fixture._clients.get(client_id, {}).get("redirects", [])
            known = fixture._preregistered_redirects.get(client_id, known)
            if redirect not in known:
                return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
            if resource != fixture.endpoint:
                return JSONResponse({"error": "invalid_target"}, status_code=400)
            if method != "S256" or not challenge:
                return JSONResponse({"error": "invalid_request"}, status_code=400)
            code = f"code-{secrets.token_hex(8)}"
            fixture._codes[code] = {
                "client_id": client_id,
                "redirect": redirect,
                "challenge": challenge,
                "resource": resource,
                "scope": scope,
                "used": False,
            }
            location = (
                f"{redirect}?code={urllib.parse.quote(code)}"
                f"&state={urllib.parse.quote(state)}"
                f"&iss={urllib.parse.quote(fixture.issuer)}"
            )
            return RedirectResponse(location, status_code=302)

        def _mint(client_id: str, scope: str):
            access = f"access-{secrets.token_hex(12)}"
            refresh = f"refresh-{secrets.token_hex(12)}"
            granted = scope
            if fixture.switches["scope_widening"]:
                granted = (scope + " admin").strip()
            fixture._access[access] = {"client_id": client_id, "scope": granted}
            fixture._refresh[refresh] = {"client_id": client_id, "scope": scope}
            return access, refresh, granted

        async def _token(request: Request):
            form = dict(await request.form())
            grant = form.get("grant_type", "")
            client_id = form.get("client_id", "")
            expected_secret = fixture._preregistered.get(client_id, "")
            if expected_secret:
                try:
                    authorization = request.headers.get("authorization", "")
                    if not authorization.startswith("Basic "):
                        raise ValueError("basic auth required")
                    pair = base64.b64decode(authorization[6:], validate=True).decode("ascii")
                    encoded_id, encoded_secret = pair.split(":", 1)
                    actual_id = urllib.parse.unquote_plus(encoded_id)
                    actual_secret = urllib.parse.unquote_plus(encoded_secret)
                    if actual_id != client_id or not hmac.compare_digest(
                        actual_secret.encode(), expected_secret.encode()
                    ):
                        raise ValueError("client credentials mismatch")
                except (ValueError, UnicodeError):
                    return JSONResponse({"error": "invalid_client"}, status_code=401)
            if grant == "authorization_code":
                fixture.counters["exchange"] += 1
                await fixture.suspend_exchange.wait()
                code = form.get("code", "")
                entry = fixture._codes.get(code)
                if entry is None or entry["used"]:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                entry["used"] = True
                if fixture.switches["lost_exchange_response"]:
                    # The code is consumed server-side; the response never
                    # arrives. A retry must not re-use the code.
                    await asyncio.sleep(12)
                    return JSONResponse({"error": "late"}, status_code=500)
                verifier = form.get("code_verifier", "")
                digest = hashlib.sha256(verifier.encode()).digest()
                expect = base64.urlsafe_b64encode(digest).decode().rstrip("=")
                if expect != entry["challenge"]:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                if (
                    form.get("redirect_uri", "") != entry["redirect"]
                    or form.get("client_id", "") != entry["client_id"]
                    or form.get("resource", "") != entry["resource"]
                ):
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                if fixture.switches["malformed_token"]:
                    return Response("not-json", media_type="application/json")
                access, refresh, granted = _mint(entry["client_id"], entry["scope"])
                payload: dict[str, Any] = {
                    "access_token": access,
                    "token_type": "Bearer",
                    "scope": granted,
                }
                if not fixture.switches["no_expiry"]:
                    payload["expires_in"] = 3600
                payload["refresh_token"] = refresh
                return JSONResponse(payload)
            if grant == "refresh_token":
                fixture.counters["refresh"] += 1
                if fixture.switches["invalid_grant_refresh"]:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                old = form.get("refresh_token", "")
                entry = fixture._refresh.get(old)
                if entry is None:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                # Rotation: consume the old refresh token.
                del fixture._refresh[old]
                if fixture.switches["lost_refresh_response"]:
                    await asyncio.sleep(12)
                    return JSONResponse({"error": "late"}, status_code=500)
                access, refresh, granted = _mint(entry["client_id"], entry["scope"])
                payload = {
                    "access_token": access,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": refresh,
                    "scope": granted,
                }
                return JSONResponse(payload)
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

        async def _revoke(request: Request):
            fixture.counters["revoke"] += 1
            mode = fixture.switches["revocation_mode"]
            if mode == "failed":
                return JSONResponse({"error": "server_error"}, status_code=500)
            form = dict(await request.form())
            token = form.get("token", "")
            fixture._access.pop(token, None)
            fixture._refresh.pop(token, None)
            return Response(status_code=200)

        async def _bearer_guard(scope, receive, send, app):
            from starlette.requests import Request as _Req

            request = _Req(scope, receive)
            path = scope.get("path", "")
            if path.startswith("/mcp"):
                authorization = request.headers.get("authorization", "")
                token = authorization[7:] if authorization.startswith("Bearer ") else ""
                entry = fixture._access.get(token)
                if entry is None:
                    status = int(fixture.switches.get("auth_failure_status", 401) or 401)
                    metadata_url = f"{fixture.base}/.well-known/oauth-protected-resource/mcp"
                    if status == 403:
                        response = PlainTextResponse("insufficient_scope", status_code=403)
                    else:
                        response = PlainTextResponse("unauthorized", status_code=401)
                    response.headers["WWW-Authenticate"] = (
                        f'Bearer resource_metadata="{metadata_url}", scope="read write"'
                        + (', error="insufficient_scope"' if status == 403 else "")
                    )
                    await response(scope, receive, send)
                    return
            await app(scope, receive, send)

        routes = [
            Route("/.well-known/oauth-protected-resource/mcp", _prm),
            Route("/.well-known/oauth-protected-resource", _prm),
            Route("/.well-known/oauth-authorization-server/auth", _well_known),
            Route("/.well-known/openid-configuration/auth", _well_known),
            Route("/auth/.well-known/openid-configuration", _well_known),
            Route("/auth/register", _register, methods=["POST"]),
            Route("/auth/authorize", _authorize, methods=["GET"]),
            Route("/auth/token", _token, methods=["POST"]),
            Route("/auth/revoke", _revoke, methods=["POST"]),
            # Splice the real SDK routes in (the SDK app owns absolute /mcp):
            # nesting whole Starlette apps would skip the SDK lifespan that
            # initializes its session task group.
            *mcp_app.routes,
        ]
        from contextlib import asynccontextmanager as _acm

        _mcp_lifespan = mcp_app.router.lifespan_context

        @_acm
        async def _lifespan(app):
            async with _mcp_lifespan(app):
                yield

        app = Starlette(routes=routes, lifespan=_lifespan)
        app = _wrap(app, _bearer_guard)
        return app

    @staticmethod
    def _unused():
        return None


def _wrap(app, guard):
    class _Middleware:
        def __init__(self, inner):
            self._inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] not in ("http", "websocket"):
                await self._inner(scope, receive, send)
                return
            await guard(scope, receive, send, self._inner)

    return _Middleware(app)
