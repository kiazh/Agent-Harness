"""ChatGPT-account sign-in for the gateway (the ``auth.chatgpt.*`` control plane).

Mirrors ``machina auth`` for the UI: read-only status, the browser PKCE login,
the non-interactive Codex CLI import, and logout. The login listener runs in a
background task bounded by :data:`LOGIN_TIMEOUT_SECONDS`: the RPC returns the
authorization URL immediately and the flow settles by emitting one gateway
event — ``auth.chatgpt.completed`` (account id + expiry) or
``auth.chatgpt.failed`` (a stable reason code). A second login while one is
pending is idempotent and returns the pending URL.

No token material ever crosses the wire: status carries
``signedIn/source/expired/expiresAt/accountId/lastRefresh`` only, events carry
an account id, an expiry, and a failure reason, and the callback listener
never logs the request line that carries the one-time code.

``auth.chatgpt.logout`` deletes ONLY Machina's store
(``~/.machina/auth/chatgpt.json``); ``~/.codex/auth.json`` is never touched.
Logout also invalidates pending authentication work (round-3 review R3-2):
the in-flight login flow can no longer exchange or publish, the store's
authentication generation is bumped before removal, and the catalog module's
in-process state for the signed-out account is dropped.
"""

from __future__ import annotations

import asyncio
import hmac
import importlib
import socket
import webbrowser
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from machina.gateway.errors import OPERATION_FAILED, RpcError
from machina.gateway.features._common import _bool
from machina.security import chatgpt_auth

if TYPE_CHECKING:
    from machina.gateway.server import Gateway

# Bounded callback wait; the UI also bounds its own settle wait (see auth.ts).
LOGIN_TIMEOUT_SECONDS = 180.0


@dataclass
class _PendingLogin:
    """The one in-flight login flow (per process; a ChatGPT account is one)."""

    url: str
    state: str
    code_verifier: str
    redirect_uri: str
    task: asyncio.Task[None]


_PENDING: _PendingLogin | None = None

#: Flows logout invalidated; they keep serving their loopback callback (a
#: browser mid-redirect must still be answered) but can never publish. They
#: are asyncio-cancelled when the NEXT login starts, which also frees the
#: fixed callback port for the fresh flow.
_INVALIDATED: list[asyncio.Task[None]] = []


def _flow_is_current() -> bool:
    """True when the running flow task is still the gateway's pending flow."""
    pending = _PENDING
    return pending is not None and pending.task is asyncio.current_task()


def _cancel_pending_login() -> bool:
    """Functionally cancel the in-flight login flow (logout, R3-2).

    Returns True when a flow was pending. The task itself is retired only
    when a fresh login starts (:func:`_retire_invalidated_flows`): a callback
    that is already redirecting is still answered, while the pending identity
    check and the ``exchange_code`` generation fence guarantee it publishes
    nothing.
    """
    global _PENDING
    if _PENDING is None:
        return False
    pending, _PENDING = _PENDING, None
    _INVALIDATED.append(pending.task)
    return True


async def _retire_invalidated_flows() -> None:
    """Cancel logout-invalidated flows and free any listener they hold."""
    tasks = [task for task in _INVALIDATED if not task.done()]
    _INVALIDATED.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _clear_catalog_state() -> None:
    """Drop in-process catalog state after logout (R3-2 catalogue gap).

    The catalog module keeps one in-process live/CLI-cache per account; the
    signed-out account's entries must not keep answering account-less reads
    (``cached_catalog`` / ``live_models_or_none`` fall back to the most
    recent live entry). Bound through importlib so the gateway feature does
    not pull the provider import stack and the deferred-import census gains
    no ``import`` statements.
    """
    with suppress(Exception):
        importlib.import_module("machina.core.provider.catalog").clear_catalog_cache()


def _status_payload() -> dict[str, Any]:
    """JSON-safe status snapshot; explicit fields, never token material."""
    report = chatgpt_auth.status()
    return {
        "signedIn": bool(report.get("signed_in")),
        "source": report.get("source"),
        "expired": report.get("expired"),
        "expiresAt": report.get("expires_at"),
        "accountId": report.get("account_id"),
        "lastRefresh": report.get("last_refresh"),
    }


