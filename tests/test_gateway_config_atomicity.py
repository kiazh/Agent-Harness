"""Failed default persistence does not change the live provider route."""

from unittest.mock import Mock

import pytest

from ah.core.config import config
from ah.gateway.server import Gateway


@pytest.mark.parametrize("key,value", [("model", "replacement-model"), ("provider", "deepseek")])
async def test_failed_default_save_keeps_live_route(monkeypatch, key, value):
    gateway = Gateway(lambda _frame: None, owns_db=False)
    before = getattr(gateway, key)
    before_default = getattr(config, key)
    monkeypatch.setattr(config, "save", Mock(side_effect=OSError("write failed")))

    with pytest.raises(OSError):
        await gateway._config_set({"key": key, "value": value, "persist": True})
    assert getattr(gateway, key) == before
    assert getattr(config, key) == before_default
