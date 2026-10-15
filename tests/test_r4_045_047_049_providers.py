"""Round-4 adversarial proofs: provider store + OAuth keyring policy.

Three verified findings, all driven through the real seams:

* R4-045 — ``MACHINA_OAUTH_ALLOW_PLAINTEXT_KEYRING`` is an allow-plaintext
  escape hatch, never an allowlist bypass: with the flag on the plaintext
  backend is accepted and unknown/fail/null backends are still rejected;
  with the flag off the strict allowlist applies unchanged.
* R4-047 — plaintext ``http://`` base URLs are loopback-only by default.
  A non-loopback target is refused (store and fetch); the explicit
  ``MACHINA_ALLOW_PLAINTEXT_HTTP=1`` opt-in proceeds and logs a visible
  warning naming the flag and host. The bearer header is only observed at
  the network boundary in the opted-in case.
* R4-049 — provider-store lock acquisition is bounded: while a real
  cross-process ``flock`` is held by a child process, the store operation
  fails with the typed ``ProviderStoreLockTimeout`` inside the window
  instead of blocking forever, leaving no store mutation and no leaked
  descriptors.

Boundary mocks: only the OS keyring (external backend) and the HTTP
transport (``httpx.MockTransport``) are stubbed. The lock adversary holds
the real ``flock`` in a real child process.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

from machina.security import oauth_keys, store_fence
from machina.security import providers as store
from machina.security.oauth_keys import OAuthKeyUnavailable

ROOT = Path(__file__).resolve().parents[1]

# ─── R4-045: keyring backend policy matrix ──────────────────────────────────


def _backend(module: str, name: str):
    backend = type(name, (), {})
    backend.__module__ = module
    return backend()


def _fake_keyring(backend):
    """Boundary stub: the OS keyring is external to the process."""

    class FakeKeyring:
        @staticmethod
        def get_keyring():
            return backend

        @staticmethod
        def get_password(*_args, **_kwargs):
            return None

        @staticmethod
        def set_password(*_args, **_kwargs):
            return None

    return FakeKeyring


_PLAINTEXT_BACKEND = ("keyring.backends.plaintext", "PlaintextKeyring")
_UNKNOWN_BACKEND = ("shadow.backends", "CustomBackend")
_NATIVE_BACKEND = ("keyring.backends.macos", "Keyring")
_FAIL_BACKEND = ("keyring.backends.fail", "Keyring")
_NULL_BACKEND = ("keyring.backends.null", "Keyring")


def _install_keyring(monkeypatch, backend_spec, flag):
    if flag is None:
        monkeypatch.delenv("MACHINA_OAUTH_ALLOW_PLAINTEXT_KEYRING", raising=False)
    else:
        monkeypatch.setenv("MACHINA_OAUTH_ALLOW_PLAINTEXT_KEYRING", flag)
    monkeypatch.delenv("MACHINA_OAUTH_MASTER_KEY", raising=False)
    module, name = backend_spec
    monkeypatch.setitem(sys.modules, "keyring", _fake_keyring(_backend(module, name)))


@pytest.mark.parametrize(
    ("backend", "flag", "allowed"),
    [
        (_PLAINTEXT_BACKEND, "1", True),  # the documented escape hatch
        (_PLAINTEXT_BACKEND, None, False),  # default: plaintext rejected
        (_PLAINTEXT_BACKEND, "0", False),  # precisely "1" enables it
        (_PLAINTEXT_BACKEND, "true", False),
        (_UNKNOWN_BACKEND, "1", False),  # the flag must not widen the allowlist
        (_UNKNOWN_BACKEND, None, False),
        (_NATIVE_BACKEND, "1", True),  # allowlisted backends unaffected
        (_NATIVE_BACKEND, None, True),
        (_FAIL_BACKEND, "1", False),  # fail/null rejected with the flag too
        (_FAIL_BACKEND, None, False),
        (_NULL_BACKEND, "1", False),
    ],
    ids=[
        "plaintext-flag-on-allowed",
        "plaintext-flag-unset-rejected",
        "plaintext-flag-0-rejected",
        "plaintext-flag-true-rejected",
        "unknown-flag-on-rejected",
        "unknown-flag-unset-rejected",
        "native-flag-on-allowed",
        "native-flag-unset-allowed",
        "fail-flag-on-rejected",
        "fail-flag-unset-rejected",
        "null-flag-on-rejected",
    ],
)
def test_keyring_backend_policy_matrix(monkeypatch, backend, flag, allowed):
    _install_keyring(monkeypatch, backend, flag)
    if allowed:
        key = oauth_keys.get_master_key()
        assert isinstance(key, bytes) and len(key) == 32
    else:
        with pytest.raises(OAuthKeyUnavailable):
            oauth_keys.get_master_key()


# ─── R4-047: plaintext http bearer policy ───────────────────────────────────


@pytest.fixture
def store_paths(tmp_path, monkeypatch):
    providers_file = tmp_path / "parts" / "providers.json"
    env_file = tmp_path / "home" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MACHINA_PROVIDERS_FILE", str(providers_file))
    monkeypatch.setenv("MACHINA_ENV_FILE", str(env_file))
    monkeypatch.delenv("MACHINA_ALLOW_PLAINTEXT_HTTP", raising=False)
    monkeypatch.delenv("MACHINA_PROVIDER_REMOTE_LLM_API_KEY", raising=False)
    yield {"providers": providers_file, "env": env_file}
    os.environ.pop("MACHINA_PROVIDER_REMOTE_LLM_API_KEY", None)


def test_non_loopback_plaintext_http_refused_by_default(store_paths):
    with pytest.raises(store.ProviderStoreError) as excinfo:
        store.add_provider(
            "remote-llm",
            "http://192.0.2.10:8000/v1",
            api_key="sk-cleartext",
            env_file=store_paths["env"],
        )
    message = str(excinfo.value)
    assert "MACHINA_ALLOW_PLAINTEXT_HTTP" in message
    assert "192.0.2.10" in message
    # Refusal is total: no store entry, no .env line, no live process key.
    assert not store_paths["providers"].exists()
    assert not store_paths["env"].exists()
    assert "MACHINA_PROVIDER_REMOTE_LLM_API_KEY" not in os.environ


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:9000/v1", "http://localhost:9001/v1", "http://[::1]:9002/v1"],
)
def test_loopback_plaintext_http_remains_allowed(store_paths, base_url):
    entry = store.add_provider("local-llm", base_url)
    assert entry["base_url"] == base_url


def test_opt_in_allows_non_loopback_http_with_visible_warning(store_paths, monkeypatch, caplog):
    monkeypatch.setenv("MACHINA_ALLOW_PLAINTEXT_HTTP", "1")
    with caplog.at_level(logging.WARNING, logger="machina.security.providers"):
        entry = store.add_provider(
            "remote-llm",
            "http://192.0.2.10:8000/v1",
            api_key="sk-cleartext",
            env_file=store_paths["env"],
        )
    assert entry["base_url"] == "http://192.0.2.10:8000/v1"
    assert store.get_provider_entry("remote-llm")["base_url"] == entry["base_url"]
    warnings = [r.getMessage() for r in caplog.records if r.name == "machina.security.providers"]
    assert any(
        "MACHINA_ALLOW_PLAINTEXT_HTTP" in line and "192.0.2.10" in line for line in warnings
    ), warnings


def test_opt_in_flag_must_be_exactly_one(store_paths, monkeypatch):
    monkeypatch.setenv("MACHINA_ALLOW_PLAINTEXT_HTTP", "true")
    with pytest.raises(store.ProviderStoreError):
        store.add_provider("remote-llm", "http://192.0.2.10:8000/v1", api_key="sk-x")


def test_https_to_non_loopback_host_is_unchanged(store_paths):
    entry = store.add_provider("remote-llm", "https://api.example.com/v1", api_key="sk-https")
    assert entry["base_url"] == "https://api.example.com/v1"
    assert store.get_provider_entry("remote-llm") is not None


def test_fetch_models_refuses_plaintext_bearer_to_non_loopback_by_default(store_paths):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(store.ProviderStoreError) as excinfo:
            store.fetch_models("http://192.0.2.10:8000/v1", api_key="sk-cleartext", client=client)
    assert "MACHINA_ALLOW_PLAINTEXT_HTTP" in str(excinfo.value)
    assert requests == []  # the bearer never reached the network boundary


def test_opted_in_cleartext_bearer_reaches_the_network_boundary(store_paths, monkeypatch):
    monkeypatch.setenv("MACHINA_ALLOW_PLAINTEXT_HTTP", "1")
    entry = store.add_provider(
        "remote-llm",
        "http://192.0.2.10:8000/v1",
        api_key="sk-cleartext",
        env_file=store_paths["env"],
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = store.fetch_models(entry["base_url"], api_key="sk-cleartext", client=client)
    assert [row["id"] for row in rows] == ["m1"]
    assert seen and seen[0].url.scheme == "http"
    assert seen[0].headers["authorization"] == "Bearer sk-cleartext"


# ─── R4-049: bounded provider-store lock (cross-process adversary) ──────────

_LOCK_HOLDER = textwrap.dedent(
    """
    import fcntl, os, sys, time

    lock_path, ready_path = sys.argv[1], sys.argv[2]
    os.makedirs(os.path.dirname(lock_path), mode=0o700, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    with open(ready_path, "w", encoding="ascii") as handle:
        handle.write("held")
    time.sleep(120)
    """
)

_ADD_UNDER_CONTENTION = textwrap.dedent(
    """
    import json, os, sys, time

    providers_file = sys.argv[1]
    timeout_override = float(sys.argv[2])
    os.environ["MACHINA_PROVIDERS_FILE"] = providers_file

    def open_fds():
        try:
            return {int(name) for name in os.listdir("/dev/fd") if name.isdigit()}
        except OSError:
            return None

    from machina.security import providers as store
    import machina.security.store_fence as store_fence
    if timeout_override:
        store_fence.STORE_LOCK_TIMEOUT_SECONDS = timeout_override
    before = open_fds()
    started = time.monotonic()
    error = None
    try:
        store.add_provider("contended", "http://127.0.0.1:9000/v1")
    except BaseException as exc:  # reported verbatim; the parent asserts the type
        error = {"type": type(exc).__name__, "message": str(exc)}
    elapsed = time.monotonic() - started
    after = open_fds()
    leaked = sorted(after - before) if before is not None and after is not None else None
    print(json.dumps({"error": error, "elapsed": elapsed, "leaked_fds": leaked}))
    """
)


def _wait_for_file(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"lock holder did not become ready: {path}")


def _child_env(providers_file: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["MACHINA_PROVIDERS_FILE"] = str(providers_file)
    env["PYTHONPATH"] = str(ROOT)
    return env


def _run_contended_add(providers_file: Path, timeout_override: str, window: float):
    try:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                _ADD_UNDER_CONTENTION,
                str(providers_file),
                timeout_override,
            ],
            capture_output=True,
            text=True,
            timeout=window,
            env=_child_env(providers_file),
            cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"lock acquisition blocked past the {window}s harness window: "
            "the provider-store lock is unbounded"
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock semantics are required")
def test_contended_store_lock_fails_bounded_with_typed_error(tmp_path):
    providers_file = tmp_path / "providers.json"
    lock_path = Path(str(providers_file) + ".lock")
    ready = tmp_path / "holder.ready"
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(lock_path), str(ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_file(ready)
        # Five compressed-clock repeats: typed error, bounded elapsed, no leak.
        for attempt in range(5):
            completed = _run_contended_add(providers_file, "0.4", window=10.0)
            assert completed.returncode == 0, completed.stderr
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
            assert payload["error"] is not None, f"attempt {attempt}: {payload}"
            assert payload["error"]["type"] == "ProviderStoreLockTimeout", (
                f"attempt {attempt}: {payload}"
            )
            assert payload["elapsed"] < 3.0, f"attempt {attempt}: {payload}"
            assert payload["leaked_fds"] in (None, []), f"attempt {attempt}: {payload}"
        # The bound now comes from the shared, single bounded-flock
        # implementation (P2-2): the provider store aliases
        # ``store_fence.STORE_LOCK_TIMEOUT_SECONDS`` rather than keeping its own
        # 5 s loop. Assert the sharing, then exercise the typed failure once more
        # under a compressed clock (the real default is exercised by the shared
        # store-lock tests).
        assert store._STORE_LOCK_TIMEOUT_SECONDS == store_fence.STORE_LOCK_TIMEOUT_SECONDS
        completed = _run_contended_add(providers_file, "0.4", window=10.0)
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        assert payload["error"] is not None and payload["error"]["type"] == (
            "ProviderStoreLockTimeout"
        ), payload
        assert payload["elapsed"] < 3.0, payload
        # The contended operation never wrote anything.
        assert not providers_file.exists()
    finally:
        holder.kill()
        holder.wait(timeout=10)
