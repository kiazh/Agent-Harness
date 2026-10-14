"""Gemini (gemini-cli) auth: token store, CLI import, refresh, PKCE login.

All HTTP is served by ``httpx.MockTransport``; no real network, no real
``~/.gemini`` or ``~/.machina`` filesystem state leaks into the tests.
"""

from __future__ import annotations

import base64
import json
import stat
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from machina.core.config import config
from machina.security import gemini_auth
from machina.security.gemini_auth import (
    GEMINI_CLIENT_ID,
    GEMINI_CLIENT_SECRET,
    GeminiAuthError,
)


def _id_token(*, email: str = "user@example.com", exp_in: float = 3600.0) -> str:
    def b64(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    payload = {"email": email, "exp": time.time() + exp_in, "aud": GEMINI_CLIENT_ID}
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(payload)}.sig"


def _tokens(*, access: str = "at-old", refresh: str = "rt-old", exp_in: float = 3600.0) -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": _id_token(),
        "scope": gemini_auth.SCOPES,
        "expires_at": time.time() + exp_in,
    }


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Fake HOME with no Machina store and no gemini-cli credentials."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MACHINA_GEMINI_AUTH_FILE", raising=False)
    monkeypatch.delenv("MACHINA_GEMINI_IMPORT_FILE", raising=False)
    monkeypatch.delenv("MACHINA_HOME", raising=False)
    monkeypatch.delenv("MACHINA_MACHINA_HOME", raising=False)
    monkeypatch.setattr(config, "machina_home", "", raising=False)
    return home


def test_save_load_roundtrip_and_permissions(fake_home, tmp_path):
    store = tmp_path / "gemini.json"
    saved = gemini_auth.save_tokens(_tokens(), store)
    assert saved == store
    mode = stat.S_IMODE(store.stat().st_mode)
    assert mode == 0o600
    parent_mode = stat.S_IMODE(store.parent.stat().st_mode)
    assert parent_mode == 0o700
    loaded = gemini_auth.load_tokens(store)
    assert loaded is not None
    assert loaded["access_token"] == "at-old"
    assert loaded["refresh_token"] == "rt-old"
    assert loaded["email"] == "user@example.com"
    assert loaded["last_refresh"]
    # No token material in the file beyond the intended fields.
    raw = json.loads(store.read_text())
    assert set(raw) == set(gemini_auth._STORE_FIELDS)


def test_import_from_gemini_cli_is_read_only_and_normalizes_ms(fake_home, tmp_path):
    cli_file = fake_home / ".gemini" / "oauth_creds.json"
    cli_file.parent.mkdir(parents=True)
    expiry_ms = int((time.time() + 1800) * 1000)
    cli_file.write_text(
        json.dumps(
            {
                "access_token": "cli-access",
                "refresh_token": "cli-refresh",
                "id_token": _id_token(email="cli@example.com"),
                "scope": gemini_auth.SCOPES,
                "token_type": "Bearer",
                "expiry_date": expiry_ms,
            }
        )
    )
    before = cli_file.read_bytes()
    before_mtime = cli_file.stat().st_mtime_ns
    tokens = gemini_auth.import_from_gemini_cli()
    assert tokens is not None
    assert tokens["access_token"] == "cli-access"
    assert tokens["refresh_token"] == "cli-refresh"
    assert tokens["email"] == "cli@example.com"
    assert tokens["expires_at"] == pytest.approx(expiry_ms / 1000.0, abs=0.001)
    assert cli_file.read_bytes() == before
    assert cli_file.stat().st_mtime_ns == before_mtime


def test_import_refused_when_store_exists_unless_forced(fake_home, tmp_path):
    cli_file = fake_home / ".gemini" / "oauth_creds.json"
    cli_file.parent.mkdir(parents=True)
    cli_file.write_text(json.dumps({"access_token": "cli-access", "refresh_token": "cli-rt"}))
    store = tmp_path / "gemini.json"
    gemini_auth.save_tokens(_tokens(access="machina-access", refresh="machina-rt"), store)
    assert gemini_auth.import_from_gemini_cli(path=store) is None
    forced = gemini_auth.import_from_gemini_cli(path=store, force=True)
    assert forced is not None
    assert forced["access_token"] == "cli-access"


def test_status_reports_sources_without_tokens(fake_home, tmp_path):
    store = tmp_path / "gemini.json"
    report = gemini_auth.status(path=store)
    assert report == {
        "signed_in": False,
        "source": None,
        "expired": None,
        "expires_at": None,
        "email": None,
        "project": None,
        "last_refresh": None,
    }
    cli_file = fake_home / ".gemini" / "oauth_creds.json"
    cli_file.parent.mkdir(parents=True)
    cli_file.write_text(json.dumps({"access_token": "cli-access"}))
    report = gemini_auth.status(path=store)
    assert report["signed_in"] is True
    assert report["source"] == "gemini-cli"
    assert "cli-access" not in json.dumps(report)
    gemini_auth.save_tokens(_tokens(), store)
    report = gemini_auth.status(path=store)
    assert report["source"] == "machina"
    assert report["expired"] is False