def _emit(gw: Gateway, event_type: str, **fields: Any) -> None:
    """Write one gateway event; the payload is built (and validated) first.

    A dead channel only drops the frame — the background flow must not crash
    on a disconnect — but a schema error in *our* payload still raises.
    """
    frame = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": gw.event_payload(event_type, "", "", **fields),
    }
    with suppress(Exception):
        gw._write(frame)


def _bind_callback_socket(redirect_uri: str) -> socket.socket:
    """Bind the loopback callback port before returning the URL to the caller.

    Binding synchronously makes a port conflict a typed RPC error instead of a
    late ``auth.chatgpt.failed``, and guarantees the URL is servable the
    moment the client receives it. Loopback only: the callback carries the
    one-time authorization code.
    """
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    bind_host = "127.0.0.1" if host == "localhost" else host
    port = parsed.port or chatgpt_auth.CALLBACK_PORT
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((bind_host, port))
    except OSError as error:
        sock.close()
        raise RpcError(
            OPERATION_FAILED,
            f"cannot listen on {bind_host}:{port} for the sign-in callback "
            f"({error}); close whatever holds that port (another login?) and retry",
        ) from None
    sock.listen(8)
    sock.setblocking(False)
    return sock


class _CallbackListener:
    """One-shot asyncio loopback listener for the OAuth callback.

    The first request carrying a code (or an OAuth error / state mismatch)
    settles the flow; every other request gets a 404. The request line is
    never logged — it carries the one-time code and state — and the state
    check uses :func:`hmac.compare_digest`.
    """

    _STATUS_TEXT = {200: "OK", 400: "Bad Request", 404: "Not Found"}

    def __init__(self, redirect_uri: str, state: str, sock: socket.socket) -> None:
        parsed = urlparse(redirect_uri)
        self.path = parsed.path or chatgpt_auth.CALLBACK_PATH
        self._state = str(state)
        self._sock = sock
        self._server: asyncio.AbstractServer | None = None
        self._settled: asyncio.Future[tuple[str | None, str | None]] | None = None

    async def start(self) -> None:
        self._settled = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(self._handle, sock=self._sock)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        with suppress(Exception):
            await server.wait_closed()

    async def wait(self, timeout: float) -> tuple[str | None, str | None]:
        """(code, None) on success; (None, reason) on error or timeout."""
        assert self._settled is not None
        try:
            return await asyncio.wait_for(self._settled, timeout)
        except TimeoutError:
            return None, "timeout"

    def _settle(self, code: str | None, error: str | None) -> None:
        if self._settled is not None and not self._settled.done():
            self._settled.set_result((code, error))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
            except (
                TimeoutError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
                ConnectionError,
            ):
                # Garbage/oversized requests (or port probers) are dropped
                # without touching the flow: the listener must stay able to
                # serve the real callback (review round 5, F5).
                return
            request_line = raw.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            parts = request_line.split(" ")
            target = parts[1] if len(parts) > 1 else ""
            parsed = urlparse(target)
            if parsed.path != self.path:
                await self._respond(writer, 404, "Not found.")
                return
            query = parse_qs(parsed.query)
            error = (query.get("error") or [""])[0]
            if error:
                self._settle(None, error)
                await self._respond(
                    writer, 200, "Sign-in was not completed. You can close this tab."
                )
                return
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            if not code:
                await self._respond(writer, 400, "Missing authorization code.")
                return
            if not hmac.compare_digest(state.encode("utf-8"), self._state.encode("utf-8")):
                self._settle(None, "state_mismatch")
                await self._respond(
                    writer, 400, "Sign-in state did not match. Run /auth sign-in again."
                )
                return
            self._settle(code, None)
            await self._respond(writer, 200, "Signed in to ChatGPT. You can close this tab.")
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    @classmethod
    async def _respond(cls, writer: asyncio.StreamWriter, status: int, message: str) -> None:
        body = (
            "<!doctype html><html><body style='font-family: system-ui; margin: 4rem auto; "
            f"max-width: 32rem;'><p>{message}</p></body></html>"
        ).encode()
        head = (
            f"HTTP/1.1 {status} {cls._STATUS_TEXT.get(status, 'Error')}\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        with suppress(Exception):
            writer.write(head + body)
            await writer.drain()


async def _run_login_flow(
    gw: Gateway, request: chatgpt_auth.LoginRequest, sock: socket.socket, no_browser: bool
) -> None:
    """Background half of ``auth.chatgpt.login``; always ends in one event."""
    global _PENDING
    generation = chatgpt_auth.credential_generation()
    listener = _CallbackListener(request.redirect_uri, request.state, sock)
    try:
        await listener.start()
        if not no_browser:
            with suppress(Exception):
                await asyncio.wait_for(
                    asyncio.to_thread(webbrowser.open, request.url), timeout=10.0
                )
        code, reason = await listener.wait(LOGIN_TIMEOUT_SECONDS)
        if reason is not None:
            _emit(gw, "auth.chatgpt.failed", reason=reason)
            return
        assert code  # a settled flow without an error always carries a code
        if not _flow_is_current():
            # Logout invalidated this flow: answer the browser-shaped callback
            # but publish nothing (R3-2).
            _emit(gw, "auth.chatgpt.failed", reason="cancelled")
            return
        try:
            tokens = await asyncio.to_thread(
                chatgpt_auth.exchange_code,
                code,
                code_verifier=request.code_verifier,
                redirect_uri=request.redirect_uri,
                generation=generation,
            )
        except chatgpt_auth.ChatGPTAuthError as error:
            _emit(
                gw,
                "auth.chatgpt.failed",
                reason=str(error.code or "") or "chatgpt_exchange_failed",
            )
            return
        except Exception as error:  # noqa: BLE001 — bounded and sanitized
            _emit(gw, "auth.chatgpt.failed", reason=type(error).__name__)
            return
        expiry = tokens.get("expires_at")
        _emit(
            gw,
            "auth.chatgpt.completed",
            account_id=tokens.get("account_id"),
            expiry=(
                float(expiry)
                if isinstance(expiry, (int, float)) and not isinstance(expiry, bool)
                else None
            ),
        )
    finally:
        await listener.stop()
        if _PENDING is not None and _PENDING.task is asyncio.current_task():
            _PENDING = None


# ─── handlers ────────────────────────────────────────────────────────────────


async def auth_chatgpt_status(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Read-only sign-in status (never tokens)."""
    return {"status": _status_payload()}


async def auth_chatgpt_login(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Start the browser PKCE flow; returns the URL immediately.

    Idempotent while a flow is pending: the same URL/state come back and no
    second listener is started. The flow settles later via
    ``auth.chatgpt.completed`` or ``auth.chatgpt.failed``.
    """
    global _PENDING
    no_browser = _bool(params, "no_browser")
    pending = _PENDING
    if pending is not None and not pending.task.done():
        return {"url": pending.url, "state": pending.state}
    # A fresh login supersedes any logout-invalidated flow: cancel it and
    # reap its listener so the fixed callback port is free to bind.
    await _retire_invalidated_flows()
    request = chatgpt_auth.login_url()
    sock = _bind_callback_socket(request.redirect_uri)
    task = asyncio.create_task(
        _run_login_flow(gw, request, sock, no_browser), name="chatgpt-login-flow"
    )
    _PENDING = _PendingLogin(
        url=request.url,
        state=request.state,
        code_verifier=request.code_verifier,
        redirect_uri=request.redirect_uri,
        task=task,
    )
    return {"url": request.url, "state": request.state}


async def auth_chatgpt_logout(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Remove Machina's ChatGPT token store ONLY.

    The Codex CLI's ``~/.codex/auth.json`` is never touched, so a user who ran
    ``codex login`` stays importable — the returned status states that plainly
    (``source`` flips to ``codex`` when the peek finds those credentials).

    Pending authentication work is invalidated first (R3-2): the in-flight
    login flow can no longer exchange or publish, the store's generation is
    bumped under its lock before the unlink, and the catalog module's
    in-process state is dropped.
    """
    path = chatgpt_auth.auth_path()
    _cancel_pending_login()
    try:
        removed = await asyncio.to_thread(chatgpt_auth.logout, path=path)
    except OSError as error:
        raise RpcError(
            OPERATION_FAILED,
            f"could not remove the Machina ChatGPT token store ({type(error).__name__})",
        ) from None
    _clear_catalog_state()
    return {"removed": removed, "status": _status_payload()}


async def auth_chatgpt_import_codex(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Non-interactive import of the Codex CLI's tokens; returns the status.

    ``imported`` is False when there is nothing to import (no Codex file) or
    when a Machina store already exists (it always wins unless forced).
    """
    tokens = await asyncio.to_thread(chatgpt_auth.import_from_codex)
    return {"imported": tokens is not None, "status": _status_payload()}
