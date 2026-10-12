"""Task 2: encrypted grant store, key source, generation/tombstone rules."""

import asyncio
import base64
import json
import os
import stat

import pytest

from ah.connectors.oauth_models import OAuthBinding
from ah.connectors.oauth_store import (
    OAuthCredentialRecord,
    OAuthCredentialStore,
    credential_key_for,
)
from ah.security import oauth_keys
from ah.security.oauth_keys import OAuthKeyUnavailable

SECRET = "test-access-token-secret-value"
REFRESH = "test-refresh-token-secret-value"


def _key_provider():
    return b"k" * 32


def _binding(**overrides):
    key = credential_key_for(
        connector_id="web",
        resource="https://mcp.example.com/mcp",
        issuer="https://auth.example.com",
        client_id="client-1",
    )
    base = {
        "connector_id": "web",
        "credential_key": key,
        "resource": "https://mcp.example.com/mcp",
        "issuer": "https://auth.example.com",
        "client_id": "client-1",
    }
    base.update(overrides)
    return OAuthBinding(**base)


def _record(binding, **overrides):
    base = {
        "version": 1,
        "credential_key": binding.credential_key,
        "connector_id": "web",
        "config_revision": 1,
        "resource": binding.resource,
        "issuer": binding.issuer,
        "client_id": "client-1",
        "token_endpoint": "https://auth.example.com/token",
        "authorization_endpoint": "https://auth.example.com/authorize",
        "grant_id": "grant-1",
        "generation": 1,
        "approved_scopes": ("read",),
        "granted_scopes": ("read",),
        "access_token": SECRET,
        "refresh_token": REFRESH,
        "acquired_at_utc": "2026-01-01T00:00:00+00:00",
        "expires_at_utc": "",
        "refresh_state": "idle",
    }
    base.update(overrides)
    return OAuthCredentialRecord(**base)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["save", "forget"])
@pytest.mark.parametrize("failure", ["timeout", "exception"])
async def test_failed_index_lock_closes_descriptor_without_changing_authority(
    tmp_path, monkeypatch, operation, failure
):
    from ah.connectors import oauth_store

    store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
    binding = _binding()
    await store.save(_record(binding), expected_generation=0)
    index_before = store._index_path().read_bytes()
    envelopes_before = set((store.root / "grants").iterdir())
    descriptors = []

    def fail_acquire(fd, **kwargs):
        descriptors.append(fd)
        if failure == "exception":
            raise OSError("synthetic acquisition error")
        return False

    monkeypatch.setattr(oauth_store, "_file_lock_acquire", fail_acquire)
    try:
        with pytest.raises(TimeoutError if failure == "timeout" else OSError):
            if operation == "save":
                await store.save(_record(binding, generation=2), expected_generation=1)
            else:
                await store.forget(binding, expected_generation=1)
        assert descriptors
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert store._index_path().read_bytes() == index_before
        assert set((store.root / "grants").iterdir()) == envelopes_before
        assert (await store.load(binding)).generation == 1
    finally:
        # Close a leaked descriptor when verifying the unfixed implementation.
        for fd in descriptors:
            try:
                os.fstat(fd)
            except OSError:
                pass
            else:
                os.close(fd)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["rotate", "forget"])
async def test_load_retries_when_snapshot_envelope_is_replaced(tmp_path, monkeypatch, mutation):
    import threading

    reader = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
    writer = OAuthCredentialStore(reader.root, key_provider=_key_provider)
    binding = _binding()
    await writer.save(_record(binding), expected_generation=0)
    captured, resume = threading.Event(), threading.Event()
    original = reader._read_index

    def pause_first_snapshot():
        index = original()
        if not captured.is_set():
            captured.set()
            assert resume.wait(3)
        return index

    monkeypatch.setattr(reader, "_read_index", pause_first_snapshot)
    task = asyncio.create_task(reader.load(binding))
    try:
        async with asyncio.timeout(2):
            while not captured.is_set():
                await asyncio.sleep(0.001)
        if mutation == "rotate":
            await writer.save(
                _record(binding, access_token="rotated-test-token"), expected_generation=1
            )
        else:
            await writer.forget(binding, expected_generation=1)
        resume.set()
        result = await task
        if mutation == "rotate":
            assert result.access_token == "rotated-test-token"
        else:
            assert result is None
    finally:
        resume.set()
        await asyncio.gather(task, return_exceptions=True)


