"""Claude subscription auth: token store, Claude Code import, refresh, PKCE.

All HTTP is served by ``httpx.MockTransport``; no real network, no real
``~/.claude``/Keychain access, no real ``~/.machina`` state.
"""

from __future__ import annotations

import json
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from machina.core.config import config
from machina.security import claude_auth
from machina.security.claude_auth import (
    CLAUDE_CLIENT_ID,
    MANUAL_REDIRECT_URI,
    ClaudeAuthError,
)


def _tokens(*, access: str = "at-old", refresh: str = "rt-old", exp_in: float = 3600.0) -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": time.time() + exp_in,
        "scopes": claude_auth.SCOPES.split(),
        "subscription_type": "max",
    }


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Fake HOME with no Machina store and no Claude Code credentials."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MACHINA_CLAUDE_AUTH_FILE", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("MACHINA_HOME", raising=False)
    monkeypatch.delenv("MACHINA_MACHINA_HOME", raising=False)
    monkeypatch.setattr(config, "machina_home", "", raising=False)
    # Never let a real macOS Keychain lookup run in tests.
    monkeypatch.setattr(claude_auth, "_read_claude_code_keychain", lambda: None)
    return home


def _write_claude_code(
    home: Path, *, access: str = "cc-access", refresh: str = "cc-refresh"
) -> Path:
    creds = home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": access,
                    "refreshToken": refresh,
                    "expiresAt": int((time.time() + 1800) * 1000),
                    "scopes": ["user:inference"],
                    "subscriptionType": "pro",
                    "rateLimitTier": "default",
                }
            }
        )
    )
    return creds


def test_save_load_roundtrip_and_permissions(fake_home, tmp_path):
    store = tmp_path / "claude.json"
    saved = claude_auth.save_tokens(_tokens(), store)
    assert saved == store
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.parent.stat().st_mode) == 0o700
    loaded = claude_auth.load_tokens(store)
    assert loaded is not None
    assert loaded["access_token"] == "at-old"
    assert loaded["subscription_type"] == "max"
    assert loaded["scopes"] == claude_auth.SCOPES.split()
    raw = json.loads(store.read_text())
    assert set(raw) == set(claude_auth._STORE_FIELDS)


def test_import_from_claude_code_is_read_only(fake_home):
    creds = _write_claude_code(fake_home)
    before = creds.read_bytes()
    tokens = claude_auth.import_from_claude_code()
    assert tokens is not None
    assert tokens["access_token"] == "cc-access"
    assert tokens["refresh_token"] == "cc-refresh"
    assert tokens["subscription_type"] == "pro"
    assert creds.read_bytes() == before
    assert claude_auth.load_tokens() is not None


def test_import_honours_claude_config_dir(fake_home, tmp_path, monkeypatch):
    custom = tmp_path / "claude-config"
    custom.mkdir()
    (custom / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "custom-at", "refreshToken": "custom-rt"}})
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    assert claude_auth.claude_code_auth_path() == custom / ".credentials.json"
    tokens = claude_auth.import_from_claude_code(force=True)
    assert tokens is not None
    assert tokens["access_token"] == "custom-at"


def test_import_reads_keychain_when_no_file(fake_home, monkeypatch):
    payload = {
        "claudeAiOauth": {
            "accessToken": "kc-at",
            "refreshToken": "kc-rt",
            "expiresAt": int((time.time() + 600) * 1000),
        }
    }
    fake = subprocess.CompletedProcess(
        args=["security"], returncode=0, stdout=json.dumps(payload), stderr=""
    )
    monkeypatch.setattr(claude_auth, "_read_claude_code_keychain", lambda: payload)
    monkeypatch.setattr(claude_auth.subprocess, "run", lambda *a, **k: fake)
    tokens = claude_auth.import_from_claude_code()
    assert tokens is not None
    assert tokens["access_token"] == "kc-at"


def test_import_refused_when_store_exists_unless_forced(fake_home, tmp_path):
    _write_claude_code(fake_home)
    store = tmp_path / "claude.json"
    claude_auth.save_tokens(_tokens(access="machina-at", refresh="machina-rt"), store)
    assert claude_auth.import_from_claude_code(path=store) is None
    forced = claude_auth.import_from_claude_code(path=store, force=True)
    assert forced is not None
    assert forced["access_token"] == "cc-access"


def test_status_reports_sources(fake_home, tmp_path):
    store = tmp_path / "claude.json"
    report = claude_auth.status(path=store)
    assert report["signed_in"] is False
    _write_claude_code(fake_home)
    report = claude_auth.status(path=store)
    assert report["signed_in"] is True
    assert report["source"] == "claude"
    assert report["subscription_type"] == "pro"
    claude_auth.save_tokens(_tokens(), store)
    report = claude_auth.status(path=store)
    assert report["source"] == "machina"
    assert "at-old" not in json.dumps(report)


