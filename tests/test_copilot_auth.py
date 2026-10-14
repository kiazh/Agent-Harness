"""Copilot auth: token store, gh import, device flow, validation.

``gh`` is always monkeypatched; HTTP is served by ``httpx.MockTransport``.
No real network, no real ``~/.machina`` state.
"""

from __future__ import annotations

import json
import stat
import time
from urllib.parse import parse_qs

import httpx
import pytest

from machina.core.config import config
from machina.security import copilot_auth
from machina.security.copilot_auth import CopilotAuthError


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Fake HOME with no Machina store; ``gh`` probing is disabled by default."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MACHINA_COPILOT_AUTH_FILE", raising=False)
    monkeypatch.delenv("MACHINA_HOME", raising=False)
    monkeypatch.delenv("MACHINA_MACHINA_HOME", raising=False)
    monkeypatch.setattr(config, "machina_home", "", raising=False)
    monkeypatch.setattr(copilot_auth, "_probe_gh_token", lambda: None)
    return home


def test_save_load_roundtrip_and_permissions(fake_home, tmp_path):
    store = tmp_path / "copilot.json"
    saved = copilot_auth.save_tokens({"token": "gho_example", "account": "octocat"}, store)
    assert saved == store
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.parent.stat().st_mode) == 0o700
    loaded = copilot_auth.load_tokens(store)
    assert loaded is not None
    assert loaded["token"] == "gho_example"
    assert loaded["account"] == "octocat"
    assert loaded["token_family"] == "gho"
    assert loaded["last_refresh"]
    raw = json.loads(store.read_text())
    assert set(raw) == set(copilot_auth._STORE_FIELDS)


def test_validate_token_families():
    assert copilot_auth.validate_token("gho_x") == (True, "ok")
    assert copilot_auth.validate_token("ghu_x") == (True, "ok")
    assert copilot_auth.validate_token("github_pat_x") == (True, "ok")
    ok, message = copilot_auth.validate_token("ghp_classic")
    assert ok is False and "classic" in message
    ok, message = copilot_auth.validate_token("plain")
    assert ok is False and "gho_" in message
    ok, message = copilot_auth.validate_token("")
    assert ok is False


def test_import_from_gh_cli_stores_and_validates(fake_home, monkeypatch):
    monkeypatch.setattr(copilot_auth, "_probe_gh_token", lambda: "gho_test")
    tokens = copilot_auth.import_from_gh_cli()
    assert tokens is not None
    assert tokens["token"] == "gho_test"
    assert tokens["account"] is None  # fetch_login defaults off (no network in import)


def test_import_from_gh_cli_rejects_classic_pat(fake_home, monkeypatch):
    monkeypatch.setattr(copilot_auth, "_probe_gh_token", lambda: "ghp_classic")
    with pytest.raises(CopilotAuthError) as excinfo:
        copilot_auth.import_from_gh_cli()
    assert excinfo.value.code == "copilot_token_unsupported"
    assert not copilot_auth.auth_path().exists()


def test_import_refused_when_store_exists_unless_forced(fake_home, monkeypatch, tmp_path):
    store = tmp_path / "copilot.json"
    copilot_auth.save_tokens({"token": "gho_machina"}, store)
    monkeypatch.setattr(copilot_auth, "_probe_gh_token", lambda: "gho_gh")
    assert copilot_auth.import_from_gh_cli(path=store) is None
    forced = copilot_auth.import_from_gh_cli(path=store, force=True)
    assert forced is not None
    assert forced["token"] == "gho_gh"


def test_import_fetch_login_best_effort(fake_home, monkeypatch):
    monkeypatch.setattr(copilot_auth, "_probe_gh_token", lambda: "gho_test")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"login": "octocat"})
        )
    )
    tokens = copilot_auth.import_from_gh_cli(fetch_login=True, client=client)
    assert tokens is not None
    assert tokens["account"] == "octocat"


def test_status_probe_and_store_sources(fake_home, monkeypatch, tmp_path):
    store = tmp_path / "copilot.json"
    report = copilot_auth.status(path=store)
    assert report["signed_in"] is False
    monkeypatch.setattr(copilot_auth, "_probe_gh_token", lambda: "gho_gh")
    report = copilot_auth.status(path=store)
    assert report["signed_in"] is True
    assert report["source"] == "gh"
    assert "gho_gh" not in json.dumps(report)
    copilot_auth.save_tokens({"token": "gho_machina"}, store)
    report = copilot_auth.status(path=store)
    assert report["source"] == "machina"


def test_device_flow_start_parses_response(fake_home):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "device_code": "dev-1",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "interval": 5,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    payload = copilot_auth.device_flow_start(client=client)
    assert payload["device_code"] == "dev-1"
    assert payload["user_code"] == "ABCD-EFGH"
    body = parse_qs(seen[0].content.decode())
    assert body["client_id"] == [copilot_auth.COPILOT_CLIENT_ID]
    assert body["scope"] == ["read:user"]


def test_device_flow_start_error_is_typed(fake_home):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="nope"))
    )
    with pytest.raises(CopilotAuthError) as excinfo:
        copilot_auth.device_flow_start(client=client)
    assert excinfo.value.code == "copilot_device_failed"


def test_device_flow_poll_success_and_pending(fake_home):
    responses = [
        httpx.Response(200, json={"error": "authorization_pending"}),
        httpx.Response(200, json={"error": "slow_down", "interval": 9}),
        httpx.Response(200, json={"access_token": "gho_device"}),
    ]
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    token = copilot_auth.device_flow_poll(
        "dev-1", interval=1, timeout=30, client=client, sleep=sleeps.append
    )
    assert token == "gho_device"
    assert len(sleeps) == 3
    assert sleeps[-1] == 9.0  # server-supplied slow_down interval honoured


def test_device_flow_poll_expired_is_typed(fake_home):
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"error": "expired_token"})
        )
    )
    with pytest.raises(CopilotAuthError) as excinfo:
        copilot_auth.device_flow_poll(
            "dev-1", interval=0.01, timeout=5, client=client, sleep=lambda s: None
        )
    assert excinfo.value.code == "copilot_device_expired_token"


def test_device_flow_poll_timeout_is_typed(fake_home):
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"error": "authorization_pending"})
        )
    )
    with pytest.raises(CopilotAuthError) as excinfo:
        copilot_auth.device_flow_poll(
            "dev-1", interval=0.01, timeout=0.05, client=client, sleep=lambda s: time.sleep(0.03)
        )
    assert excinfo.value.code == "copilot_device_timeout"


def test_resolve_tokens_missing_is_typed_with_hint(fake_home, tmp_path):
    with pytest.raises(CopilotAuthError) as excinfo:
        copilot_auth.resolve_tokens(path=tmp_path / "absent.json")
    assert excinfo.value.code == "copilot_auth_missing"
    assert "machina auth copilot login" in str(excinfo.value)


def test_resolve_tokens_imports_from_gh(fake_home, monkeypatch, tmp_path):
    monkeypatch.setattr(copilot_auth, "_probe_gh_token", lambda: "gho_gh")
    store = tmp_path / "copilot.json"
    tokens = copilot_auth.resolve_tokens(path=store)
    assert tokens["token"] == "gho_gh"
    assert copilot_auth.load_tokens(store) is not None


def test_resolve_tokens_rejects_stored_classic(fake_home, tmp_path):
    store = tmp_path / "copilot.json"
    copilot_auth.save_tokens({"token": "ghp_classic"}, store)
    with pytest.raises(CopilotAuthError) as excinfo:
        copilot_auth.resolve_tokens(path=store)
    assert excinfo.value.code == "copilot_token_unsupported"
