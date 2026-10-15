"""``machina auth <provider>`` CLI: status, import, logout scope, login flows.

No real network and no real ``~/.machina`` writes: HOME is sandboxed and the
token exchanges are monkeypatched. The GitHub device flow is driven through
monkeypatched start/poll functions (no browser listener involved).

The claude (Claude Pro/Max) and gemini (Gemini Code Assist) subscription flows
were removed 2026-10-14 (vendor terms disallow third-party subscription-token
reuse); ``tests/test_subscription_removals.py`` guards their absence.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from machina.cli import auth as auth_cli
from machina.core.config import config
from machina.security import copilot_auth

runner = CliRunner()


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for env in (
        "MACHINA_COPILOT_AUTH_FILE",
        "MACHINA_HOME",
        "MACHINA_MACHINA_HOME",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(config, "machina_home", "", raising=False)
    monkeypatch.setattr(copilot_auth, "_probe_gh_token", lambda: None)
    return home


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


def test_copilot_device_login_cancelled_by_logout_writes_nothing(fake_home, monkeypatch):
    """R4-015: a device authorization completing after logout writes nothing."""
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

    def poll_after_logout(device_code, **kwargs):
        auth_cli.copilot_logout()  # the logout lands while the user authorizes
        return "gho_late"

    monkeypatch.setattr(copilot_auth, "device_flow_poll", poll_after_logout)
    monkeypatch.setattr(copilot_auth, "fetch_account", lambda token, **kwargs: "octocat")

    result = runner.invoke(auth_cli.app, ["copilot", "login", "--timeout", "5"])

    assert result.exit_code == 1
    assert "cancelled" in result.output
    assert copilot_auth.load_tokens() is None
    assert not copilot_auth.auth_path().exists()


# ─── runtime readiness branches ─────────────────────────────────────────────


def test_runtime_readiness_reports_copilot(fake_home, monkeypatch):
    from machina.core import runtime

    # Signed out: the branch reports unavailable with its own login hint.
    monkeypatch.setattr(config, "provider", "copilot", raising=False)
    report = runtime._detect_providers()
    assert report["chat_provider_selected"] == "copilot"
    assert report["chat_provider"] == "unavailable", report
    assert "machina auth copilot login" in report["chat_provider_reason"], report

    # Signed in (Machina store): ready with no reason.
    copilot_auth.save_tokens({"token": "gho_store"})
    report = runtime._detect_providers()
    assert report["chat_provider"] == "ready", report
    assert report["chat_provider_reason"] == "", report


def test_runtime_readiness_notes_reference_cli_sources(fake_home, monkeypatch):
    from machina.core import runtime

    # The gh CLI credential shows up as a "via gh CLI login" note.
    monkeypatch.setattr(copilot_auth, "_probe_gh_token", lambda: "gho_gh")
    monkeypatch.setattr(config, "provider", "copilot", raising=False)
    report = runtime._detect_providers()
    assert report["chat_provider"] == "ready"
    assert "via gh CLI login" in report["chat_provider_reason"]
