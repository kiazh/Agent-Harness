"""ChatGPT-account auth: token store, Codex import, refresh, PKCE.

All HTTP is served by ``httpx.MockTransport``; no real network, no real
``~/.codex`` or ``~/.machina`` filesystem state leaks into the tests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from machina.core.config import config
from machina.security import chatgpt_auth
from machina.security.chatgpt_auth import (
    CALLBACK_PATH,
    CALLBACK_PORT,
    CODEX_CLIENT_ID,
    REDIRECT_URI,
    ChatGPTAuthError,
)


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


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


# ─── token store ────────────────────────────────────────────────────────────


def test_save_load_roundtrip_is_atomic_and_0600(tmp_path):
    target = tmp_path / "auth" / "chatgpt.json"
    expected = _tokens()
    chatgpt_auth.save_tokens(expected, target)

    assert target.exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list((tmp_path / "auth").glob("*.tmp")) == []

    loaded = chatgpt_auth.load_tokens(target)
    assert loaded is not None
    assert loaded["access_token"] == expected["access_token"]
    assert loaded["refresh_token"] == "rt-old"
    assert loaded["account_id"] == "acct-123"
    assert loaded["expires_at"] == pytest.approx(time.time() + 3600, abs=30)
    assert loaded["last_refresh"]


def test_save_tokens_rewrites_0600_on_existing_file(tmp_path):
    target = tmp_path / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(), target)
    target.chmod(0o644)
    chatgpt_auth.save_tokens(_tokens(refresh="rt-new"), target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert chatgpt_auth.load_tokens(target)["refresh_token"] == "rt-new"


def test_load_tokens_missing_or_invalid_returns_none(tmp_path):
    assert chatgpt_auth.load_tokens(tmp_path / "absent.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert chatgpt_auth.load_tokens(broken) is None
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"tokens": {"refresh_token": "rt"}}))
    assert chatgpt_auth.load_tokens(empty) is None


# ─── id_token account extraction ────────────────────────────────────────────


def test_account_id_extracted_from_scoped_id_token_claim():
    token = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-xyz"}})
    assert chatgpt_auth.extract_account_id(token) == "acct-xyz"


def test_account_id_tolerates_access_token_and_top_level_fallbacks():
    scoped = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "from-access"}})
    assert chatgpt_auth.extract_account_id(scoped) == "from-access"
    flattened = _jwt({"chatgpt_account_id": "flat"})
    assert chatgpt_auth.extract_account_id(flattened) == "flat"
    assert chatgpt_auth.extract_account_id("not-a-jwt") is None
    assert chatgpt_auth.extract_account_id(_jwt({"sub": "user"})) is None
    assert chatgpt_auth.extract_account_id(None) is None


def test_store_prefers_id_token_account_over_access_token(tmp_path):
    auth = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-id"}}
    other = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-access"}}
    chatgpt_auth.save_tokens(
        {
            "access_token": _jwt(other),
            "refresh_token": "rt",
            "id_token": _jwt(auth),
        },
        tmp_path / "chatgpt.json",
    )
    loaded = chatgpt_auth.load_tokens(tmp_path / "chatgpt.json")
    assert loaded["account_id"] == "acct-id"


# ─── expiry ─────────────────────────────────────────────────────────────────


def test_is_expired_uses_skew_and_unknown_stays_callable():
    assert chatgpt_auth.is_expired({"access_token": "", "expires_at": time.time() + 999}) is True
    assert chatgpt_auth.is_expired(_tokens(exp_in=30)) is True  # inside 120s skew
    assert chatgpt_auth.is_expired(_tokens(exp_in=3600)) is False
    assert chatgpt_auth.is_expired(_tokens(exp_in=30), skew_seconds=0) is False
    # Unknown expiry is not treated as expired; the wire 401 drives refresh.
    assert chatgpt_auth.is_expired({"access_token": "opaque"}) is False


# ─── Codex CLI import ───────────────────────────────────────────────────────


def test_import_from_codex_copies_tokens_and_leaves_source_readonly(fake_home):
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir()
    codex_file = codex_dir / "auth.json"
    payload = {"auth_mode": "chatgpt", "tokens": _tokens(account="acct-codex")}
    codex_file.write_text(json.dumps(payload))
    before_bytes = codex_file.read_bytes()
    before_mtime = codex_file.stat().st_mtime_ns

    imported = chatgpt_auth.import_from_codex()

    assert imported is not None
    assert imported["account_id"] == "acct-codex"
    assert imported["refresh_token"] == "rt-old"
    store = fake_home / ".machina" / "auth" / "chatgpt.json"
    assert store.exists()
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert chatgpt_auth.load_tokens(store)["access_token"] == imported["access_token"]
    # The Codex CLI's file is read-only for us: bytes and mtime untouched.
    assert codex_file.read_bytes() == before_bytes
    assert codex_file.stat().st_mtime_ns == before_mtime


def test_import_from_codex_never_clobbers_existing_machina_store(fake_home, tmp_path):
    store = tmp_path / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(refresh="rt-machina"), store)
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text(json.dumps({"tokens": _tokens(refresh="rt-codex")}))

    assert chatgpt_auth.import_from_codex(path=store) is None
    assert chatgpt_auth.load_tokens(store)["refresh_token"] == "rt-machina"


def test_import_from_codex_absent_file_returns_none(fake_home):
    assert chatgpt_auth.import_from_codex() is None


# ─── refresh ────────────────────────────────────────────────────────────────


def test_refresh_rotates_refresh_token_and_persists(tmp_path):
    target = tmp_path / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(refresh="rt-old"), target)
    seen: dict[str, str] = {}
    new_access = _jwt(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"},
            "exp": time.time() + 7200,
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(
            200, json={"access_token": new_access, "refresh_token": "rt-new", "expires_in": 7200}
        )

    updated = chatgpt_auth.refresh(
        chatgpt_auth.load_tokens(target), path=target, client=_mock_client(handler)
    )

    assert seen["url"] == chatgpt_auth.TOKEN_URL
    assert "grant_type=refresh_token" in seen["body"]
    assert "refresh_token=rt-old" in seen["body"]
    assert f"client_id={CODEX_CLIENT_ID}" in seen["body"]
    assert updated["refresh_token"] == "rt-new"
    assert updated["account_id"] == "acct-123"
    stored = chatgpt_auth.load_tokens(target)
    assert stored["refresh_token"] == "rt-new"
    assert stored["access_token"] == new_access


def test_refresh_keeps_old_refresh_token_when_server_omits_it(tmp_path):
    target = tmp_path / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(refresh="rt-stay"), target)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": _tokens()["access_token"]})

    updated = chatgpt_auth.refresh(
        chatgpt_auth.load_tokens(target), path=target, client=_mock_client(handler)
    )
    assert updated["refresh_token"] == "rt-stay"


def test_refresh_failure_is_typed_with_login_hint_and_no_token_material(tmp_path):
    target = tmp_path / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(refresh="rt-secret"), target)
    tokens = chatgpt_auth.load_tokens(target)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"code": "invalid_grant", "message": "refresh token revoked"}}
        )

    with pytest.raises(ChatGPTAuthError) as excinfo:
        chatgpt_auth.refresh(tokens, path=target, client=_mock_client(handler))

    message = str(excinfo.value)
    assert excinfo.value.code == "invalid_grant"
    assert "codex login" in message
    assert "rt-secret" not in message
    assert tokens["access_token"] not in message
    # The stored chain is untouched on failure.
    assert chatgpt_auth.load_tokens(target)["refresh_token"] == "rt-secret"


def test_refresh_reports_rate_limit_without_relogin(tmp_path):
    target = tmp_path / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(), target)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    with pytest.raises(ChatGPTAuthError) as excinfo:
        chatgpt_auth.refresh(
            chatgpt_auth.load_tokens(target), path=target, client=_mock_client(handler)
        )
    assert excinfo.value.code == "chatgpt_refresh_rate_limited"
    assert "codex login" not in str(excinfo.value)


def test_refresh_adopts_peer_rotation_instead_of_replaying(tmp_path):
    target = tmp_path / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(refresh="rt-newer"), target)
    stale = _tokens(refresh="rt-consumed")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("peer adoption must not hit the network")

    adopted = chatgpt_auth.refresh(stale, path=target, client=_mock_client(handler))
    assert adopted["refresh_token"] == "rt-newer"


def test_refresh_without_refresh_token_is_typed_login_error(tmp_path):
    target = tmp_path / "chatgpt.json"
    tokens = _tokens()
    tokens["refresh_token"] = ""
    chatgpt_auth.save_tokens(tokens, target)

    with pytest.raises(ChatGPTAuthError) as excinfo:
        chatgpt_auth.refresh(None, path=target)
    assert excinfo.value.code == "chatgpt_auth_missing_refresh_token"
    assert "codex login" in str(excinfo.value)


# ─── resolution ─────────────────────────────────────────────────────────────


def test_resolve_tokens_imports_codex_when_store_missing(fake_home):
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text(json.dumps({"tokens": _tokens(account="acct-cli")}))

    resolved = chatgpt_auth.resolve_tokens()

    assert resolved["account_id"] == "acct-cli"
    assert (fake_home / ".machina" / "auth" / "chatgpt.json").exists()


def test_resolve_tokens_missing_credentials_raises_with_hint(fake_home):
    with pytest.raises(ChatGPTAuthError) as excinfo:
        chatgpt_auth.resolve_tokens()
    assert excinfo.value.code == "chatgpt_auth_missing"
    assert "codex login" in str(excinfo.value)


def test_login_hint_points_at_the_landed_auth_login_command():
    """F7: `machina auth login` shipped — the hint must not call it future."""
    assert "machina auth login" in chatgpt_auth.LOGIN_HINT
    assert "once that command lands" not in chatgpt_auth.LOGIN_HINT


def test_status_reports_source_and_never_tokens(fake_home):
    assert chatgpt_auth.status()["signed_in"] is False

    codex_dir = fake_home / ".codex"
    codex_dir.mkdir()
    tokens = _tokens(exp_in=1800)
    (codex_dir / "auth.json").write_text(json.dumps({"tokens": tokens}))
    peeked = chatgpt_auth.status()
    assert peeked["signed_in"] is True
    assert peeked["source"] == "codex"
    assert peeked["account_id"] == "acct-123"
    assert peeked["expired"] is False
    assert not (fake_home / ".machina").exists()  # read-only peek
    assert tokens["access_token"] not in json.dumps(peeked)
    assert tokens["refresh_token"] not in json.dumps(peeked)

    store = fake_home / ".machina" / "auth" / "chatgpt.json"
    chatgpt_auth.save_tokens(_tokens(account="acct-machina", exp_in=-5), store)
    own = chatgpt_auth.status()
    assert own["source"] == "machina"
    assert own["account_id"] == "acct-machina"
    assert own["expired"] is True


# ─── default path ───────────────────────────────────────────────────────────


def test_default_auth_path_uses_fake_home(fake_home):
    assert chatgpt_auth.auth_path() == fake_home / ".machina" / "auth" / "chatgpt.json"


def test_auth_path_env_override_wins(monkeypatch, tmp_path):
    override = tmp_path / "custom.json"
    monkeypatch.setenv("MACHINA_CHATGPT_AUTH_FILE", str(override))
    assert chatgpt_auth.auth_path() == override


# ─── PKCE login flow ────────────────────────────────────────────────────────


def test_login_url_is_pkce_s256_on_the_fixed_loopback_redirect():
    request = chatgpt_auth.login_url()
    parsed = urlparse(request.url)
    query = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == chatgpt_auth.AUTHORIZE_URL
    assert query["client_id"] == [CODEX_CLIENT_ID]
    assert query["redirect_uri"] == [f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert "offline_access" in query["scope"][0]
    assert len(query["state"][0]) >= 16
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(request.code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode()
    )
    assert query["code_challenge"] == [expected_challenge]
    assert request.state == query["state"][0]


def test_login_url_honors_injected_state_and_verifier():
    request = chatgpt_auth.login_url(state="fixed-state", code_verifier="v" * 43)
    assert "state=fixed-state" in request.url
    assert request.code_verifier == "v" * 43


def test_exchange_code_persists_tokens(tmp_path):
    target = tmp_path / "chatgpt.json"
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "access_token": _tokens(account="acct-flow")["access_token"],
                "refresh_token": "rt-flow",
                "id_token": _tokens(account="acct-flow")["id_token"],
            },
        )

    tokens = chatgpt_auth.exchange_code(
        "the-code",
        code_verifier="verifier-123",
        path=target,
        client=_mock_client(handler),
    )

    assert "grant_type=authorization_code" in seen["body"]
    assert "code=the-code" in seen["body"]
    assert "code_verifier=verifier-123" in seen["body"]
    assert f"client_id={CODEX_CLIENT_ID}" in seen["body"]
    assert tokens["refresh_token"] == "rt-flow"
    assert chatgpt_auth.load_tokens(target)["account_id"] == "acct-flow"


def test_exchange_code_failure_is_typed(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_request", "error_description": "bad verifier"}
        )

    with pytest.raises(ChatGPTAuthError) as excinfo:
        chatgpt_auth.exchange_code(
            "code", code_verifier="v", path=tmp_path / "chatgpt.json", client=_mock_client(handler)
        )
    assert excinfo.value.code == "invalid_request"
    assert "bad verifier" in str(excinfo.value)
    assert not (tmp_path / "chatgpt.json").exists()


def test_exchange_code_empty_code_rejected(tmp_path):
    with pytest.raises(ChatGPTAuthError):
        chatgpt_auth.exchange_code("  ", code_verifier="v", path=tmp_path / "chatgpt.json")


def test_store_path_stays_writable_directory_permissions(fake_home):
    tokens = _tokens()
    chatgpt_auth.save_tokens(tokens)
    auth_dir = fake_home / ".machina" / "auth"
    assert auth_dir.exists()
    assert stat.S_IMODE(auth_dir.stat().st_mode) == 0o700
    assert Path(chatgpt_auth.auth_path()).name == "chatgpt.json"


# ─── redaction registration (R3-1) ──────────────────────────────────────────


def _opaque_tokens(suffix: str = "old") -> dict:
    """Non-JWT values so only the exact-value registry can redact them."""
    return {
        "access_token": f"SYNTHETIC_ACCESS_{suffix}_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "refresh_token": f"SYNTHETIC_REFRESH_{suffix}_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "account_id": "synthetic-account",
        "expires_at": time.time() - 300,
    }


def test_saved_and_loaded_token_values_are_redactable(tmp_path):
    from machina.memory.redaction import redact_secrets

    tokens = _opaque_tokens()
    store = tmp_path / "chatgpt.json"
    chatgpt_auth.save_tokens(tokens, store)
    for field in ("access_token", "refresh_token"):
        # Bare values: no regex pattern matches these strings, so a redaction
        # here proves the credential boundary registered the exact bytes.
        assert redact_secrets(tokens[field]).text == "[REDACTED_SECRET]"

    assert chatgpt_auth.load_tokens(store) is not None  # load boundary too
    raw = store.read_text()
    assert tokens["access_token"] in raw  # sanity: raw store content has them
    redacted = redact_secrets(raw).text
    assert tokens["access_token"] not in redacted
    assert tokens["refresh_token"] not in redacted


# ─── serialization + generation fence (R3-2/R3-3) ───────────────────────────


def test_refresh_serializes_concurrent_callers_to_one_exchange(tmp_path):
    target = tmp_path / "chatgpt.json"
    chatgpt_auth.save_tokens(_opaque_tokens(), target)
    lock = threading.Lock()
    requests: list[str] = []
    start = threading.Barrier(2)

    def handler(request: httpx.Request) -> httpx.Response:
        supplied = parse_qs(request.content.decode())["refresh_token"][0]
        with lock:
            requests.append(supplied)
        time.sleep(0.2)  # hold the store lock so the peer must wait it out
        return httpx.Response(
            200,
            json={
                "access_token": _opaque_tokens("fresh")["access_token"],
                "refresh_token": _opaque_tokens("fresh")["refresh_token"],
            },
        )

    def call() -> str:
        with _mock_client(handler) as client:
            start.wait(timeout=10)
            try:
                return chatgpt_auth.refresh(_opaque_tokens(), path=target, client=client)[
                    "refresh_token"
                ]
            except ChatGPTAuthError as error:  # pragma: no cover - the race bug
                return f"ERROR:{error.code}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: call(), range(2)))

    assert requests == [_opaque_tokens()["refresh_token"]], "one exchange only"
    fresh = _opaque_tokens("fresh")["refresh_token"]
    assert results == [fresh, fresh], "both callers get the rotated pair"
    assert chatgpt_auth.load_tokens(target)["refresh_token"] == fresh


def test_refresh_late_response_never_resurrects_a_removed_store(tmp_path):
    target = tmp_path / "chatgpt.json"
    chatgpt_auth.save_tokens(_opaque_tokens(), target)
    dispatched = threading.Event()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        dispatched.set()
        assert release.wait(timeout=5)
        return httpx.Response(
            200,
            json={
                "access_token": _opaque_tokens("fresh")["access_token"],
                "refresh_token": _opaque_tokens("fresh")["refresh_token"],
            },
        )

    def call() -> dict:
        with _mock_client(handler) as client:
            return chatgpt_auth.refresh(_opaque_tokens(), path=target, client=client)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call)
        assert dispatched.wait(timeout=5)
        target.unlink()  # logout (raw removal) while the response is in flight
        release.set()
        updated = future.result(timeout=5)

    # The caller keeps the fresh in-memory pair; the store stays removed.
    assert updated["refresh_token"] == _opaque_tokens("fresh")["refresh_token"]
    assert not target.exists()


def test_logout_bumps_generation_durably_and_fences_late_exchange(tmp_path):
    target = tmp_path / "chatgpt.json"
    chatgpt_auth.save_tokens(_opaque_tokens(), target)
    generation = chatgpt_auth.credential_generation(target)

    removed = chatgpt_auth.logout(path=target)

    assert removed is True
    assert not target.exists()
    assert chatgpt_auth.credential_generation(target) == generation + 1
    generation_file = tmp_path / "chatgpt.json.generation"
    assert int(generation_file.read_text().strip()) == generation + 1

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("a fenced exchange must publish nothing")

    with pytest.raises(ChatGPTAuthError) as excinfo:
        chatgpt_auth.exchange_code(
            "code",
            code_verifier="verifier",
            path=target,
            client=_mock_client(handler),
            generation=generation,
        )
    assert excinfo.value.code == "chatgpt_auth_cancelled"
    assert not target.exists(), "a late callback must not recreate the store"


def test_exchange_code_publishes_with_the_current_generation(tmp_path):
    target = tmp_path / "chatgpt.json"
    generation = chatgpt_auth.credential_generation(target)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": _opaque_tokens("login")["access_token"],
                "refresh_token": _opaque_tokens("login")["refresh_token"],
            },
        )

    tokens = chatgpt_auth.exchange_code(
        "code",
        code_verifier="verifier",
        path=target,
        client=_mock_client(handler),
        generation=generation,
    )
    assert tokens["refresh_token"] == _opaque_tokens("login")["refresh_token"]
    assert chatgpt_auth.load_tokens(target) is not None


def test_exchange_code_refuses_to_clobber_a_store_that_changed(tmp_path):
    target = tmp_path / "chatgpt.json"
    chatgpt_auth.save_tokens(_opaque_tokens("existing"), target)

    def handler(request: httpx.Request) -> httpx.Response:
        # A peer logout removes the store while the exchange is in flight.
        target.unlink()
        return httpx.Response(
            200,
            json={
                "access_token": _opaque_tokens("late")["access_token"],
                "refresh_token": _opaque_tokens("late")["refresh_token"],
            },
        )

    with pytest.raises(ChatGPTAuthError) as excinfo:
        chatgpt_auth.exchange_code(
            "code", code_verifier="verifier", path=target, client=_mock_client(handler)
        )
    assert excinfo.value.code == "chatgpt_auth_cancelled"
    assert not target.exists()


def test_invalidate_credentials_without_a_store_still_bumps(tmp_path):
    target = tmp_path / "chatgpt.json"
    before = chatgpt_auth.credential_generation(target)
    after = chatgpt_auth.invalidate_credentials(target)
    assert after == before + 1
    assert chatgpt_auth.credential_generation(target) == after


def test_import_from_codex_refuses_after_a_concurrent_logout(fake_home, monkeypatch):
    codex_dir = fake_home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text(json.dumps({"tokens": _tokens()}))
    store = fake_home / ".machina" / "auth" / "chatgpt.json"
    real_read = chatgpt_auth._read_codex_tokens

    def read_then_invalidate(*args, **kwargs):
        tokens = real_read(*args, **kwargs)
        chatgpt_auth.invalidate_credentials(store)  # logout lands mid-import
        return tokens

    monkeypatch.setattr(chatgpt_auth, "_read_codex_tokens", read_then_invalidate)
    assert chatgpt_auth.import_from_codex() is None
    assert not store.exists(), "a logout-fenced import must publish nothing"