def test_encrypted_grant_survives_restart_and_contains_no_secret_bytes(tmp_path):
    async def _run():
        store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
        binding = _binding()
        await store.save(_record(binding), expected_generation=0)
        identity = await store.public_identity(binding)
        assert identity is not None and identity["grantId"] == "grant-1"
        # Reopen (restart): recover tokens.
        reopened = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
        loaded = await reopened.load(binding)
        assert loaded is not None and loaded.access_token == SECRET
        assert loaded.refresh_token == REFRESH
        return identity

    identity = asyncio.run(_run())
    assert SECRET not in json.dumps(identity) and REFRESH not in json.dumps(identity)
    for path in (tmp_path / "oauth").rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()
            assert REFRESH.encode() not in path.read_bytes()
    assert "grant-1" in json.dumps(identity)


def test_tamper_wrong_key_wrong_aad_rejected(tmp_path):
    async def _run():
        store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
        binding = _binding()
        await store.save(_record(binding), expected_generation=0)
        # Tamper with envelope bytes.
        index = json.loads((tmp_path / "oauth" / "index.json").read_text())
        envelope = index["grants"][binding.credential_key]["envelope"]
        envelope_path = tmp_path / "oauth" / "grants" / envelope
        raw = bytearray(envelope_path.read_bytes())
        raw[len(raw) // 2] ^= 1
        envelope_path.write_bytes(bytes(raw))
        with pytest.raises(ValueError):
            await store.load(binding)
        # Wrong key.
        wrong = OAuthCredentialStore(tmp_path / "oauth", key_provider=lambda: b"z" * 32)
        with pytest.raises(ValueError):
            await wrong.load(binding)
        # Wrong AAD (different credential key).
        other = _binding(connector_id="other")
        with pytest.raises(ValueError):
            await store.load(other)

    asyncio.run(_run())


def test_atomic_replacement_fault_and_file_security(tmp_path):
    async def _run():
        store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
        binding = _binding()
        await store.save(_record(binding), expected_generation=0)
        # Simulate a crash between envelope write and index commit: orphan
        # envelope must not disturb the committed record.
        orphan = tmp_path / "oauth" / "grants" / f"{binding.credential_key}.orphan.enc"
        orphan.write_bytes(b"orphan")
        loaded = await store.load(binding)
        assert loaded is not None and loaded.access_token == SECRET
        # Symlink index refuses.
        index_path = tmp_path / "oauth" / "index.json"
        backup = index_path.read_bytes()
        index_path.unlink()
        index_path.symlink_to(tmp_path / "oauth" / "grants")
        with pytest.raises(ValueError):
            await store.load(binding)
        index_path.unlink()
        index_path.write_bytes(backup)
        # Hostile credential key rejected.
        with pytest.raises(ValueError):
            await store.load(_binding(credential_key="../../etc/passwd"))

    asyncio.run(_run())
    if os.name != "nt":
        mode = stat.S_IMODE((tmp_path / "oauth").stat().st_mode)
        assert mode == 0o700


def test_logout_tombstone_rejects_stale_writes_and_rollback(tmp_path):
    async def _run():
        store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
        binding = _binding()
        await store.save(_record(binding), expected_generation=0)
        new_gen = await store.forget(binding, expected_generation=1)
        assert new_gen == 2
        assert await store.load(binding) is None
        # Late writer with stale generation cannot resurrect.
        with pytest.raises(ValueError):
            await store.save(_record(binding, generation=2), expected_generation=1)
        # Generation rollback fails closed.
        with pytest.raises(ValueError):
            await store.forget(binding, expected_generation=0)

    asyncio.run(_run())


def test_key_sources_and_no_plaintext_fallback(tmp_path, monkeypatch):
    # Missing backend and no env → storage_unavailable.
    monkeypatch.delenv("AGENT_HARNESS_OAUTH_MASTER_KEY", raising=False)
    monkeypatch.setenv("AGENT_HARNESS_SECRET_BACKEND", "")
    import sys

    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(OAuthKeyUnavailable):
        oauth_keys.get_master_key()
    # Malformed supplied key does not fall back.
    monkeypatch.setenv("AGENT_HARNESS_OAUTH_MASTER_KEY", "not-base64!!")
    with pytest.raises(OAuthKeyUnavailable):
        oauth_keys.get_master_key()
    monkeypatch.setenv("AGENT_HARNESS_OAUTH_MASTER_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(OAuthKeyUnavailable):
        oauth_keys.get_master_key()
    # Valid supplied key resolves.
    monkeypatch.setenv("AGENT_HARNESS_OAUTH_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    assert oauth_keys.get_master_key() == b"k" * 32


def test_fake_keyring_backend_selection(tmp_path, monkeypatch):
    class _Backend:
        pass

    _Backend.__module__ = "keyring.backends.fail"

    class _Keyring:
        @staticmethod
        def get_keyring():
            return _Backend()

    import sys

    monkeypatch.setitem(sys.modules, "keyring", _Keyring)
    monkeypatch.delenv("AGENT_HARNESS_OAUTH_MASTER_KEY", raising=False)
    with pytest.raises(OAuthKeyUnavailable):
        oauth_keys.get_master_key()


def test_refresh_lock_cancellation_releases_file_and_local_locks(tmp_path, monkeypatch):
    import threading

    from ah.connectors import oauth_store

    async def run():
        store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
        binding = _binding()
        entered = threading.Event()
        release = threading.Event()
        original = oauth_store._file_lock_acquire
        captured = []

        def suspended(fd, *, timeout, cancelled=None):
            captured.append(fd)
            entered.set()
            release.wait(2)
            return original(fd, timeout=timeout, cancelled=cancelled)

        monkeypatch.setattr(oauth_store, "_file_lock_acquire", suspended)

        async def acquire():
            async with store.refresh_lock(binding, deadline=asyncio.get_running_loop().time() + 3):
                pytest.fail("cancelled lock entered body")

        task = asyncio.create_task(acquire())
        await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
        assert not store._async_locks[binding.credential_key].locked()
        with pytest.raises(OSError):
            os.fstat(captured[0])
        monkeypatch.setattr(oauth_store, "_file_lock_acquire", original)
        async with store.refresh_lock(binding, deadline=asyncio.get_running_loop().time() + 1):
            pass

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["backend", "read", "write"])
def test_keyring_timeout_returns_without_waiting_for_backend(monkeypatch, operation):
    import sys
    import threading
    import time

    class Backend:
        pass

    Backend.__module__ = "keyring.backends.macos"
    release = threading.Event()
    encoded = base64.b64encode(b"k" * 32).decode()

    class FakeKeyring:
        @staticmethod
        def get_keyring():
            if operation == "backend":
                release.wait(0.5)
            return Backend()

        @staticmethod
        def get_password(*args):
            if operation == "read":
                release.wait(0.5)
                return encoded
            return None

        @staticmethod
        def set_password(*args):
            release.wait(0.5)

    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring)
    monkeypatch.delenv("AGENT_HARNESS_OAUTH_MASTER_KEY", raising=False)
    started = time.monotonic()
    try:
        with pytest.raises(OAuthKeyUnavailable):
            oauth_keys.get_master_key(timeout=0.02)
        assert time.monotonic() - started < 0.2
    finally:
        release.set()


def test_keyring_timeout_does_not_hold_process_shutdown(tmp_path):
    import subprocess
    import sys
    import textwrap

    program = textwrap.dedent("""
        import sys, threading
        from ah.security.oauth_keys import get_master_key, OAuthKeyUnavailable
        class Backend:
            pass
        Backend.__module__ = "keyring.backends.macos"
        class FakeKeyring:
            @staticmethod
            def get_keyring():
                return Backend()
            @staticmethod
            def get_password(*args):
                threading.Event().wait()
        sys.modules["keyring"] = FakeKeyring
        try:
            get_master_key(timeout=0.02)
        except OAuthKeyUnavailable:
            print("timed out", flush=True)
    """)
    env = os.environ.copy()
    env.pop("AGENT_HARNESS_OAUTH_MASTER_KEY", None)
    result = subprocess.run(
        [sys.executable, "-c", program], env=env, capture_output=True, text=True, timeout=2
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "timed out"


def test_timed_out_key_creation_keeps_lock_until_backend_write_finishes(tmp_path, monkeypatch):
    import sys
    import threading

    class Backend:
        pass

    Backend.__module__ = "keyring.backends.macos"
    release = threading.Event()
    finished = threading.Event()
    writes = []
    stored = []

    class FakeKeyring:
        @staticmethod
        def get_keyring():
            return Backend()

        @staticmethod
        def get_password(*args):
            return stored[-1] if stored else None

        @staticmethod
        def set_password(service, account, value):
            writes.append(value)
            release.wait(1)
            stored.append(value)
            finished.set()

    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring)
    monkeypatch.delenv("AGENT_HARNESS_OAUTH_MASTER_KEY", raising=False)
    lock_path = tmp_path / "creation.lock"
    try:
        with pytest.raises(OAuthKeyUnavailable):
            oauth_keys.get_master_key(timeout=0.02, lock_path=lock_path)
        with pytest.raises(OAuthKeyUnavailable):
            oauth_keys.get_master_key(timeout=0.02, lock_path=lock_path)
        assert len(writes) == 1, "a pending creation may not overwrite a sibling master key"
    finally:
        release.set()
        assert finished.wait(1)
    assert oauth_keys.get_master_key(timeout=1, lock_path=lock_path) == base64.b64decode(stored[-1])


def test_failed_keyring_read_never_creates_replacement_key(monkeypatch):
    import sys

    class Backend:
        pass

    Backend.__module__ = "keyring.backends.macos"

    class FakeKeyring:
        @staticmethod
        def get_keyring():
            return Backend()

        @staticmethod
        def get_password(*args):
            raise RuntimeError("keyring temporarily locked")

        @staticmethod
        def set_password(*args):
            pytest.fail("unreadable keyring must not mint a replacement master key")

    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring)
    monkeypatch.delenv("AGENT_HARNESS_OAUTH_MASTER_KEY", raising=False)
    with pytest.raises(OAuthKeyUnavailable):
        oauth_keys.get_master_key(timeout=0.2)


