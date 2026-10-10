"""Resolve deployment secrets from environment, mounted files, Vault, or AWS."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from pathlib import Path
from urllib.parse import quote

import httpx

_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,64}$")

_CACHE_SECONDS = 60
_cache: dict[tuple[str, str, str], tuple[float, str]] = {}
_lock = threading.Lock()
_redaction_values: set[str] = set()
_redaction_lock = threading.Lock()


def _read_file(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("secret path must be a file")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        try:
            fd = os.open(path, os.O_RDONLY | nofollow)
        except OSError as e:
            raise ValueError("secret path must be a file") from e
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("secret path must be a file")
            if st.st_size > 8192:
                raise ValueError("secret file is too large")
            data = b""
            while len(data) <= 8192:
                chunk = os.read(fd, 8193 - len(data))
                if not chunk:
                    break
                data += chunk
            if len(data) > 8192:
                raise ValueError("secret file is too large")
            return data.decode("utf-8").strip()
        finally:
            os.close(fd)
    else:
        # Windows fallback (no O_NOFOLLOW): reject symlinks explicitly.
        source = Path(path)
        try:
            if source.is_symlink() or not source.is_file():
                raise ValueError("secret path must be a file")
            if source.stat().st_size > 8192:
                raise ValueError("secret file is too large")
            data = source.read_bytes()[:8193]
        except OSError as e:
            raise ValueError("secret path must be a file") from e
        if len(data) > 8192:
            raise ValueError("secret file is too large")
        return data.decode("utf-8").strip()


def _external_secret(name: str, backend: str) -> str:
    if not _SECRET_NAME_RE.match(name or ""):
        raise ValueError(f"invalid secret name {name!r}")
    if backend == "vault":
        address = os.environ.get("AGENT_HARNESS_VAULT_ADDR", "").rstrip("/")
        raw_path = os.environ.get("AGENT_HARNESS_VAULT_PATH", "").lstrip("/")
        path = quote(raw_path, safe="/")
        token = os.environ.get("AGENT_HARNESS_VAULT_TOKEN", "")
        if not token and (os.environ.get("AGENT_HARNESS_VAULT_TOKEN_FILE") or "").strip():
            token = _read_file(os.environ["AGENT_HARNESS_VAULT_TOKEN_FILE"].strip())
        if not address or not path or not token:
            raise ValueError("Vault address, path, and token must be configured")
        if not address.startswith("https://"):
            raise ValueError("Vault address must use HTTPS")
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{address}/v1/{path}", headers={"X-Vault-Token": token})
            response.raise_for_status()
            data = response.json().get("data", {})
        values = data.get("data", data)  # KV v2 or KV v1
    elif backend == "aws":
        secret_id = os.environ.get("AGENT_HARNESS_AWS_SECRET_ID", "")
        if not secret_id:
            raise ValueError("AGENT_HARNESS_AWS_SECRET_ID must be configured")
        try:
            import boto3
        except ImportError:
            raise RuntimeError("install agent-harness[secrets] for AWS Secrets Manager") from None
        response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)
        values = json.loads(response["SecretString"])
    else:
        raise ValueError("secret backend must be 'vault' or 'aws'")
    value = values.get(name) if isinstance(values, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"secret {name} is missing from {backend}")
    return value


def _resolve_secret(name: str, *, env_names: tuple[str, ...] | None = None) -> str | None:
    """Resolve a named secret without caching environment or file values."""
    if not _SECRET_NAME_RE.match(name or ""):
        raise ValueError(f"invalid secret name {name!r}")
    names = env_names or (name,)
    for env_name in names:
        if not _SECRET_NAME_RE.fullmatch(env_name):
            raise ValueError("invalid secret environment name")
        direct = os.environ.get(env_name, "").strip()
        if direct:
            return direct
    for env_name in names:
        file_path = (os.environ.get(f"{env_name}_FILE") or "").strip()
        if file_path:
            # An explicitly selected mounted file never falls through.
            return _read_file(file_path) or None
    backend = os.environ.get("AGENT_HARNESS_SECRET_BACKEND", "").lower()
    if not backend:
        return None
    source = (
        os.environ.get("AGENT_HARNESS_VAULT_PATH", "")
        if backend == "vault"
        else os.environ.get("AGENT_HARNESS_AWS_SECRET_ID", "")
    )
    if backend == "vault":
        source = f"{os.environ.get('AGENT_HARNESS_VAULT_ADDR', '').rstrip('/')}/{source}"
    cache_key = (backend, source, name)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(cache_key)
        if cached and now - cached[0] < _CACHE_SECONDS:
            return cached[1]
    value = _external_secret(name, backend)
    with _lock:
        _cache[cache_key] = (now, value)
    return value


def get_secret(name: str, *, env_names: tuple[str, ...] | None = None) -> str | None:
    """Resolve a credential and remember its exact bytes for outbound redaction.

    Retain rotated credentials for the process lifetime: an older provider may
    still be retiring and echoing its original credential.
    """
    value = _resolve_secret(name, env_names=env_names)
    if value:
        with _redaction_lock:
            _redaction_values.add(value)
    return value


def known_secret_values() -> tuple[str, ...]:
    """Snapshot credentials without backend access, logging, or file reads."""
    with _redaction_lock:
        values = set(_redaction_values)
    for name, value in os.environ.copy().items():
        if value and (
            name.endswith(("_API_KEY", "_TOKEN", "_PASSWORD"))
            or name in {"AGENT_HARNESS_API_KEY", "AGENT_HARNESS_PROVENANCE_KEY"}
        ):
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


def invalidate_secret_cache() -> None:
    """Forget backend values after an administrator rotation."""
    with _lock:
        _cache.clear()