def test_refresh_posts_json_with_beta_header_and_persists(fake_home, tmp_path):
    store = tmp_path / "claude.json"
    claude_auth.save_tokens(_tokens(), store)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"access_token": "at-new", "expires_in": 3600, "scope": claude_auth.SCOPES},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tokens = claude_auth.refresh(path=store, client=client)
    assert tokens["access_token"] == "at-new"
    assert tokens["refresh_token"] == "rt-old"
    body = json.loads(seen[0].content.decode())
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "rt-old"
    assert body["client_id"] == CLAUDE_CLIENT_ID
    assert seen[0].headers["anthropic-beta"] == claude_auth.OAUTH_BETA
    assert str(seen[0].url) == claude_auth.TOKEN_URL


def test_refresh_rotates_and_adopts_peer_store(fake_home, tmp_path):
    store = tmp_path / "claude.json"
    claude_auth.save_tokens(_tokens(access="at-old", refresh="rt-old"), store)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": "at-new", "refresh_token": "rt-new"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rotated = claude_auth.refresh(_tokens(refresh="rt-old"), path=store, client=client)
    assert rotated["refresh_token"] == "rt-new"
    # A peer rotates the store; a later refresh call with the stale pair adopts
    # the store instead of replaying the consumed token.
    claude_auth.save_tokens(
        {"access_token": "at-peer", "refresh_token": "rt-peer", "expires_at": time.time() + 3600},
        store,
    )
    adopted = claude_auth.refresh(rotated, path=store, client=client)
    assert adopted["access_token"] == "at-peer"
    assert len(calls) == 1  # no second POST: the peer pair was adopted


def test_refresh_falls_back_to_console_host_on_404(fake_home, tmp_path):
    store = tmp_path / "claude.json"
    claude_auth.save_tokens(_tokens(), store)
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if "platform.claude.com" in str(request.url):
            return httpx.Response(404, json={"error": "not_found"})
        return httpx.Response(200, json={"access_token": "at-console", "expires_in": 60})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tokens = claude_auth.refresh(path=store, client=client)
    assert tokens["access_token"] == "at-console"
    assert urls == [claude_auth.TOKEN_URL, claude_auth.CONSOLE_TOKEN_URL]


def test_refresh_invalid_grant_is_typed_with_hint(fake_home, tmp_path):
    store = tmp_path / "claude.json"
    claude_auth.save_tokens(_tokens(), store)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": "invalid_grant"})
        )
    )
    with pytest.raises(ClaudeAuthError) as excinfo:
        claude_auth.refresh(path=store, client=client)
    assert excinfo.value.code == "invalid_grant"
    assert "claude" in str(excinfo.value)


def test_exchange_code_sends_state_and_persists(fake_home, tmp_path):
    store = tmp_path / "claude.json"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "at-exchanged",
                "refresh_token": "rt-exchanged",
                "expires_in": 900,
                "scope": claude_auth.SCOPES,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tokens = claude_auth.exchange_code(
        "code-1",
        code_verifier="verifier-1",
        redirect_uri=MANUAL_REDIRECT_URI,
        state="state-1",
        path=store,
        client=client,
    )
    body = json.loads(seen[0].content.decode())
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "code-1"
    assert body["code_verifier"] == "verifier-1"
    assert body["state"] == "state-1"
    assert body["redirect_uri"] == MANUAL_REDIRECT_URI
    assert tokens["access_token"] == "at-exchanged"
    assert claude_auth.load_tokens(store)["refresh_token"] == "rt-exchanged"


def test_parse_manual_code_splits_state():
    assert claude_auth.parse_manual_code("abc#def") == ("abc", "def")
    assert claude_auth.parse_manual_code("  abc  ") == ("abc", "")


def test_login_url_loopback_and_manual_variants(fake_home):
    loopback = claude_auth.login_url(
        redirect_uri="http://localhost:41519/callback",
        state="s1",
        code_verifier="v1",
    )
    query = parse_qs(urlparse(loopback.url).query)
    assert urlparse(loopback.url).netloc == "claude.com"
    assert query["client_id"] == [CLAUDE_CLIENT_ID]
    assert query["redirect_uri"] == ["http://localhost:41519/callback"]
    assert query["code"] == ["true"]
    assert query["code_challenge_method"] == ["S256"]
    assert "user:inference" in query["scope"][0]

    manual = claude_auth.login_url(manual=True, state="s2", code_verifier="v2")
    manual_query = parse_qs(urlparse(manual.url).query)
    assert manual_query["redirect_uri"] == [MANUAL_REDIRECT_URI]


def test_resolve_tokens_missing_is_typed_with_hint(fake_home, tmp_path):
    with pytest.raises(ClaudeAuthError) as excinfo:
        claude_auth.resolve_tokens(path=tmp_path / "absent.json")
    assert excinfo.value.code == "claude_auth_missing"
    assert "machina auth claude login" in str(excinfo.value)
