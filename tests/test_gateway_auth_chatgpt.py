"""Gateway ``auth.chatgpt.*`` handlers: status payloads, the immediate login
return with terminal events, pending idempotency, logout scope, and import.

No real network and no real token writes: HOME/CODEX_HOME are sandboxed, the
exchange is monkeypatched, and the callback is driven by a local loopback
HTTP request against the listener the handler itself started.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import time
import urllib.error
import urllib.request
from contextlib import suppress

import httpx
import pytest

from machina.core.config import config
from machina.gateway.errors import OPERATION_FAILED, RpcError
from machina.gateway.features import auth_chatgpt as feature
from machina.gateway.server import Gateway
from machina.protocol import validate_event
from machina.security import chatgpt_auth
from machina.security.chatgpt_auth import LoginRequest


def _jwt(payload: dict) -> str:
    def b64(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(payload)}.sig"


def _tokens(*, account: str = "acct-123", refresh: str = "rt-old", exp_in: float = 3600.0) -> dict:
    auth = {"https://api.openai.com/auth": {"chatgpt_account_id": account}}
    return {
        "access_token": _jwt({**auth, "exp": time.time() + exp_in}),
        "refresh_token": refresh,
        "id_token": _jwt(auth),
    }


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Fake HOME with no Machina store and no Codex CLI credentials."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MACHINA_CHATGPT_AUTH_FILE", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("MACHINA_HOME", raising=False)
    monkeypatch.delenv("MACHINA_MACHINA_HOME", raising=False)
    monkeypatch.setattr(config, "machina_home", "", raising=False)
    return home


@pytest.fixture(autouse=True)
async def no_pending_login():
    """No flow leaks across tests; a live listener is always cancelled."""
    feature._PENDING = None
    feature._INVALIDATED.clear()
    yield
    pending = feature._PENDING
    feature._PENDING = None
    feature._INVALIDATED.clear()
    if pending is not None and not pending.task.done():
        pending.task.cancel()
        with suppress(asyncio.CancelledError):
            await pending.task


class _Harness:
    """A real Gateway with a frame-collecting writer (v2-negotiated client)."""

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.gw = Gateway(self.frames.append)
        # A real UI negotiates protocol 2 during initialize; the canonical
        # event envelope (and validate_event) targets that version.
        self.gw.protocol_version = 2

    async def call(self, method: str, params: dict | None = None) -> dict:
        return await self.gw._methods[method](params or {})

    def events(self) -> list[dict]:
        return [frame["params"] for frame in self.frames if frame.get("method") == "event"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _open_callback(url: str, *, retries: float = 5.0) -> tuple[int, str]:
    """GET *url* against the handler's listener, retrying while it binds."""
    deadline = time.monotonic() + retries
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:  # loopback only
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()
        except OSError as error:
            last = error
            time.sleep(0.02)
    raise AssertionError(f"callback listener never answered {url}: {last}")


def _install_login(monkeypatch, port: int, *, state: str = "gw-state") -> None:
    monkeypatch.setattr(
        chatgpt_auth,
        "login_url",
        lambda: LoginRequest(
            url=f"https://auth.example/authorize?state={state}",
            state=state,
            code_verifier="gw-verifier",
            redirect_uri=f"http://127.0.0.1:{port}/auth/callback",
        ),
    )


# ─── status ──────────────────────────────────────────────────────────────────


async def test_status_payload_is_camel_case_and_never_tokens(fake_home):
    _write = fake_home / ".codex"
    _write.mkdir()
    tokens = _tokens(account="acct-codex")
    (_write / "auth.json").write_text(json.dumps({"tokens": tokens}))
    harness = _Harness()

    result = await harness.call("auth.chatgpt.status")

    assert result == {
        "status": {
            "signedIn": True,
            "source": "codex",
            "expired": False,
            "expiresAt": pytest.approx(time.time() + 3600, abs=30),
            "accountId": "acct-codex",
            "lastRefresh": None,
        }
    }
    wire = json.dumps(result)
    assert tokens["access_token"] not in wire
    assert tokens["refresh_token"] not in wire
    # A read-only peek never creates the Machina store.
    assert not (fake_home / ".machina").exists()


async def test_status_signed_out_is_explicit(fake_home):
    harness = _Harness()
    result = await harness.call("auth.chatgpt.status")
    assert result["status"]["signedIn"] is False
    assert result["status"]["source"] is None
    assert result["status"]["accountId"] is None


# ─── login ───────────────────────────────────────────────────────────────────


