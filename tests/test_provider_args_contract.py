"""H10 (holistic review 2026-10-15): one max_tokens ceiling for config + transport.

Configuration accepted any positive ``max_tokens`` while every provider
transport rejected values above 32768, so saving 65536 succeeded and later
calls failed before reaching the provider. These tests pin the single shared
policy: configuration and request time agree on the same executable range.
"""

from __future__ import annotations

import pytest

from machina.core.config import Config, validate_value
from machina.core.exceptions import ValidationError
from machina.core.provider.transport import _validate_params

TRANSPORT_CEILING = 32768


def test_config_validation_rejects_max_tokens_the_transport_cannot_execute():
    """The save-time contract must match the request-time contract (H10)."""
    assert validate_value("max_tokens", TRANSPORT_CEILING) == TRANSPORT_CEILING
    with pytest.raises(ValueError, match="max_tokens must be in range 1..32768"):
        validate_value("max_tokens", 65536)


def test_config_set_rejects_without_mutating_the_value():
    """A rejected set must not half-apply (AH-AUDIT-042 contract)."""
    cfg = Config()
    with pytest.raises(ValueError, match="max_tokens must be in range 1..32768"):
        cfg.set("max_tokens", 65536)
    assert cfg.max_tokens == 4096


def test_config_load_keeps_the_default_for_an_unexecutable_value(tmp_path, caplog):
    """A stale config file value the transport can never run is not activated."""
    import logging

    path = tmp_path / "config.yaml"
    path.write_text("max_tokens: 65536\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="machina.core.config"):
        loaded = Config.load(path)
    assert loaded.get("max_tokens") == 4096
    assert any("max_tokens" in record.getMessage() for record in caplog.records)


def test_config_and_transport_agree_at_the_boundary():
    """No path accepts a value the other rejects: the ceiling is shared."""
    for boundary in (TRANSPORT_CEILING, TRANSPORT_CEILING + 1):
        try:
            validate_value("max_tokens", boundary)
            config_ok = True
        except ValueError:
            config_ok = False
        try:
            _validate_params(0.7, boundary)
            transport_ok = True
        except ValidationError:
            transport_ok = False
        assert config_ok == transport_ok, f"surfaces disagree at {boundary}"


def test_never_accepted_value_cannot_be_saved_and_called_end_to_end():
    """The H10 reproduction path stays closed: save fails, so no call can
    reach the transport with a value request-time validation rejects."""
    cfg = Config()
    with pytest.raises(ValueError):
        cfg.set("max_tokens", 65536)
    # The value the provider would have received is still the default.
    _validate_params(0.7, int(cfg.get("max_tokens")))


def test_config_exports_the_single_ceiling_the_transport_imports():
    """One named constant, not two literals: transport reads config's value."""
    import machina.core.config as config_module
    from machina.core.provider import transport

    assert config_module.MAX_TOKENS == TRANSPORT_CEILING
    assert transport.MAX_TOKENS == config_module.MAX_TOKENS
