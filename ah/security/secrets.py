"""Resolve deployment secrets from environment, mounted files, Vault, or AWS."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import httpx

_CACHE_SECONDS = 60
_cache: dict[tuple[str, str, str], tuple[float, str]] = {}
_lock = threading.Lock()


def _read_file(path: str) -> str:
    source = Path(path)
    if not source.is_file():
        raise ValueError("secret path must be a file")
    if source.stat().st_size > 8192:
        raise ValueError("secret file is too large")
    return source.read_text(encoding="utf-8").strip()


def _external_secret(name: str, backend: str) -> str:
    if backend == "vault":
        address = os.environ.get("AGENT_HARNESS_VAULT_ADDR", "").rstrip("/")
        path = os.environ.get("AGENT_HARNESS_VAULT_PATH", "").lstrip("/")
        token = os.environ.get("AGENT_HARNESS_VAULT_TOKEN", "")
        if not token and os.environ.get("AGENT_HARNESS_VAULT_TOKEN_FILE"):
            token = _read_file(os.environ["AGENT_HARNESS_VAULT_TOKEN_FILE"])
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


def get_secret(name: str) -> str | None:
    """Resolve a named secret without caching environment or file values."""
    direct = os.environ.get(name, "").strip()
    if direct:
        return direct
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
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
        source = f"{os.environ.get('AGENT_HARNESS_VAULT_ADDR', '')}/{source}"
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