def _counting_login(monkeypatch, port: int, calls: list[str]) -> None:
    """Install a login_url that also records each fresh-flow start."""
    _install_login(monkeypatch, port)
    installed = chatgpt_auth.login_url

    def wrapped():
        calls.append("login_url")
        return installed()

    monkeypatch.setattr(chatgpt_auth, "login_url", wrapped)


async def test_login_returns_url_immediately_and_emits_completed(fake_home, monkeypatch):
    port = _free_port()
    _install_login(monkeypatch, port)
    fixed_expiry = time.time() + 7200
    seen: dict[str, str] = {}

    def fake_exchange(code, *, code_verifier, redirect_uri, **kwargs):
        seen.update(code=code, code_verifier=code_verifier, redirect_uri=redirect_uri)
        return {**_tokens(account="acct-gw"), "account_id": "acct-gw", "expires_at": fixed_expiry}

    monkeypatch.setattr(chatgpt_auth, "exchange_code", fake_exchange)
    harness = _Harness()

    result = await harness.call("auth.chatgpt.login", {"no_browser": True})
    pending = feature._PENDING
    assert pending is not None

    assert result == {"url": "https://auth.example/authorize?state=gw-state", "state": "gw-state"}
    # The listener is bound before the URL is returned: connecting works now.
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        pass
    status, _ = await asyncio.to_thread(
        _open_callback, f"http://127.0.0.1:{port}/auth/callback?code=gw-code&state=gw-state"
    )
    assert status == 200

    await asyncio.wait_for(pending.task, timeout=5)

    assert seen == {
        "code": "gw-code",
        "code_verifier": "gw-verifier",
        "redirect_uri": f"http://127.0.0.1:{port}/auth/callback",
    }
    events = harness.events()
    assert [event["type"] for event in events] == ["auth.chatgpt.completed"]
    event = events[0]
    validate_event(event)
    assert set(event) == {"protocolVersion", "type", "sessionId", "turnId", "account_id", "expiry"}
    assert event["account_id"] == "acct-gw"
    assert event["expiry"] == pytest.approx(fixed_expiry)
    assert feature._PENDING is None


async def test_login_pending_is_idempotent_then_a_new_flow_starts(fake_home, monkeypatch):
    port = _free_port()
    calls: list[str] = []
    _counting_login(monkeypatch, port, calls)
    monkeypatch.setattr(
        chatgpt_auth,
        "exchange_code",
        lambda code, **kwargs: {**_tokens(account="acct-gw"), "expires_at": time.time() + 60},
    )
    harness = _Harness()

    first = await harness.call("auth.chatgpt.login", {"no_browser": True})
    pending = feature._PENDING
    assert pending is not None
    second = await harness.call("auth.chatgpt.login", {"no_browser": True})

    assert first == second
    assert calls == ["login_url"], "a second login must not start a second flow"
    assert feature._PENDING is pending, "the pending flow must be the same one"
    await asyncio.to_thread(
        _open_callback, f"http://127.0.0.1:{port}/auth/callback?code=gw-code&state=gw-state"
    )
    await asyncio.wait_for(pending.task, timeout=5)
    assert len(harness.events()) == 1

    # A settled flow frees the slot: the next login starts fresh.
    port2 = _free_port()
    _counting_login(monkeypatch, port2, calls)
    third = await harness.call("auth.chatgpt.login", {"no_browser": True})
    assert calls == ["login_url", "login_url"]
    assert third["state"] == "gw-state"
    pending = feature._PENDING
    assert pending is not None
    pending.task.cancel()
    with suppress(asyncio.CancelledError):
        await pending.task


async def test_login_timeout_emits_failed(fake_home, monkeypatch):
    port = _free_port()
    _install_login(monkeypatch, port)
    monkeypatch.setattr(feature, "LOGIN_TIMEOUT_SECONDS", 0.1)
    harness = _Harness()

    await harness.call("auth.chatgpt.login", {"no_browser": True})
    pending = feature._PENDING
    assert pending is not None
    await asyncio.wait_for(pending.task, timeout=5)

    events = harness.events()
    assert [event["type"] for event in events] == ["auth.chatgpt.failed"]
    validate_event(events[0])
    assert events[0]["reason"] == "timeout"
    assert feature._PENDING is None


