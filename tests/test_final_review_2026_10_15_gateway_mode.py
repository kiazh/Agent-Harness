"""Evidence tests — final review 2026-10-15 gateway mode env-shadow + durability.

F1 (P2): ``Config.get`` reads ``MACHINA_EXECUTION_MODE`` (env / ``.env``)
*before* the in-memory attribute. The gateway global ``mode.set`` /
``mode.revoke`` / generic ``config.set`` / ``_initialize --mode`` paths only
called ``Config.set``, so a ``MACHINA_EXECUTION_MODE`` shadow — exactly the
line ``machina mode X`` writes into ``.env`` — silently won. A reported
full→ask downgrade returned ``{"mode": "ask"}`` while ``config.get`` stayed
``full`` and ``grant_full_default`` kept granting full authority.

F6 (P3): global ``mode.revoke`` reset ``ask`` in memory only, so a restart
re-read the old default; the durable reset was not persisted.

These tests assert on the *effective* value (``config.get`` after the call),
not the echoed result. DB: none. ``.env`` and ``config.yaml`` writes are
isolated to ``tmp_path``. RED before the fix, GREEN after.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
import yaml

from machina.core.config import config
from machina.gateway.features import mode as modefeat


class _Gw:
    """DB-free gateway stand-in: session lookup + require_db no-op."""

    def require_db(self) -> None:
        pass

    async def get_session(self, params):
        return SimpleNamespace(id=uuid.UUID(params["sessionId"]), agent_id="h")


@pytest.fixture
def isolated_mode_env(tmp_path, monkeypatch):
    """Pre-existing ``full`` shadow, isolated config.yaml and .env paths.

    Mirrors the real failure input: ``machina mode full`` left
    ``MACHINA_EXECUTION_MODE=full`` in the environment/``.env`` before the
    gateway downgrade arrives. The autouse conftest fixture restores the live
    environment and in-memory config afterwards.
    """
    import machina.core.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "DEFAULT_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setenv("MACHINA_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("MACHINA_EXECUTION_MODE", "full")
    config.set("execution_mode", "full")
    return tmp_path


# ─── F1: a global set must beat the pre-existing env shadow ─────────────────


async def test_global_mode_set_beats_preexisting_env_shadow(isolated_mode_env):
    """RED (F1): returns the request but ``config.get`` stayed the old shadow."""
    gw = _Gw()
    result = await modefeat.mode_set(gw, {"mode": "ask", "scope": "global"})

    assert result["mode"] == "ask"
    # The effective value is what grant_full_default / every reader sees.
    assert config.get("execution_mode") == "ask"
    assert os.environ["MACHINA_EXECUTION_MODE"] == "ask"


async def test_global_mode_set_persist_writes_env_shadow(isolated_mode_env):
    """RED (F1, durable): persist mirrors the shadow into the isolated .env."""
    from machina.security.env_file import read_env_values

    gw = _Gw()
    await modefeat.mode_set(gw, {"mode": "ask", "scope": "global", "persist": True})

    assert config.get("execution_mode") == "ask"
    assert read_env_values()["MACHINA_EXECUTION_MODE"] == "ask"


async def test_generic_config_set_execution_mode_beats_env_shadow(isolated_mode_env):
    """RED (F1): the generic ``config.set`` bridge had the same shadow bug."""
    from machina.gateway.server import Gateway

    gw = Gateway(lambda _frame: None, owns_db=False)
    await gw._config_set({"key": "execution_mode", "value": "ask"})

    assert config.get("execution_mode") == "ask"
    assert os.environ["MACHINA_EXECUTION_MODE"] == "ask"


async def test_initialize_mode_beats_env_shadow(isolated_mode_env, monkeypatch):
    """RED (F1): ``_initialize --mode`` is process-scoped but must take effect.

    No ``.env`` write here: the launcher's ``--mode`` is documented as applied
    to this gateway process only (never persisted).
    """
    from machina.gateway.server import Gateway

    monkeypatch.delenv("MACHINA_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr("machina.plugins.loader.load_plugins", lambda: None, raising=False)
    gw = Gateway(lambda _frame: None, owns_db=False)
    gw._db_ready = True

    await gw._initialize({"mode": "ask"})

    assert config.get("execution_mode") == "ask"
    from machina.security.env_file import read_env_values

    assert "MACHINA_EXECUTION_MODE" not in read_env_values()


# ─── F6: the global revoke must be durable (persist=True) ───────────────────


async def test_global_mode_revoke_is_durable(isolated_mode_env, monkeypatch):
    """RED (F6): global revoke reset in memory only; restart resurrected full."""
    from machina.permissions.broker import permission_broker
    from machina.security.env_file import read_env_values

    # Force the DB-free branch (this suite runs without a database).
    monkeypatch.setattr("machina.db.connection.db", SimpleNamespace(connected=False))

    async def fake_revoke(_session_id):
        return 3

    monkeypatch.setattr(permission_broker, "revoke", fake_revoke)

    gw = _Gw()
    sid = str(uuid.uuid4())
    result = await modefeat.mode_revoke(gw, {"sessionId": sid, "scope": "global"})

    assert result == {"revoked": 3, "mode": "ask", "scope": "global"}
    assert config.get("execution_mode") == "ask"
    # Durable: YAML default and .env shadow both reset.
    saved = yaml.safe_load((isolated_mode_env / "config.yaml").read_text())
    assert saved["execution_mode"] == "ask"
    assert read_env_values()["MACHINA_EXECUTION_MODE"] == "ask"
