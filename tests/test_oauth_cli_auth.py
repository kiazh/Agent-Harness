"""``machina auth <provider>`` CLI: status, import, logout scope, login flows.

No real network and no real ``~/.machina`` writes: HOME is sandboxed and the
token exchanges are monkeypatched. The loopback login tests serve the callback
with a real (local, loopback) HTTP request to the CLI's own listener.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest
from typer.testing import CliRunner

from machina.cli import auth as auth_cli
from machina.core.config import config
from machina.security import claude_auth, copilot_auth, gemini_auth
from machina.security.claude_auth import LoginRequest as ClaudeLoginRequest

runner = CliRunner()


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for env in (
        "MACHINA_CLAUDE_AUTH_FILE",
        "MACHINA_COPILOT_AUTH_FILE",
        "MACHINA_GEMINI_AUTH_FILE",
        "MACHINA_GEMINI_IMPORT_FILE",
        "CLAUDE_CONFIG_DIR",
        "MACHINA_HOME",
        "MACHINA_MACHINA_HOME",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(config, "machina_home", "", raising=False)
    monkeypatch.setattr(claude_auth, "_read_claude_code_keychain", lambda: None)
    monkeypatch.setattr(copilot_auth, "_probe_gh_token", lambda: None)
    return home


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _open_callback(url: str, *, retries: float = 5.0) -> tuple[int, str]:
    deadline = time.monotonic() + retries
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()
        except Exception as error:  # listener not up yet
            last = error
            time.sleep(0.05)
    raise AssertionError(f"callback never settled: {last}")


# ─── claude ─────────────────────────────────────────────────────────────────


def test_claude_status_signed_out_exits_1(fake_home):
    result = runner.invoke(auth_cli.app, ["claude", "status", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["signed_in"] is False
    assert payload["source"] is None
    assert not (fake_home / ".machina").exists()


def test_claude_import_status_logout_roundtrip(fake_home):
    creds = fake_home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "cc-access",
                    "refreshToken": "cc-refresh",
                    "expiresAt": int((time.time() + 1800) * 1000),
                    "subscriptionType": "max",
                }
            }
        )
    )
    before = creds.read_bytes()
    imported = runner.invoke(auth_cli.app, ["claude", "login", "--import-claude"])
    assert imported.exit_code == 0, imported.output
    assert "Signed in to Claude" in imported.output
    assert "max" in imported.output
    assert creds.read_bytes() == before

    status = runner.invoke(auth_cli.app, ["claude", "status", "--json"])
    assert status.exit_code == 0
    payload = json.loads(status.output)
    assert payload["signed_in"] is True
    assert payload["source"] == "machina"
    assert payload["subscription_type"] == "max"
    assert "cc-access" not in status.output
    assert "cc-refresh" not in status.output

    refused = runner.invoke(auth_cli.app, ["claude", "login", "--import-claude"])
    assert refused.exit_code == 1
    assert "already has its own Claude tokens" in refused.output

    logout = runner.invoke(auth_cli.app, ["claude", "logout"])
    assert logout.exit_code == 0
    assert "Signed out of Claude" in logout.output
    assert creds.exists() and creds.read_bytes() == before
    again = runner.invoke(auth_cli.app, ["claude", "logout"])
    assert again.exit_code == 0
    assert "Already signed out" in again.output


def test_claude_import_without_credentials_is_clear(fake_home):
    result = runner.invoke(auth_cli.app, ["claude", "login", "--import-claude"])
    assert result.exit_code == 1
    assert "No Claude Code credentials" in result.output
    assert not (fake_home / ".machina").exists()


def test_claude_loopback_login_exchanges_with_the_real_state(fake_home, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(auth_cli, "_free_port", lambda: port)
    seen: dict[str, str] = {}

    def fake_exchange(code, *, code_verifier, redirect_uri, state="", **kwargs):
        seen.update(code=code, code_verifier=code_verifier, redirect_uri=redirect_uri, state=state)
        return {"access_token": "at-flow", "subscription_type": "pro"}

    monkeypatch.setattr(claude_auth, "exchange_code", fake_exchange)
    captured: dict[str, str] = {}
    real_login_url = claude_auth.login_url

    def capture_login_url(**kwargs):
        request = real_login_url(**kwargs)
        captured["state"] = request.state
        captured["verifier"] = request.code_verifier
        return request

    monkeypatch.setattr(claude_auth, "login_url", capture_login_url)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runner.invoke, auth_cli.app, ["claude", "login", "--no-browser", "--timeout", "15"]
        )
        deadline = time.monotonic() + 5
        while "state" not in captured and time.monotonic() < deadline:
            time.sleep(0.02)
        status, _body = _open_callback(
            f"http://127.0.0.1:{port}/callback?code=test-code&state={captured['state']}"
        )
        result = future.result(timeout=20)

    assert status == 200
    assert result.exit_code == 0, result.output
    assert "Signed in to Claude" in result.output
    assert seen["code"] == "test-code"
    assert seen["code_verifier"] == captured["verifier"]
    assert seen["state"] == captured["state"]
    assert seen["redirect_uri"] == f"http://localhost:{port}/callback"
    assert "test-code" not in result.output


def test_claude_manual_login_parses_code_state(fake_home, monkeypatch):
    monkeypatch.setattr(
        claude_auth,
        "login_url",
        lambda **kwargs: ClaudeLoginRequest(
            url="https://claude.example/authorize",
            state="state-manual",
            code_verifier="verifier-manual",
            redirect_uri=claude_auth.MANUAL_REDIRECT_URI,
        ),
    )
    seen: dict[str, str] = {}

    def fake_exchange(code, *, code_verifier, redirect_uri, state="", **kwargs):
        seen.update(code=code, code_verifier=code_verifier, redirect_uri=redirect_uri, state=state)
        return {"access_token": "at-manual"}

    monkeypatch.setattr(claude_auth, "exchange_code", fake_exchange)
    result = runner.invoke(
        auth_cli.app, ["claude", "login", "--manual"], input="code-manual#state-manual\n"
    )
    assert result.exit_code == 0, result.output
    assert seen == {
        "code": "code-manual",
        "code_verifier": "verifier-manual",
        "redirect_uri": claude_auth.MANUAL_REDIRECT_URI,
        "state": "state-manual",
    }


# ─── gemini ─────────────────────────────────────────────────────────────────


def test_gemini_status_signed_out_exits_1(fake_home):
    result = runner.invoke(auth_cli.app, ["gemini", "status", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["signed_in"] is False
    assert payload["project"] is None


def test_gemini_import_status_logout_roundtrip(fake_home):
    creds = fake_home / ".gemini" / "oauth_creds.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(
        json.dumps(
            {
                "access_token": "cli-access",
                "refresh_token": "cli-refresh",
                "scope": gemini_auth.SCOPES,
                "expiry_date": int((time.time() + 1800) * 1000),
            }
        )
    )
    before = creds.read_bytes()
    imported = runner.invoke(auth_cli.app, ["gemini", "login", "--import-gemini"])
    assert imported.exit_code == 0, imported.output
    assert "Signed in to Gemini" in imported.output
    assert creds.read_bytes() == before

    status = runner.invoke(auth_cli.app, ["gemini", "status", "--json"])
    assert status.exit_code == 0
    payload = json.loads(status.output)
    assert payload["signed_in"] is True
    assert payload["source"] == "machina"
    assert "cli-access" not in status.output

    refused = runner.invoke(auth_cli.app, ["gemini", "login", "--import-gemini"])
    assert refused.exit_code == 1
    assert "already has its own Gemini tokens" in refused.output

    logout = runner.invoke(auth_cli.app, ["gemini", "logout"])
    assert logout.exit_code == 0
    assert "Signed out of Gemini" in logout.output
    assert creds.exists() and creds.read_bytes() == before


def test_gemini_import_without_credentials_is_clear(fake_home):
    result = runner.invoke(auth_cli.app, ["gemini", "login", "--import-gemini"])
    assert result.exit_code == 1
    assert "No gemini-cli credentials" in result.output


def test_gemini_loopback_login_settles_and_exchanges(fake_home, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(auth_cli, "_free_port", lambda: port)
    seen: dict[str, str] = {}
    captured: dict[str, str] = {}
    real_login_url = gemini_auth.login_url

    def capture_login_url(**kwargs):
        request = real_login_url(**kwargs)
        captured["state"] = request.state
        captured["verifier"] = request.code_verifier
        return request

    def fake_exchange(code, *, code_verifier, redirect_uri, **kwargs):
        seen.update(code=code, code_verifier=code_verifier, redirect_uri=redirect_uri)
        return {"access_token": "at-flow", "email": "flow@example.com"}

    monkeypatch.setattr(gemini_auth, "login_url", capture_login_url)
    monkeypatch.setattr(gemini_auth, "exchange_code", fake_exchange)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runner.invoke, auth_cli.app, ["gemini", "login", "--no-browser", "--timeout", "15"]
        )
        deadline = time.monotonic() + 5
        while "state" not in captured and time.monotonic() < deadline:
            time.sleep(0.02)
        status, _body = _open_callback(
            f"http://127.0.0.1:{port}/oauth2callback?code=g-code&state={captured['state']}"
        )
        result = future.result(timeout=20)

    assert status == 200
    assert result.exit_code == 0, result.output
    assert "Signed in to Gemini" in result.output
    assert seen == {
        "code": "g-code",
        "code_verifier": captured["verifier"],
        "redirect_uri": f"http://127.0.0.1:{port}/oauth2callback",
    }


# ─── copilot ────────────────────────────────────────────────────────────────


def test_copilot_status_signed_out_exits_1(fake_home):
    result = runner.invoke(auth_cli.app, ["copilot", "status", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["signed_in"] is False
    assert payload["token_family"] is None


def test_copilot_import_status_logout_roundtrip(fake_home, monkeypatch):
    monkeypatch.setattr(copilot_auth, "_probe_gh_token", lambda: "gho_from_gh")
    monkeypatch.setattr(copilot_auth, "fetch_account", lambda token, **kwargs: "octocat")
    imported = runner.invoke(auth_cli.app, ["copilot", "login", "--import-gh"])
    assert imported.exit_code == 0, imported.output
    assert "Signed in to GitHub Copilot" in imported.output
    assert "octocat" in imported.output

    status = runner.invoke(auth_cli.app, ["copilot", "status", "--json"])
    assert status.exit_code == 0
    payload = json.loads(status.output)
    assert payload["signed_in"] is True
    assert payload["source"] == "machina"
    assert payload["token_family"] == "gho"
    assert "gho_from_gh" not in status.output

    refused = runner.invoke(auth_cli.app, ["copilot", "login", "--import-gh"])
    assert refused.exit_code == 1
    assert "already has its own Copilot token" in refused.output

    logout = runner.invoke(auth_cli.app, ["copilot", "logout"])
    assert logout.exit_code == 0
    assert "Signed out of GitHub Copilot" in logout.output
    # gh's credential is untouched: the next status still reports it.
    status = runner.invoke(auth_cli.app, ["copilot", "status", "--json"])
    assert json.loads(status.output)["source"] == "gh"


def test_copilot_import_without_gh_is_clear(fake_home):
    result = runner.invoke(auth_cli.app, ["copilot", "login", "--import-gh"])
    assert result.exit_code == 1
    assert "No gh CLI credential" in result.output


def test_copilot_device_login_flow(fake_home, monkeypatch):
    monkeypatch.setattr(
        copilot_auth,
        "device_flow_start",
        lambda **kwargs: {
            "device_code": "dev-1",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device",
            "interval": 5,
        },
    )
    monkeypatch.setattr(
        copilot_auth, "device_flow_poll", lambda device_code, **kwargs: "gho_device"
    )
    monkeypatch.setattr(copilot_auth, "fetch_account", lambda token, **kwargs: "octocat")
    result = runner.invoke(auth_cli.app, ["copilot", "login", "--timeout", "5"])
    assert result.exit_code == 0, result.output
    assert "ABCD-EFGH" in result.output
    assert "Signed in to GitHub Copilot" in result.output
    assert copilot_auth.load_tokens()["token"] == "gho_device"


def test_copilot_device_login_timeout_is_clean(fake_home, monkeypatch):
    monkeypatch.setattr(
        copilot_auth,
        "device_flow_start",
        lambda **kwargs: {"device_code": "dev-1", "user_code": "X", "verification_uri": "u"},
    )

    def boom(device_code, **kwargs):
        raise copilot_auth.CopilotAuthError(
            "GitHub device authorization timed out", code="copilot_device_timeout"
        )

    monkeypatch.setattr(copilot_auth, "device_flow_poll", boom)
    result = runner.invoke(auth_cli.app, ["copilot", "login", "--timeout", "1"])
    assert result.exit_code == 1
    assert "timed out" in result.output
    assert not (fake_home / ".machina").exists()


# ─── runtime readiness branches ─────────────────────────────────────────────


def test_runtime_readiness_reports_each_subscription_provider(fake_home, monkeypatch):
    from machina.core import runtime

    # Signed out: every branch reports unavailable with its own login hint.
    for provider, hint in (
        ("claude", "machina auth claude login"),
        ("copilot", "machina auth copilot login"),
        ("gemini", "machina auth gemini login"),
    ):
        monkeypatch.setattr(config, "provider", provider, raising=False)
        report = runtime._detect_providers()
        assert report["chat_provider_selected"] == provider
        assert report["chat_provider"] == "unavailable", report
        assert hint in report["chat_provider_reason"], report

    # Signed in (Machina store): ready with no reason.
    claude_auth.save_tokens(
        {"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600}
    )
    copilot_auth.save_tokens({"token": "gho_store"})
    gemini_auth.save_tokens({"access_token": "at", "refresh_token": "rt"})
    for provider in ("claude", "copilot", "gemini"):
        monkeypatch.setattr(config, "provider", provider, raising=False)
        report = runtime._detect_providers()
        assert report["chat_provider"] == "ready", report
        assert report["chat_provider_reason"] == "", report


def test_runtime_readiness_notes_reference_cli_sources(fake_home, monkeypatch):
    from machina.core import runtime

    # Claude Code / gemini-cli file imports show up as "via ... login" notes.
    creds = fake_home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "cc-at",
                    "refreshToken": "cc-rt",
                    "expiresAt": int((time.time() + 1800) * 1000),
                }
            }
        )
    )
    monkeypatch.setattr(config, "provider", "claude", raising=False)
    report = runtime._detect_providers()
    assert report["chat_provider"] == "ready"
    assert "via Claude Code login" in report["chat_provider_reason"]

    gem = fake_home / ".gemini" / "oauth_creds.json"
    gem.parent.mkdir(parents=True)
    gem.write_text(json.dumps({"access_token": "cli-at", "refresh_token": "cli-rt"}))
    monkeypatch.setattr(config, "provider", "gemini", raising=False)
    report = runtime._detect_providers()
    assert report["chat_provider"] == "ready"
    assert "via gemini-cli login" in report["chat_provider_reason"]

    monkeypatch.setattr(copilot_auth, "_probe_gh_token", lambda: "gho_gh")
    monkeypatch.setattr(config, "provider", "copilot", raising=False)
    report = runtime._detect_providers()
    assert report["chat_provider"] == "ready"
    assert "via gh CLI login" in report["chat_provider_reason"]