async def test_login_state_mismatch_emits_failed_without_exchanging(fake_home, monkeypatch):
    port = _free_port()
    _install_login(monkeypatch, port)

    def fake_exchange(code, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("a rejected callback must never exchange")

    monkeypatch.setattr(chatgpt_auth, "exchange_code", fake_exchange)
    harness = _Harness()

    await harness.call("auth.chatgpt.login", {"no_browser": True})
    pending = feature._PENDING
    assert pending is not None
    status, _ = await asyncio.to_thread(
        _open_callback, f"http://127.0.0.1:{port}/auth/callback?code=gw-code&state=wrong-state"
    )
    assert status == 400
    await asyncio.wait_for(pending.task, timeout=5)

    events = harness.events()
    assert events[0]["type"] == "auth.chatgpt.failed"
    assert events[0]["reason"] == "state_mismatch"


async def test_login_port_conflict_is_a_typed_rpc_error(fake_home, monkeypatch):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = int(blocker.getsockname()[1])
    _install_login(monkeypatch, port)
    harness = _Harness()
    try:
        with pytest.raises(RpcError) as error:
            await harness.call("auth.chatgpt.login", {"no_browser": True})
        assert error.value.code == OPERATION_FAILED
        assert "cannot listen" in error.value.message
        assert feature._PENDING is None
        assert harness.events() == []
    finally:
        blocker.close()


# ─── logout / import ─────────────────────────────────────────────────────────


async def test_logout_removes_only_machina_store(fake_home):
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir()
    codex_file = codex_dir / "auth.json"
    codex_file.write_text(json.dumps({"tokens": _tokens(account="acct-codex")}))
    codex_bytes = codex_file.read_bytes()
    codex_mtime = codex_file.stat().st_mtime_ns
    store = fake_home / ".machina" / "auth" / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(account="acct-machina"), store)
    harness = _Harness()

    result = await harness.call("auth.chatgpt.logout")

    assert result["removed"] is True
    assert result["status"]["signedIn"] is True
    assert result["status"]["source"] == "codex"
    assert not store.exists()
    # The Codex CLI's file is untouched (bytes and mtime).
    assert codex_file.read_bytes() == codex_bytes
    assert codex_file.stat().st_mtime_ns == codex_mtime

    again = await harness.call("auth.chatgpt.logout")
    assert again["removed"] is False
    assert again["status"]["source"] == "codex"


async def test_logout_cancels_pending_login_and_late_callback_cannot_sign_in(
    fake_home, monkeypatch
):
    """R3-2: a callback that arrives after logout must publish nothing."""
    port = _free_port()
    _install_login(monkeypatch, port)
    exchanged: list[str] = []

    def fake_exchange(code, **kwargs):  # pragma: no cover - must not run
        exchanged.append(code)
        raise AssertionError("a logout-invalidated callback must never exchange")

    monkeypatch.setattr(chatgpt_auth, "exchange_code", fake_exchange)
    harness = _Harness()

    await harness.call("auth.chatgpt.login", {"no_browser": True})
    pending = feature._PENDING
    assert pending is not None

    result = await harness.call("auth.chatgpt.logout")

    assert result["removed"] is False  # nothing was signed in
    assert feature._PENDING is None, "logout must cancel the pending flow"
    # The invalidated flow still answers the redirecting browser...
    status, _ = await asyncio.to_thread(
        _open_callback, f"http://127.0.0.1:{port}/auth/callback?code=late-code&state=gw-state"
    )
    assert status == 200
    await asyncio.wait_for(pending.task, timeout=5)
    # ...but never exchanges, and emits a failed event instead of completed.
    assert exchanged == []
    events = harness.events()
    assert [event["type"] for event in events] == ["auth.chatgpt.failed"]
    assert events[0]["reason"] == "cancelled"
    assert not (fake_home / ".machina" / "auth" / "chatgpt.json").exists()


async def test_logout_generation_fences_a_late_exchange(fake_home, monkeypatch):
    """R3-2: the security layer refuses a stale-generation publication."""
    store = fake_home / ".machina" / "auth" / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(), store)
    harness = _Harness()
    generation = chatgpt_auth.credential_generation()

    result = await harness.call("auth.chatgpt.logout")
    assert result["removed"] is True

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("a fenced exchange must not reach the network")

    with pytest.raises(chatgpt_auth.ChatGPTAuthError) as error:
        chatgpt_auth.exchange_code(
            "late-code",
            code_verifier="verifier",
            path=store,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            generation=generation,
        )
    assert error.value.code == "chatgpt_auth_cancelled"
    assert not store.exists(), "a late callback must not recreate the store"


