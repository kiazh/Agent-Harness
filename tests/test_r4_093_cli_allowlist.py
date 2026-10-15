"""R4-093 secrets-allowlist widening — E2E proof over the real handler.

RED reference (D1 evidence ``R4-080_082_083_gateway_config.md`` follow-up 2):
``secrets.set`` refused OPENCODE_API_KEY / CUSTOM_API_KEY /
CUSTOM_BASE_URL / KIMI_API_KEY with INVALID_PARAMS ("not a managed secret
key") while ``provider/specs.py`` consumes OPENCODE_API_KEY/KIMI_API_KEY,
``provider/factory.py`` consumes CUSTOM_BASE_URL/CUSTOM_API_KEY, and
``config.SECRET_KEYS``/``LEGACY_ENV_VARS`` carry them — so the keys were
unsettable from any gateway/UI surface.

These tests drive the REAL ``secrets_set``/``secrets_clear``/``secrets_list``
handlers with the REAL ``.env`` machinery (isolated ``MACHINA_ENV_FILE``).
Only the gateway object is stubbed (``require_db`` gate) — the fixed seam
(allowlist + env file) is fully real. DB: none needed.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from machina.gateway.errors import RpcError
from machina.gateway.features.secrets import secrets_clear, secrets_list, secrets_set


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / ".env"
    monkeypatch.setenv("MACHINA_ENV_FILE", str(target))
    for var in (
        "OPENCODE_API_KEY",
        "KIMI_API_KEY",
        "CUSTOM_API_KEY",
        "CUSTOM_BASE_URL",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return target


def _gw():
    return SimpleNamespace(require_db=lambda: None)


def _value(key: str) -> str:
    return f"test-value-for-{key.lower()}-0123456789"


# ─── the widened keys round-trip end to end ──────────────────────────────────


@pytest.mark.parametrize(
    "key", ["OPENCODE_API_KEY", "KIMI_API_KEY", "CUSTOM_API_KEY", "CUSTOM_BASE_URL"]
)
async def test_secrets_set_accepts_previously_refused_keys(env_file: Path, key: str) -> None:
    """The D1 RED probe, now GREEN: set persists to env AND .env, clear removes."""
    from machina.security.env_file import read_env_values

    value = _value(key)
    result = await secrets_set(_gw(), {"key": key, "value": value, "persist": True})
    assert result == {
        "key": key,
        "set": True,
        "persisted": True,
        "envFile": str(env_file),
    }
    assert os.environ.get(key) == value, "live process sees the key immediately"
    assert read_env_values(env_file).get(key) == value, "durable .env carries the key"

    cleared = await secrets_clear(_gw(), {"key": key})
    assert cleared == {"key": key, "set": False}
    assert key not in os.environ


async def test_secrets_set_session_only_leaves_dotenv_alone(env_file: Path) -> None:
    from machina.security.env_file import read_env_values

    key, value = "KIMI_API_KEY", _value("KIMI_API_KEY")
    result = await secrets_set(_gw(), {"key": key, "value": value, "persist": False})
    assert result["persisted"] is False
    assert os.environ.get(key) == value
    assert not env_file.is_file() or key not in read_env_values(env_file)


async def test_secrets_list_catalog_covers_widened_keys(env_file: Path) -> None:
    catalog = (await secrets_list(_gw(), {}))["secrets"]
    keys = {item["key"] for item in catalog}
    assert {
        "OPENCODE_API_KEY",
        "KIMI_API_KEY",
        "CUSTOM_API_KEY",
        "CUSTOM_BASE_URL",
        "OPENROUTER_API_KEY",
    } <= keys
    assert all("description" in item and "set" in item for item in catalog)


# ─── the wiring the gap broke: specs + config consume these names ────────────


def test_specs_and_config_consume_the_widened_keys() -> None:
    """Pins the D1 diagnosis: the allowlist must cover what the runtime reads."""
    from machina.core.config import LEGACY_ENV_VARS, SECRET_KEYS
    from machina.core.provider.specs import PROVIDER_SPECS

    assert PROVIDER_SPECS["opencode"].api_key_env == "OPENCODE_API_KEY"
    assert PROVIDER_SPECS["kimi"].api_key_env == "KIMI_API_KEY"
    assert SECRET_KEYS >= {"opencode_api_key", "kimi_api_key", "custom_api_key"}
    assert LEGACY_ENV_VARS["opencode_api_key"] == "OPENCODE_API_KEY"
    assert LEGACY_ENV_VARS["kimi_api_key"] == "KIMI_API_KEY"
    assert LEGACY_ENV_VARS["custom_api_key"] == "CUSTOM_API_KEY"
    assert LEGACY_ENV_VARS["custom_base_url"] == "CUSTOM_BASE_URL"
    # factory.py consumes CUSTOM_* for the dynamic custom provider.
    import inspect

    import machina.core.provider.factory as factory

    source = inspect.getsource(factory)
    assert "CUSTOM_BASE_URL" in source and "CUSTOM_API_KEY" in source


def test_allowlist_source_of_truth_stays_single() -> None:
    """PROVIDER_KEYS + MANAGED_ENV_KEYS feed the gateway allowlist; the UI
    mirror (keys.ts KNOWN_KEYS) must carry exactly the same key set."""
    import re

    from machina.gateway.features.secrets import MANAGED_ENV_KEYS
    from machina.security.env_file import PROVIDER_KEYS

    python_keys = {key for key, _ in (*PROVIDER_KEYS, *MANAGED_ENV_KEYS)}
    assert {
        "OPENCODE_API_KEY",
        "KIMI_API_KEY",
        "CUSTOM_API_KEY",
        "CUSTOM_BASE_URL",
    } <= python_keys
    text = (Path(__file__).resolve().parents[1] / "ui" / "src" / "features" / "keys.ts").read_text(
        encoding="utf-8"
    )
    ts_keys = set(re.findall(r'\["([A-Z][A-Z0-9_]+)",', text))
    assert python_keys <= ts_keys, f"keys.ts is missing: {sorted(python_keys - ts_keys)}"


async def test_control_plane_keys_still_refused(env_file: Path) -> None:
    """Widening must not open the control plane: PATH etc. still refused."""
    with pytest.raises(RpcError):
        await secrets_set(_gw(), {"key": "PATH", "value": "x" * 16, "persist": False})
    with pytest.raises(RpcError):
        await secrets_set(
            _gw(),
            {"key": "MACHINA_GATEWAY_TOKEN", "value": "x" * 16, "persist": False},
        )
