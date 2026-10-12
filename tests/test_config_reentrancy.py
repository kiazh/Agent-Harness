"""Configuration remains one object during recursive module imports."""

import importlib

import pytest


def test_reentrant_load_publishes_the_same_singleton(monkeypatch):
    module = importlib.import_module("machina.core.config")
    observed = []

    def load(cls, path=None, *, _target=None):
        observed.append(module.get_config())
        result = _target or cls()
        result.persona_description = "A patient coding partner"
        return result

    monkeypatch.setattr(module, "_config", None)
    monkeypatch.setattr(module.Config, "load", classmethod(load))
    loaded = module.get_config()
    assert observed == [loaded]
    assert observed[0].persona_description == "A patient coding partner"


def test_failed_atomic_save_preserves_default_file_and_live_value(monkeypatch, tmp_path):
    module = importlib.import_module("machina.core.config")
    path = tmp_path / "config.yaml"
    path.write_text("persona_description: Original persona\n")
    monkeypatch.setattr(module, "DEFAULT_CONFIG_PATH", path)
    value = module.Config(persona_description="Original persona")

    def fail_replace(*_args):
        raise OSError("rename failed")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError):
        value.set("persona_description", "Replacement persona", persist=True)
    assert value.persona_description == "Original persona"
    assert path.read_text() == "persona_description: Original persona\n"
    assert list(tmp_path.iterdir()) == [path]