async def test_logout_clears_in_process_catalog_state(fake_home, monkeypatch):
    """R3-2 follow-up: the signed-out account's catalog entries must not answer."""
    from machina.core.provider import catalog

    store = fake_home / ".machina" / "auth" / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(account="acct-machina"), store)
    seeded = catalog.parse_catalog(
        {"models": [{"slug": "gpt-fixture", "visibility": "list"}]}, source="live"
    )
    monkeypatch.setattr(catalog, "fetch_live_catalog", lambda *args, **kwargs: seeded)
    catalog.refresh_live_catalog("opaque-access", "acct-machina", force=True)
    assert catalog.live_models_or_none("acct-machina") is not None

    harness = _Harness()
    result = await harness.call("auth.chatgpt.logout")
    assert result["removed"] is True

    assert catalog.live_models_or_none("acct-machina") is None
    assert catalog.live_models_or_none() is None


async def test_fresh_login_after_logout_retires_the_invalidated_flow(fake_home, monkeypatch):
    """R3-2: the next login supersedes the invalidated flow and binds anew."""
    port = _free_port()
    calls: list[str] = []
    _counting_login(monkeypatch, port, calls)
    monkeypatch.setattr(chatgpt_auth, "exchange_code", lambda code, **kwargs: _tokens())
    harness = _Harness()

    await harness.call("auth.chatgpt.login", {"no_browser": True})
    first = feature._PENDING
    assert first is not None
    await harness.call("auth.chatgpt.logout")
    assert feature._PENDING is None

    port2 = _free_port()
    _counting_login(monkeypatch, port2, calls)
    second = await harness.call("auth.chatgpt.login", {"no_browser": True})
    assert calls == ["login_url", "login_url"]
    assert second["state"] == "gw-state"
    pending = feature._PENDING
    assert pending is not None
    pending.task.cancel()
    with suppress(asyncio.CancelledError):
        await pending.task


async def test_import_codex_reports_the_resulting_status(fake_home):
    harness = _Harness()

    empty = await harness.call("auth.chatgpt.importCodex")
    assert empty == {
        "imported": False,
        "status": {
            "signedIn": False,
            "source": None,
            "expired": None,
            "expiresAt": None,
            "accountId": None,
            "lastRefresh": None,
        },
    }

    codex_dir = fake_home / ".codex"
    codex_dir.mkdir()
    codex_file = codex_dir / "auth.json"
    codex_file.write_text(json.dumps({"tokens": _tokens(account="acct-cli")}))
    codex_bytes = codex_file.read_bytes()

    imported = await harness.call("auth.chatgpt.importCodex")

    assert imported["imported"] is True
    assert imported["status"]["signedIn"] is True
    assert imported["status"]["source"] == "machina"
    assert imported["status"]["accountId"] == "acct-cli"
    assert codex_file.read_bytes() == codex_bytes


async def test_oversized_callback_headers_leave_the_listener_serving(fake_home, monkeypatch):
    """F5 (review round 5): >64 KiB of headers without a terminator must be
    dropped without breaking the flow - the real callback still completes."""
    port = _free_port()
    _install_login(monkeypatch, port)
    monkeypatch.setattr(chatgpt_auth, "exchange_code", lambda code, **kwargs: _tokens())
    harness = _Harness()

    await harness.call("auth.chatgpt.login", {"no_browser": True})
    pending = feature._PENDING
    assert pending is not None

    def oversized() -> None:
        conn = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        try:
            conn.sendall(b"GET /auth/callback HTTP/1.1\r\nX-Junk: " + b"a" * 200_000)
            # Keep the socket open: the server must cross its 64 KiB stream
            # limit before EOF, which is the unhandled-exception case (F5).
            time.sleep(0.3)
        finally:
            conn.close()

    loop = asyncio.get_running_loop()
    unhandled: list[str] = []
    previous_handler = loop.get_exception_handler()

    def collect(_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        unhandled.append(str(context.get("message", "")) + repr(context.get("exception")))

    loop.set_exception_handler(collect)
    try:
        await asyncio.to_thread(oversized)
    finally:
        loop.set_exception_handler(previous_handler)
    assert not any("LimitOverrun" in item for item in unhandled), unhandled
    status, _ = await asyncio.to_thread(
        _open_callback, f"http://127.0.0.1:{port}/auth/callback?code=gw-code&state=gw-state"
    )
    assert status == 200
    await asyncio.wait_for(pending.task, timeout=5)
    events = harness.events()
    assert events and events[-1]["type"] == "auth.chatgpt.completed"
