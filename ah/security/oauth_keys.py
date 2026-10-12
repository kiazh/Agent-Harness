"""OAuth master-key source: operator secret first, OS keyring second.

Never falls back to plaintext JSON/.env keys. Fake providers are injected
in tests; no live keyring access happens in automated verification.
"""

from __future__ import annotations

import base64
import os
import secrets as _secrets

MASTER_KEY_ENV = "AGENT_HARNESS_OAUTH_MASTER_KEY"
KEYRING_SERVICE = "AgentHarness/MCP-OAuth"
KEYRING_ACCOUNT = "master-key-v1"

_ALLOWED_BACKENDS = ("macos", "windows", "secretservice", "kwallet", "libsecret")


class OAuthKeyUnavailable(Exception):
    """No usable secure master key; caller reports storage_unavailable."""


def _decode_supplied(raw: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except Exception as exc:
        raise OAuthKeyUnavailable("oauth master key is malformed") from exc
    if len(key) != 32:
        raise OAuthKeyUnavailable("oauth master key must decode to exactly 32 bytes")
    return key


def _keyring_backend_usable(keyring_mod) -> bool:
    try:
        backend = keyring_mod.get_keyring()
    except Exception:
        return False
    name = type(backend).__module__ + "." + type(backend).__name__
    lowered = name.lower()
    if "fail" in lowered or "null" in lowered:
        return False
    if os.environ.get("AGENT_HARNESS_OAUTH_ALLOW_PLAINTEXT_KEYRING") == "1":
        return "plaintext" not in lowered
    if "plaintext" in lowered or "plain" in lowered:
        return False
    return (
        any(token in lowered for token in _ALLOWED_BACKENDS)
        or "keyring" in lowered
        and (os.name == "posix" or os.name == "nt")
    )


def get_master_key(*, key_provider=None, timeout: float = 5.0, lock_path=None) -> bytes:
    """Resolve the 32-byte AES master key.

    Priority: explicit key_provider (tests) → operator env secret →
    OS keyring. Raises OAuthKeyUnavailable on absence/failure.
    Callers must invoke this from a worker thread (store uses to_thread);
    keyring calls are bounded by *timeout* and never hang an event loop.
    When *lock_path* is given, key creation is serialized across processes
    so racing workers cannot mint conflicting keys.
    """
    if key_provider is not None:
        key = key_provider()
        if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
            raise OAuthKeyUnavailable("oauth key provider must return 32 bytes")
        return bytes(key)
    supplied = os.environ.get(MASTER_KEY_ENV, "").strip()
    if supplied:
        from ah.security.secrets import get_secret

        resolved = get_secret(MASTER_KEY_ENV)
        if not resolved:
            raise OAuthKeyUnavailable("oauth master key is configured but unreadable")
        return _decode_supplied(resolved)
    # Distinguish missing (fall through to keyring) from malformed (fail above).
    try:
        import keyring as keyring_mod
    except ImportError as exc:
        raise OAuthKeyUnavailable(
            "no oauth master key: set AGENT_HARNESS_OAUTH_MASTER_KEY or install an OS keyring backend"
        ) from exc
    if not _keyring_backend_usable(keyring_mod):
        raise OAuthKeyUnavailable("no secure OS keyring backend available for oauth master key")
    import concurrent.futures

    def _read() -> str | None:
        try:
            return keyring_mod.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:
            return None

    def _read_bounded() -> str | None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_read)
            try:
                return future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                raise OAuthKeyUnavailable("os keyring is locked or unavailable") from None

    stored = _read_bounded()
    if stored:
        return _decode_supplied(stored)
    # Creation races across processes: serialize on lock_path when given,
    # then re-read (a sibling worker may have just created the key).
    if lock_path is not None:
        from pathlib import Path as _Path

        _acquire_creation_lock(_Path(lock_path), timeout=timeout)
        try:
            stored = _read_bounded()
            if stored:
                return _decode_supplied(stored)
            return _create_key(keyring_mod, timeout=timeout)
        finally:
            _release_creation_lock()
    return _create_key(keyring_mod, timeout=timeout)


_creation_lock_fd: int | None = None


def _acquire_creation_lock(path, *, timeout: float) -> None:
    global _creation_lock_fd
    import os as _os
    import time as _time

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = _os.open(str(path), _os.O_CREAT | _os.O_RDWR, 0o600)
    deadline = _time.monotonic() + max(0.0, timeout)
    try:
        if _os.name == "nt":  # pragma: no cover
            import msvcrt as _msvcrt

            if _os.fstat(fd).st_size == 0:
                _os.write(fd, b"\0")
            _os.lseek(fd, 0, _os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)
        else:
            import fcntl as _fcntl

            while True:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    break
                except (OSError, BlockingIOError):
                    if _time.monotonic() >= deadline:
                        raise OAuthKeyUnavailable(
                            "oauth master-key creation lock timed out"
                        ) from None
                    _time.sleep(0.01)
        _creation_lock_fd = fd
    except BaseException:
        _os.close(fd)
        raise


def _release_creation_lock() -> None:
    global _creation_lock_fd
    import os as _os

    fd, _creation_lock_fd = _creation_lock_fd, None
    if fd is None:
        return
    try:
        if _os.name == "nt":  # pragma: no cover
            import msvcrt as _msvcrt

            _os.lseek(fd, 0, _os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        else:
            import fcntl as _fcntl

            _fcntl.flock(fd, _fcntl.LOCK_UN)
    finally:
        _os.close(fd)


def _create_key(keyring_mod, *, timeout: float) -> bytes:
    import concurrent.futures

    fresh = _secrets.token_bytes(32)
    encoded = base64.b64encode(fresh).decode()

    def _write() -> None:
        keyring_mod.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, encoded)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_write)
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            raise OAuthKeyUnavailable("os keyring write timed out") from exc
        except Exception as exc:
            raise OAuthKeyUnavailable("os keyring is locked or unavailable") from exc
    return fresh


def encode_key_for_tests(key: bytes) -> str:
    return base64.b64encode(bytes(key)).decode()
