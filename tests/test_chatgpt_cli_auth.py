"""``machina auth`` CLI: status, Codex import, logout scope, and PKCE login.

No real network and no real ``~/.machina`` writes: HOME/CODEX_HOME are
sandboxed and the token exchange is monkeypatched. The login tests serve the
callback with a real (local, loopback) HTTP request to the CLI's own listener.
"""

from __future__ import annotations

import base64
import json
import socket
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

from machina.cli import auth as auth_cli
from machina.core.config import config
from machina.security import chatgpt_auth
from machina.security.chatgpt_auth import LoginRequest

runner = CliRunner()

_STATUS_KEYS = {"signed_in", "source", "expired", "expires_at", "account_id", "last_refresh"}


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


def _write_codex(home: Path, tokens: dict) -> Path:
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    codex_file = codex_dir / "auth.json"
    codex_file.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": tokens}))
    return codex_file


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _open_callback(url: str, *, retries: float = 5.0) -> tuple[int, str]:
    """GET *url* against the CLI's listener, retrying while it binds."""
    deadline = time.monotonic() + retries
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:  # loopback only
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()
        except OSError as error:  # not listening yet
            last = error
            time.sleep(0.02)
    raise AssertionError(f"callback listener never answered {url}: {last}")


# ─── status ──────────────────────────────────────────────────────────────────


def test_status_json_signed_out_exits_1(fake_home):
    result = runner.invoke(auth_cli.app, ["status", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert set(payload) == _STATUS_KEYS
    assert payload["signed_in"] is False
    assert payload["source"] is None
    assert payload["account_id"] is None
    # A read-only peek never creates a store.
    assert not (fake_home / ".machina").exists()


def test_status_json_reports_codex_peek_without_tokens(fake_home):
    tokens = _tokens(account="acct-codex")
    codex_file = _write_codex(fake_home, tokens)
    codex_bytes = codex_file.read_bytes()
    codex_mtime = codex_file.stat().st_mtime_ns

    result = runner.invoke(auth_cli.app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["signed_in"] is True
    assert payload["source"] == "codex"
    assert payload["account_id"] == "acct-codex"
    assert payload["expired"] is False
    # Peeking is read-only and never echoes token material.
    assert not (fake_home / ".machina").exists()
    assert tokens["access_token"] not in result.output
    assert tokens["refresh_token"] not in result.output
    assert codex_file.read_bytes() == codex_bytes
    assert codex_file.stat().st_mtime_ns == codex_mtime


def test_status_human_card_covers_both_states(fake_home):
    signed_out = runner.invoke(auth_cli.app, ["status"])
    assert signed_out.exit_code == 1
    assert "not signed in" in signed_out.output

    _write_codex(fake_home, _tokens(account="acct-codex"))
    signed_in = runner.invoke(auth_cli.app, ["status"])
    assert signed_in.exit_code == 0, signed_in.output
    assert "Codex CLI" in signed_in.output
    assert "acct-codex" in signed_in.output


# ─── logout ─────────────────────────────────────────────────────────────────


def test_logout_removes_only_machina_store_and_never_codex(fake_home):
    codex_file = _write_codex(fake_home, _tokens(account="acct-codex"))
    codex_bytes = codex_file.read_bytes()
    codex_mtime = codex_file.stat().st_mtime_ns
    store = fake_home / ".machina" / "auth" / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(account="acct-machina"), store)

    result = runner.invoke(auth_cli.app, ["logout"])

    assert result.exit_code == 0, result.output
    assert "Signed out" in result.output
    assert not store.exists()
    # The Codex CLI's file is untouched (bytes and mtime).
    assert codex_file.read_bytes() == codex_bytes
    assert codex_file.stat().st_mtime_ns == codex_mtime
    # The remaining Codex credentials are surfaced, not hidden.
    assert "left untouched" in result.output

    again = runner.invoke(auth_cli.app, ["logout"])
    assert again.exit_code == 0, again.output
    assert "Already signed out" in again.output
    assert codex_file.exists()


def test_logout_bumps_the_authentication_generation(fake_home):
    """R3-2: CLI logout fences in-flight login/refresh publication."""
    store = fake_home / ".machina" / "auth" / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(), store)
    before = chatgpt_auth.credential_generation(store)

    result = runner.invoke(auth_cli.app, ["logout"])

    assert result.exit_code == 0, result.output
    assert not store.exists()
    assert chatgpt_auth.credential_generation(store) == before + 1
    generation_file = store.with_name(store.name + ".generation")
    assert int(generation_file.read_text().strip()) == before + 1


def test_logout_honors_auth_file_override(fake_home, monkeypatch, tmp_path):
    override = tmp_path / "custom-store.json"
    chatgpt_auth.save_tokens(_tokens(), override)
    default_store = fake_home / ".machina" / "auth" / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(), default_store)
    monkeypatch.setenv("MACHINA_CHATGPT_AUTH_FILE", str(override))

    result = runner.invoke(auth_cli.app, ["logout"])

    assert result.exit_code == 0, result.output
    assert not override.exists()
    assert default_store.exists(), "logout must only remove the configured store"


# ─── import-codex ───────────────────────────────────────────────────────────