def test_refresh_posts_expected_form_and_persists(fake_home, tmp_path):
    store = tmp_path / "gemini.json"
    gemini_auth.save_tokens(_tokens(), store)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "at-new",
                "expires_in": 3599,
                "scope": gemini_auth.SCOPES,
                "token_type": "Bearer",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tokens = gemini_auth.refresh(path=store, client=client)
    assert tokens["access_token"] == "at-new"
    assert tokens["refresh_token"] == "rt-old"  # unchanged when not rotated
    assert tokens["expires_at"] > time.time() + 3000
    assert len(seen) == 1
    body = parse_qs(seen[0].content.decode())
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == ["rt-old"]
    assert body["client_id"] == [GEMINI_CLIENT_ID]
    assert body["client_secret"] == [GEMINI_CLIENT_SECRET]
    assert str(seen[0].url) == gemini_auth.TOKEN_URL
    assert gemini_auth.load_tokens(store)["access_token"] == "at-new"


def test_refresh_adopts_server_rotation(fake_home, tmp_path):
    store = tmp_path / "gemini.json"
    gemini_auth.save_tokens(_tokens(), store)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 60}
            )
        )
    )
    tokens = gemini_auth.refresh(path=store, client=client)
    assert tokens["refresh_token"] == "rt-new"


def test_refresh_invalid_grant_is_typed_with_hint(fake_home, tmp_path):
    store = tmp_path / "gemini.json"
    gemini_auth.save_tokens(_tokens(), store)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": "invalid_grant"})
        )
    )
    with pytest.raises(GeminiAuthError) as excinfo:
        gemini_auth.refresh(path=store, client=client)
    assert excinfo.value.code == "invalid_grant"
    assert "gemini" in str(excinfo.value)


def test_refresh_unreachable_is_typed(fake_home, tmp_path):
    store = tmp_path / "gemini.json"
    gemini_auth.save_tokens(_tokens(), store)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GeminiAuthError) as excinfo:
        gemini_auth.refresh(path=store, client=client)
    assert excinfo.value.code == "gemini_refresh_unreachable"


def test_refresh_missing_tokens_is_typed(fake_home, tmp_path):
    with pytest.raises(GeminiAuthError) as excinfo:
        gemini_auth.refresh(path=tmp_path / "absent.json")
    assert excinfo.value.code == "gemini_auth_missing"


def test_refresh_requires_refresh_token(fake_home, tmp_path):
    store = tmp_path / "gemini.json"
    gemini_auth.save_tokens({"access_token": "at", "expires_at": time.time() - 10}, store)
    with pytest.raises(GeminiAuthError) as excinfo:
        gemini_auth.refresh(path=store)
    assert excinfo.value.code == "gemini_auth_missing_refresh_token"


def test_exchange_code_persists_and_sends_verifier(fake_home, tmp_path):
    store = tmp_path / "gemini.json"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "at-exchanged",
                "refresh_token": "rt-exchanged",
                "expires_in": 1200,
                "id_token": _id_token(email="new@example.com"),
                "scope": gemini_auth.SCOPES,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tokens = gemini_auth.exchange_code(
        "the-code",
        code_verifier="verifier-1",
        redirect_uri="http://127.0.0.1:42813/oauth2callback",
        path=store,
        client=client,
    )
    body = parse_qs(seen[0].content.decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["the-code"]
    assert body["code_verifier"] == ["verifier-1"]
    assert body["redirect_uri"] == ["http://127.0.0.1:42813/oauth2callback"]
    assert body["client_id"] == [GEMINI_CLIENT_ID]
    assert tokens["access_token"] == "at-exchanged"
    assert tokens["email"] == "new@example.com"
    assert gemini_auth.load_tokens(store)["refresh_token"] == "rt-exchanged"


def test_exchange_code_empty_is_typed(fake_home, tmp_path):
    with pytest.raises(GeminiAuthError) as excinfo:
        gemini_auth.exchange_code("  ", code_verifier="v", path=tmp_path / "x.json")
    assert excinfo.value.code == "gemini_code_missing"


def test_login_url_has_pkce_scopes_and_offline_access(fake_home):
    request = gemini_auth.login_url(
        redirect_uri="http://127.0.0.1:42813/oauth2callback",
        state="state-1",
        code_verifier="verifier-1",
    )
    parsed = urlparse(request.url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert query["client_id"] == [GEMINI_CLIENT_ID]
    assert query["redirect_uri"] == ["http://127.0.0.1:42813/oauth2callback"]
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["state"] == ["state-1"]
    assert query["code_challenge_method"] == ["S256"]
    assert "cloud-platform" in query["scope"][0]
    assert request.code_verifier == "verifier-1"
    assert request.state == "state-1"


def test_is_expired_treats_unknown_expiry_as_live():
    assert gemini_auth.is_expired({"access_token": "a"}) is False
    assert gemini_auth.is_expired({"access_token": "a", "expires_at": time.time() - 1}) is True
    assert gemini_auth.is_expired(None) is True


def test_resolve_tokens_missing_is_typed_with_hint(fake_home, tmp_path):
    with pytest.raises(GeminiAuthError) as excinfo:
        gemini_auth.resolve_tokens(path=tmp_path / "absent.json")
    assert excinfo.value.code == "gemini_auth_missing"
    assert "machina auth gemini login" in str(excinfo.value)


def test_resolve_tokens_imports_cli_credentials(fake_home, tmp_path):
    cli_file = fake_home / ".gemini" / "oauth_creds.json"
    cli_file.parent.mkdir(parents=True)
    cli_file.write_text(json.dumps({"access_token": "cli-access", "refresh_token": "cli-rt"}))
    store = tmp_path / "gemini.json"
    tokens = gemini_auth.resolve_tokens(path=store)
    assert tokens["access_token"] == "cli-access"
    assert gemini_auth.load_tokens(store) is not None
