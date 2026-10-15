"""Regression tests for the config import-cycle (order-dependent silent discard).

Root cause: ``machina.db.connection`` (partial, line 14) → ``machina.core.__init__`` →
``get_config()`` → ``Config.load()`` → ``validate_value(persona_description)``
→ ``machina.memory`` package init → ``approval → provider → usage`` →
``from machina.db.connection import db`` → ImportError. The old all-or-nothing
``load()`` then discarded the ENTIRE config file, so affected processes ran on
defaults (wrong model/provider/mode) with only one warning.

Fix: lazy ``db`` binding (``machina.db.lazy``) in the nine cycle members plus
per-key isolation in ``Config.load``.
"""

from __future__ import annotations

import subprocess
import sys


def _run_child(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_db_first_import_order_keeps_file_config(tmp_path):
    """The historical poison order must apply the config file with no warning."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "model: probe-model-xyz\nexecution_mode: workspace\npersona_description: hello\n",
        encoding="utf-8",
    )
    code = (
        "from machina.db.connection import db;"  # poison order: partial connection first
        "from machina.core.config import Config;"
        f"c = Config.load(r'{cfg_file}');"
        "assert c.model == 'probe-model-xyz', repr(c.model);"
        "assert c.execution_mode == 'workspace', repr(c.execution_mode);"
        "assert c.persona_description == 'hello', repr(c.persona_description);"
        "print('ORDER-OK')"
    )
    proc = _run_child(code)
    assert "ORDER-OK" in proc.stdout, proc.stderr[-3000:]
    assert "Failed to load config" not in proc.stderr
    assert "Ignoring unreadable config" not in proc.stderr


def test_memory_first_import_order_keeps_file_config(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("model: probe-model-xyz\n", encoding="utf-8")
    code = (
        "import machina.memory.redaction;"
        "from machina.core.config import Config;"
        f"c = Config.load(r'{cfg_file}');"
        "assert c.model == 'probe-model-xyz', repr(c.model);"
        "print('ORDER-OK')"
    )
    proc = _run_child(code)
    assert "ORDER-OK" in proc.stdout, proc.stderr[-3000:]
    assert "Failed to load config" not in proc.stderr


def test_gateway_first_import_order_keeps_file_config(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("model: probe-model-xyz\n", encoding="utf-8")
    code = (
        "import machina.gateway.server;"
        "from machina.core.config import Config;"
        f"c = Config.load(r'{cfg_file}');"
        "assert c.model == 'probe-model-xyz', repr(c.model);"
        "print('ORDER-OK')"
    )
    proc = _run_child(code)
    assert "ORDER-OK" in proc.stdout, proc.stderr[-3000:]
    assert "Failed to load config" not in proc.stderr


def test_unreadable_key_does_not_discard_file(tmp_path, monkeypatch):
    """One failing key must not discard the other keys (per-key isolation)."""
    import importlib

    cfgmod = importlib.import_module("machina.core.config")

    real = cfgmod.validate_value

    def boom(key, value):
        if key == "persona_description":
            raise ImportError("simulated startup cycle")
        return real(key, value)

    monkeypatch.setattr(cfgmod, "validate_value", boom)
    cfg_file = tmp_path / "config.yaml"
    # Failing key FIRST: all-or-nothing loaders discard the later valid key.
    cfg_file.write_text("persona_description: whatever\nmodel: keep-me\n", encoding="utf-8")
    loaded = cfgmod.Config.load(str(cfg_file))
    assert loaded.model == "keep-me"


def test_lazy_db_delegates_to_live_singleton():
    """The lazy handle resolves to the real shared Database object."""
    from machina.core import context as ctxmod
    from machina.core import usage as usagemod
    from machina.db import connection as connmod
    from machina.db.lazy import LazyDb, parse_command_count

    assert isinstance(ctxmod.db, LazyDb)
    assert isinstance(usagemod.db, LazyDb)
    assert connmod.db is not None
    # Delegation works without a connection (attribute read, not I/O).
    assert ctxmod.db.connected is False or isinstance(ctxmod.db.connected, bool)
    assert parse_command_count("DELETE 3") == 3
    assert parse_command_count(None) == 0
    # Writes forward to the live singleton (transparent proxy behavior).
    ctxmod.db._lazy_probe_attr_xyz = 1
    assert connmod.db._lazy_probe_attr_xyz == 1
    del connmod.db._lazy_probe_attr_xyz
    assert not hasattr(connmod.db, "_lazy_probe_attr_xyz")


def test_provider_first_import_order_keeps_file_config(tmp_path):
    """Provider-first order must keep persona_description.

    The provider package triggers ``Config.load`` while still initializing
    (``provider/__init__`` line 26 imports ``.anthropic``, whose module-level
    ``from machina.core.config import config`` resolves the lazy attribute).
    Validating ``persona_description`` used to import the eagerly-initialized
    ``machina.memory`` package, whose ``approval`` module does ``from
    machina.core.provider import audit_log`` — an attribute the provider
    package only binds two __init__ lines later — so the ImportError silently
    discarded the configured persona.
    """
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "model: probe-model-xyz\npersona_description: hello\n",
        encoding="utf-8",
    )
    code = (
        "from machina.core.provider.copilot import CopilotProvider;"  # poison order
        "from machina.core.config import Config;"
        f"c = Config.load(r'{cfg_file}');"
        "assert c.model == 'probe-model-xyz', repr(c.model);"
        "assert c.persona_description == 'hello', repr(c.persona_description);"
        "print('ORDER-OK')"
    )
    proc = _run_child(code)
    assert "ORDER-OK" in proc.stdout, proc.stderr[-3000:]
    assert "Ignoring unreadable config" not in proc.stderr


def test_config_load_pulls_no_provider_stack(tmp_path):
    """Loading config must not import the provider stack or the memory body.

    ``Config.load`` only needs ``machina.memory.redaction`` (a stdlib-only
    leaf) for persona validation; the eager memory re-exports used to drag
    in the whole provider tree on every load.
    """
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "model: probe-model-xyz\npersona_description: hello\n",
        encoding="utf-8",
    )
    code = (
        "import sys;"
        "from machina.core.config import Config;"
        f"Config.load(r'{cfg_file}');"
        "assert 'machina.core.provider' not in sys.modules, 'provider stack pulled';"
        "assert 'machina.memory.approval' not in sys.modules, 'memory body pulled';"
        "print('NO-STACK-OK')"
    )
    proc = _run_child(code)
    assert "NO-STACK-OK" in proc.stdout, proc.stderr[-3000:]