def test_login_import_codex_copies_and_leaves_source_readonly(fake_home):
    tokens = _tokens(account="acct-codex")
    codex_file = _write_codex(fake_home, tokens)
    codex_bytes = codex_file.read_bytes()
    codex_mtime = codex_file.stat().st_mtime_ns

    result = runner.invoke(auth_cli.app, ["login", "--import-codex"])

    assert result.exit_code == 0, result.output
    assert "acct-codex" in result.output
    store = fake_home / ".machina" / "auth" / "chatgpt.json"
    assert chatgpt_auth.load_tokens(store)["account_id"] == "acct-codex"
    assert codex_file.read_bytes() == codex_bytes
    assert codex_file.stat().st_mtime_ns == codex_mtime


def test_login_import_codex_without_source_is_a_clean_error(fake_home):
    result = runner.invoke(auth_cli.app, ["login", "--import-codex"])

    assert result.exit_code == 1, result.output
    assert "No Codex CLI credentials" in result.output
    assert not (fake_home / ".machina").exists()


def test_login_import_codex_refuses_to_clobber_machina_store(fake_home):
    store = fake_home / ".machina" / "auth" / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(account="acct-machina", refresh="rt-machina"), store)
    _write_codex(fake_home, _tokens(account="acct-codex", refresh="rt-codex"))

    result = runner.invoke(auth_cli.app, ["login", "--import-codex"])

    assert result.exit_code == 1, result.output
    assert "already has its own ChatGPT tokens" in result.output
    assert chatgpt_auth.load_tokens(store)["refresh_token"] == "rt-machina"


# ─── browser login flow ─────────────────────────────────────────────────────


def _install_login(monkeypatch, port: int, *, state: str = "cli-state") -> None:
    monkeypatch.setattr(
        chatgpt_auth,
        "login_url",
        lambda: LoginRequest(
            url=f"https://auth.example/authorize?state={state}",
            state=state,
            code_verifier="cli-verifier",
            redirect_uri=f"http://127.0.0.1:{port}/auth/callback",
        ),
    )


def test_login_settles_from_a_real_local_callback(fake_home, monkeypatch):
    port = _free_port()
    _install_login(monkeypatch, port)
    seen: dict[str, object] = {}

    def fake_exchange(code, *, code_verifier, redirect_uri, **kwargs):
        seen.update(
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            # R3-2: the flow's captured generation must reach the exchange.
            generation=kwargs.get("generation"),
        )
        return {**_tokens(account="acct-flow"), "account_id": "acct-flow"}

    monkeypatch.setattr(chatgpt_auth, "exchange_code", fake_exchange)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runner.invoke, auth_cli.app, ["login", "--no-browser", "--timeout", "15"]
        )
        status, _ = _open_callback(
            f"http://127.0.0.1:{port}/auth/callback?code=test-code&state=cli-state"
        )
        result = future.result(timeout=20)

    assert status == 200
    assert result.exit_code == 0, result.output
    assert seen == {
        "code": "test-code",
        "code_verifier": "cli-verifier",
        "redirect_uri": f"http://127.0.0.1:{port}/auth/callback",
        "generation": 0,
    }
    assert "https://auth.example/authorize?state=cli-state" in result.output
    assert "acct-flow" in result.output
    # The one-time authorization code is never echoed.
    assert "test-code" not in result.output


def test_login_rejects_state_mismatch_without_exchanging(fake_home, monkeypatch):
    port = _free_port()
    _install_login(monkeypatch, port)
    exchanged: list[str] = []

    def fake_exchange(code, **kwargs):  # pragma: no cover - must not run
        exchanged.append(code)
        raise AssertionError("a rejected callback must never exchange")

    monkeypatch.setattr(chatgpt_auth, "exchange_code", fake_exchange)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runner.invoke, auth_cli.app, ["login", "--no-browser", "--timeout", "15"]
        )
        status, _ = _open_callback(
            f"http://127.0.0.1:{port}/auth/callback?code=test-code&state=wrong-state"
        )
        result = future.result(timeout=20)

    assert status == 400
    assert result.exit_code == 1, result.output
    assert "state did not match" in result.output
    assert exchanged == []
    assert not (fake_home / ".machina").exists()


def test_login_timeout_is_a_clean_error_and_releases_the_port(fake_home, monkeypatch):
    port = _free_port()
    _install_login(monkeypatch, port)

    result = runner.invoke(auth_cli.app, ["login", "--no-browser", "--timeout", "0.3"])

    assert result.exit_code == 1, result.output
    assert "timed out after" in result.output
    assert not (fake_home / ".machina").exists()
    # The listener was stopped: the port is bindable again.
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))


def test_login_negative_timeout_is_a_usage_error(fake_home):
    result = runner.invoke(auth_cli.app, ["login", "--timeout", "0"])

    assert result.exit_code == 2, result.output


def test_login_port_conflict_is_a_clean_error(fake_home, monkeypatch):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = int(blocker.getsockname()[1])
    _install_login(monkeypatch, port)
    try:
        result = runner.invoke(auth_cli.app, ["login", "--no-browser", "--timeout", "5"])
    finally:
        blocker.close()

    assert result.exit_code == 1, result.output
    assert "Could not listen" in result.output
    assert not (fake_home / ".machina").exists()
