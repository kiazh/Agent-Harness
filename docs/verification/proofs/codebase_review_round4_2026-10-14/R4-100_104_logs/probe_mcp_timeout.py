"""R4-104 probe: force the live-MCP wall-clock timeout branch (evidence only).

Replaces ``asyncio.timeout`` with an immediate raiser for the single live
round-trip test so the new timeout→fail path executes; it must FAIL with the
budget breakdown, never skip.

Usage:
    PYTHONPATH=.../R4-100_104_logs .venv/bin/python -m pytest -p probe_mcp_timeout \
        tests/test_mcp_manage_tool.py::test_live_self_connect_call_disconnect -q
"""

from __future__ import annotations

import asyncio

_replaced: object = None


class _ForcedTimeout:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        raise TimeoutError("forced by probe_mcp_timeout")

    async def __aexit__(self, *exc_info) -> bool:
        return False


def pytest_runtest_setup(item) -> None:
    if item.name == "test_live_self_connect_call_disconnect":
        global _replaced
        _replaced = asyncio.timeout
        asyncio.timeout = _ForcedTimeout


def pytest_runtest_teardown(item) -> None:
    global _replaced
    if item.name == "test_live_self_connect_call_disconnect" and _replaced is not None:
        asyncio.timeout = _replaced
        _replaced = None