def test_repeated_refresh_cancellation_waits_for_file_worker_cleanup(tmp_path, monkeypatch):
    import threading

    from ah.connectors import oauth_store

    async def run():
        store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
        binding = _binding()
        entered = threading.Event()
        release = threading.Event()
        captured = []

        def suspended(fd, *, timeout, cancelled=None):
            captured.append(fd)
            entered.set()
            release.wait(1)
            os.fstat(fd)
            return False

        monkeypatch.setattr(oauth_store, "_file_lock_acquire", suspended)

        async def acquire():
            async with store.refresh_lock(binding, deadline=asyncio.get_running_loop().time() + 2):
                pytest.fail("cancelled acquisition entered body")

        task = asyncio.create_task(acquire())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done(), (
                "file descriptor must remain owned until its acquisition worker exits"
            )
            os.fstat(captured[0])
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert not store._async_locks[binding.credential_key].locked()
        with pytest.raises(OSError):
            os.fstat(captured[0])

    asyncio.run(run())


def test_revision_filtered_retirement_preserves_newer_authority_atomically(tmp_path):
    async def run():
        store = OAuthCredentialStore(tmp_path / "oauth", key_provider=_key_provider)
        binding = _binding()
        await store.save(_record(binding), expected_generation=0)
        await store.save(
            _record(binding, config_revision=2, generation=2, grant_id="new-grant"),
            expected_generation=1,
        )
        generation = await store.forget(binding, expected_generation=1, max_config_revision=1)
        assert generation == 2
        record = await store.load(binding)
        assert record is not None and record.grant_id == "new-grant"

    asyncio.run(run())
