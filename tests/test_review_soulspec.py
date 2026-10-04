"""The internal empty-tool marker cannot collide with a plugin tool."""

import pytest

from ah.tools.base import ToolRegistry


def test_internal_no_tools_name_is_reserved():
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="reserved"):

        @registry.register(name="__no_tools__")
        def plugin_tool():
            return "unexpected"
