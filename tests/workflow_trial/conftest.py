"""Use a Selector loop for psycopg's async PostgreSQL saver on Windows."""

from __future__ import annotations

import asyncio
import sys


def pytest_asyncio_loop_factories(config, item):
    if sys.platform == "win32":
        return {"windows-selector": asyncio.WindowsSelectorEventLoopPolicy().new_event_loop}
    return {"default": asyncio.new_event_loop}
