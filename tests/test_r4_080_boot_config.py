"""R4-080 boot/config cluster — per-bullet RED-first proofs.

* (a) a non-mapping YAML root warns and keeps defaults instead of crashing
  every CLI command (``Config.load`` guard).
* (b) the runtime-schema recovery message names ``machina init`` — the
  command that exists (``machina db init`` does not).
* (c) overlapping first connects create exactly one pool against the real
  ``machina_test`` database (a second pool would leak its backends).
* (d) ``run_bounded`` returns its typed spawn-error result for NUL-byte argv
  (the "spawning never raises" contract).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DSN = os.environ.get("MACHINA_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not TEST_DSN, reason="MACHINA_TEST_DATABASE_URL not set")


# ─── (a) non-mapping YAML root ──────────────────────────────────────────────


def test_config_load_list_root_warns_and_keeps_defaults(tmp_path, caplog):
    from machina.core.config import Config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="machina.core.config"):
        loaded = Config.load(str(cfg_file))
    assert loaded.model == "openrouter/free"
    assert loaded.provider == "openrouter"
    assert loaded.execution_mode == "ask"
    assert any(
        "Ignoring config file" in record.getMessage() and "list" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_config_load_scalar_root_warns_and_keeps_defaults(tmp_path, caplog):
    from machina.core.config import Config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("just a scalar\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="machina.core.config"):
        loaded = Config.load(str(cfg_file))
    assert loaded.model == "openrouter/free"
    assert any("Ignoring config file" in r.getMessage() for r in caplog.records)


def test_cli_runs_with_a_non_mapping_config_root(tmp_path):
    """E2E: every CLI command reads the global config; a list root must not crash it."""
    home = tmp_path / "home"
    (home / ".machina").mkdir(parents=True)
    (home / ".machina" / "config.yaml").write_text("[1, 2, 3]\n", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONIOENCODING": "utf-8",
        "MACHINA_ENV_FILE": str(tmp_path / ".env"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "machina", "config"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "Traceback" not in proc.stderr
    assert "openrouter" in proc.stdout


# ─── (b) runtime-schema recovery message ────────────────────────────────────


def test_chat_runtime_schema_failure_names_machina_init(monkeypatch):
    from typer.testing import CliRunner

    import machina.cli as cli_module
    from machina.db.connection import db

    async def _connect_ok() -> None:
        return None

    async def _schema_fails() -> None:
        raise RuntimeError("simulated runtime schema failure")

    monkeypatch.setattr(db, "connect", _connect_ok)
    monkeypatch.setattr(db, "initialize_runtime_schema", _schema_fails)
    result = CliRunner().invoke(cli_module.app, ["chat", "hello"])
    assert result.exit_code == 1
    assert "Run machina init and retry." in result.output
    assert "machina db init" not in result.output


# ─── (c) overlapping first connects: exactly one pool ───────────────────────


@needs_db
async def test_overlapping_first_connects_create_exactly_one_pool(monkeypatch):
    from machina.db import connection as conn_mod

    # One backend per pool keeps the leak count exact.
    monkeypatch.setenv("MACHINA_DB_MIN_POOL", "1")
    monkeypatch.setenv("MACHINA_DB_MAX_POOL", "1")
    real_create_pool = conn_mod.asyncpg.create_pool
    calls = {"count": 0}

    async def counting_create_pool(*args, **kwargs):
        calls["count"] += 1
        # Let every racer reach the check before the first pool lands, the way
        # real overlapping startup connects interleave.
        await asyncio.sleep(0.05)
        return await real_create_pool(*args, **kwargs)

    monkeypatch.setattr(conn_mod.asyncpg, "create_pool", counting_create_pool)
    database = conn_mod.Database(dsn=TEST_DSN)
    try:
        await asyncio.gather(*(database.connect() for _ in range(8)))
        assert calls["count"] == 1, "overlapping first connects built multiple pools"
        assert database.connected
        assert await database.fetchval("SELECT 1") == 1
        # The single surviving pool holds its minimum-size connection; every
        # racer that built its own pool would have leaked its backends.
        assert database.pool.get_size() == 1
    finally:
        await database.close()
    assert not database.connected


# ─── (d) NUL bytes in argv ──────────────────────────────────────────────────


def test_run_bounded_returns_typed_result_for_nul_argv(tmp_path):
    from machina.core.child_process import run_bounded

    result = run_bounded(
        [sys.executable, "-c", "print('ok')", "bad\x00arg"], timeout=5.0, cwd=tmp_path
    )
    assert result.spawn_error, result
    assert result.returncode is None
    assert result.stdout == ""
    assert result.timed_out is False
    assert result.truncated is False
    assert "null" in result.spawn_error.lower()
