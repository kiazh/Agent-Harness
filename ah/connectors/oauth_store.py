"""Encrypted OAuth credential store: atomic index pointer + immutable envelopes.

Layout (root never supplied by RPC):
    <root>/index.json
    <root>/index.lock
    <root>/grants/<sha256>. <writeid>.enc
    <root>/locks/<sha256>.lock

AES-GCM with a 32-byte master key, fresh 12-byte nonce per write, AAD
binding record version + credential key. Index commits are compare-and-swap
on generation; tombstones defeat late writers.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from ah.connectors.oauth_models import (
    OAUTH_GRANT_FILE_MAX_BYTES,
    OAUTH_INDEX_MAX_BYTES,
    OAUTH_LOCK_SECONDS,
    OAuthBinding,
    OAuthCredentialRecord,
)

_WINDOWS = os.name == "nt"
if _WINDOWS:  # pragma: no cover - platform branch
    import msvcrt
else:
    import fcntl

INDEX_VERSION = 1
RECORD_VERSION = 1


def credential_key_for(
    *, connector_id: str, resource: str, issuer: str, client_id: str, revision: int = 0
) -> str:
    canonical = "\x00".join([connector_id, resource, issuer, client_id, str(revision)])
    return hashlib.sha256(canonical.encode()).hexdigest()


def _safe_name(key: str) -> str:
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("invalid credential key")
    return key


def _check_secret_file(path: Path, *, max_bytes: int) -> None:
    try:
        st = path.lstat()
    except OSError as exc:
        raise ValueError("credential file unreadable") from exc
    import stat as _stat

    if not _stat.S_ISREG(st.st_mode):
        raise ValueError("credential path must be a regular file")
    if st.st_size > max_bytes:
        raise ValueError("credential file exceeds size limit")
    if os.name != "nt" and (st.st_mode & 0o077):
        raise ValueError("credential file must not be group/world accessible")


def _ensure_dirs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
    for sub in ("grants", "locks"):
        path = root / sub
        path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".oauth-")
    try:
        with os.fdopen(fd, "wb") as handle:
            if os.name != "nt":
                try:
                    os.fchmod(handle.fileno(), mode)
                except OSError:
                    pass
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            try:
                dfd = os.open(str(path.parent), os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _lock_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)


def _file_lock_acquire(fd: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if _WINDOWS:  # pragma: no cover
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, BlockingIOError):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)


def _file_lock_release(fd: int) -> None:
    try:
        if _WINDOWS:  # pragma: no cover
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class OAuthCredentialStore:
    def __init__(self, root: Path | None = None, *, key_provider=None) -> None:
        self.root = Path(root) if root is not None else Path.home() / ".agent-harness" / "oauth"
        self._key_provider = key_provider
        self._master_key: bytes | None = None
        self._key_lock = asyncio.Lock()
        self._async_locks: dict[str, asyncio.Lock] = {}
        self._async_lock_guard = asyncio.Lock()

    def _index_path(self) -> Path:
        return self.root / "index.json"

    def _lock_path(self) -> Path:
        return self.root / "index.lock"

    # ── master key ────────────────────────────────────────────────
    async def _master(self) -> bytes:
        if self._master_key is not None:
            return self._master_key
        async with self._key_lock:
            if self._master_key is not None:
                return self._master_key
            from ah.security.oauth_keys import OAuthKeyUnavailable, get_master_key

            try:
                key = await asyncio.to_thread(
                    get_master_key,
                    key_provider=self._key_provider,
                    lock_path=self._lock_path(),
                )
            except Exception as exc:
                raise OAuthKeyUnavailable(str(exc)) from exc
            self._master_key = key
            return key

    # ── crypto ────────────────────────────────────────────────────
    @staticmethod
    def _aad(credential_key: str) -> bytes:
        return f"ah-oauth-v{RECORD_VERSION}\x00{credential_key}".encode()

    async def _encrypt(self, record: OAuthCredentialRecord) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = await self._master()
        payload = {
            "version": RECORD_VERSION,
            "credential_key": record.credential_key,
            "connector_id": record.connector_id,
            "config_revision": record.config_revision,
            "resource": record.resource,
            "issuer": record.issuer,
            "client_id": record.client_id,
            "client_secret_ref": record.client_secret_ref,
            "authorization_endpoint": record.authorization_endpoint,
            "token_endpoint": record.token_endpoint,
            "revocation_endpoint": record.revocation_endpoint,
            "redirect_uri_used": record.redirect_uri_used,
            "token_endpoint_auth_method": record.token_endpoint_auth_method,
            "grant_id": record.grant_id,
            "generation": record.generation,
            "approved_scopes": list(record.approved_scopes),
            "granted_scopes": list(record.granted_scopes),
            "access_token": record.access_token,
            "refresh_token": record.refresh_token,
            "acquired_at_utc": record.acquired_at_utc,
            "expires_at_utc": record.expires_at_utc,
            "refresh_state": record.refresh_state,
        }
        plaintext = json.dumps(payload, sort_keys=True).encode()
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, self._aad(record.credential_key))
        envelope = {
            "version": RECORD_VERSION,
            "credential_key": record.credential_key,
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }
        return json.dumps(envelope, sort_keys=True).encode()

    async def _decrypt(self, raw: bytes, *, expected_key: str) -> OAuthCredentialRecord:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = await self._master()
        try:
            envelope = json.loads(raw.decode())
            nonce = base64.b64decode(envelope["nonce"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
            if envelope.get("credential_key") != expected_key:
                raise ValueError("credential binding mismatch")
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, self._aad(expected_key))
            data = json.loads(plaintext.decode())
        except InvalidTag as exc:
            raise ValueError("credential decryption failed") from exc
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("credential envelope invalid") from exc
        if data.get("credential_key") != expected_key:
            raise ValueError("credential binding mismatch")
        return OAuthCredentialRecord(
            version=int(data.get("version", 1)),
            credential_key=data.get("credential_key", ""),
            connector_id=data.get("connector_id", ""),
            config_revision=int(data.get("config_revision", 0)),
            resource=data.get("resource", ""),
            issuer=data.get("issuer", ""),
            client_id=data.get("client_id", ""),
            client_secret_ref=data.get("client_secret_ref", ""),
            authorization_endpoint=data.get("authorization_endpoint", ""),
            token_endpoint=data.get("token_endpoint", ""),
            revocation_endpoint=data.get("revocation_endpoint", ""),
            redirect_uri_used=data.get("redirect_uri_used", ""),
            token_endpoint_auth_method=data.get("token_endpoint_auth_method", "none"),
            grant_id=data.get("grant_id", ""),
            generation=int(data.get("generation", 0)),
            approved_scopes=tuple(data.get("approved_scopes", [])),
            granted_scopes=tuple(data.get("granted_scopes", [])),
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            acquired_at_utc=data.get("acquired_at_utc", ""),
            expires_at_utc=data.get("expires_at_utc", ""),
            refresh_state=data.get("refresh_state", "idle"),
        )

    # ── index ─────────────────────────────────────────────────────
    def _read_index(self) -> dict[str, Any]:
        path = self._index_path()
        if not path.exists():
            return {"version": INDEX_VERSION, "grants": {}, "tombstones": {}}
        _check_secret_file(path, max_bytes=OAUTH_INDEX_MAX_BYTES)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError("oauth index unreadable") from exc
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            raise ValueError("oauth index has unsupported format")
        grants = data.get("grants", {})
        tombstones = data.get("tombstones", {})
        if not isinstance(grants, dict) or not isinstance(tombstones, dict):
            raise ValueError("oauth index is corrupt")
        return {"version": INDEX_VERSION, "grants": grants, "tombstones": tombstones}

    def _write_index(self, index: dict[str, Any]) -> None:
        raw = json.dumps(index, indent=2, sort_keys=True).encode()
        if len(raw) > OAUTH_INDEX_MAX_BYTES:
            raise ValueError("oauth index exceeds size limit")
        _atomic_write(self._index_path(), raw)

    # ── public API ────────────────────────────────────────────────
    async def load(self, binding: OAuthBinding) -> OAuthCredentialRecord | None:
        key = _safe_name(binding.credential_key)
        await self._master()
        index = await asyncio.to_thread(self._read_index)
        entry = index["grants"].get(key)
        if entry is None:
            return None
        envelope_name = entry.get("envelope")
        if not isinstance(envelope_name, str) or "/" in envelope_name:
            raise ValueError("oauth index references an invalid envelope")
        envelope_path = self.root / "grants" / envelope_name
        _check_secret_file(envelope_path, max_bytes=OAUTH_GRANT_FILE_MAX_BYTES)
        raw = await asyncio.to_thread(envelope_path.read_bytes)
        return await self._decrypt(raw, expected_key=key)

    async def public_identity(self, binding: OAuthBinding) -> dict[str, Any] | None:
        record = await self.load(binding)
        if record is None:
            return None
        return {
            "credentialKey": record.credential_key,
            "grantId": record.grant_id,
            "generation": record.generation,
            "resource": record.resource,
            "issuer": record.issuer,
            "clientId": record.client_id,
            "grantedScopes": list(record.granted_scopes),
        }

    async def save(
        self, record: OAuthCredentialRecord, *, expected_generation: int
    ) -> OAuthCredentialRecord:
        key = _safe_name(record.credential_key)
        _ensure_dirs(self.root)
        await self._master()
        envelope_bytes = await self._encrypt(record)
        if len(envelope_bytes) > OAUTH_GRANT_FILE_MAX_BYTES:
            raise ValueError("encrypted grant exceeds size limit")
        write_id = secrets.token_hex(8)
        envelope_name = f"{key}.{write_id}.enc"
        envelope_path = self.root / "grants" / envelope_name
        await asyncio.to_thread(_atomic_write, envelope_path, envelope_bytes)

        def _commit() -> OAuthCredentialRecord:
            fd = _lock_file(self._lock_path())
            try:
                if not _file_lock_acquire(fd, timeout=OAUTH_LOCK_SECONDS):
                    raise TimeoutError("oauth store lock acquisition timed out")
                try:
                    index = self._read_index()
                    current = index["grants"].get(key)
                    current_gen = int(current.get("generation", 0)) if current else 0
                    tombstone_gen = int(index["tombstones"].get(key, 0))
                    floor = max(current_gen, tombstone_gen)
                    if expected_generation != floor:
                        raise ValueError("oauth generation mismatch; stale writer")
                    if record.generation != floor + 1 and not (
                        current is not None and record.generation == current_gen
                    ):
                        # New authority must advance generation; same-generation
                        # rotation (token bytes only) is permitted.
                        if record.generation <= floor:
                            raise ValueError("oauth generation must advance")
                    index["grants"][key] = {
                        "envelope": envelope_name,
                        "generation": record.generation,
                        "grant_id": record.grant_id,
                        "connector_id": record.connector_id,
                    }
                    self._write_index(index)
                    # GC: remove superseded envelopes for this key.
                    if current and current.get("envelope") != envelope_name:
                        old = self.root / "grants" / str(current["envelope"])
                        try:
                            old.unlink()
                        except OSError:
                            pass
                    return record
                finally:
                    _file_lock_release(fd)
            except BaseException:
                try:
                    envelope_path.unlink()
                except OSError:
                    pass
                raise

        committed = await asyncio.to_thread(_commit)
        # Defense in depth: report failure loudly; caller marks flow failed.
        return committed

    async def forget(self, binding: OAuthBinding, *, expected_generation: int) -> int:
        key = _safe_name(binding.credential_key)
        _ensure_dirs(self.root)
        await self._master()

        def _commit() -> int:
            fd = _lock_file(self._lock_path())
            try:
                if not _file_lock_acquire(fd, timeout=OAUTH_LOCK_SECONDS):
                    raise TimeoutError("oauth store lock acquisition timed out")
                try:
                    index = self._read_index()
                    current = index["grants"].get(key)
                    current_gen = int(current.get("generation", 0)) if current else 0
                    tombstone_gen = int(index["tombstones"].get(key, 0))
                    floor = max(current_gen, tombstone_gen)
                    if expected_generation != floor:
                        raise ValueError("oauth generation mismatch; stale forget")
                    new_gen = floor + 1
                    if current is not None:
                        old = self.root / "grants" / str(current.get("envelope", ""))
                        try:
                            old.unlink()
                        except OSError:
                            pass
                        del index["grants"][key]
                    index["tombstones"][key] = new_gen
                    self._write_index(index)
                    return new_gen
                finally:
                    _file_lock_release(fd)
            except BaseException:
                raise

        return await asyncio.to_thread(_commit)

    async def current_generation(self, binding: OAuthBinding) -> int:
        key = _safe_name(binding.credential_key)
        index = await asyncio.to_thread(self._read_index)
        current = index["grants"].get(key)
        current_gen = int(current.get("generation", 0)) if current else 0
        tombstone_gen = int(index["tombstones"].get(key, 0))
        return max(current_gen, tombstone_gen)

    @asynccontextmanager
    async def refresh_lock(self, binding: OAuthBinding, *, deadline: float):
        key = _safe_name(binding.credential_key)
        async with self._async_lock_guard:
            lock = self._async_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._async_locks[key] = lock
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("oauth refresh lock deadline already expired")
        try:
            await asyncio.wait_for(lock.acquire(), timeout=min(remaining, OAUTH_LOCK_SECONDS))
        except TimeoutError as exc:
            raise TimeoutError("oauth refresh lock acquisition timed out") from exc
        lock_path = self.root / "locks" / f"{key}.lock"
        fd = await asyncio.to_thread(_lock_file, lock_path)
        acquired = await asyncio.to_thread(
            _file_lock_acquire, fd, timeout=min(remaining, OAUTH_LOCK_SECONDS)
        )
        if not acquired:
            lock.release()
            await asyncio.to_thread(os.close, fd)
            raise TimeoutError("oauth cross-process refresh lock timed out")
        try:
            yield
        finally:
            await asyncio.to_thread(_file_lock_release, fd)
            lock.release()

    async def mark_refresh_state(self, binding: OAuthBinding, state: str) -> None:
        record = await self.load(binding)
        if record is None:
            return
        record.refresh_state = state
        await self.save(record, expected_generation=record.generation)
