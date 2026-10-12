"""Import-cycle-safe lazy handles to the shared database objects.

``machina.db.connection`` cannot be imported at module top level everywhere: its own
top-level ``config`` import runs package init (machina.core.__init__ →
config.load → machina.memory → …), and any module already on that import stack that
also needs ``db`` would trigger a circular import — which once silently
discarded the entire config file depending on import order.

This module imports nothing, so ``from machina.db.lazy import …`` is safe in every
import order. Bindings resolve against ``machina.db.connection`` on first USE
(always post-boot), never at import time.
"""

from __future__ import annotations


def parse_command_count(result: str | None) -> int:
    """Parse the count from an asyncpg command result string (e.g. 'DELETE 3' -> 3).

    Handles various formats: 'DELETE 3', 'UPDATE 1', 'INSERT 0 1', etc.
    Returns 0 if the result is None, empty, or cannot be parsed.
    Mocks returning non-strings are treated as 1 (success) to stay
    backward-compatible with unit tests using AsyncMock.
    """
    if result is None:
        return 0
    if isinstance(result, int) and not isinstance(result, bool):
        return result
    if not isinstance(result, str):
        # AsyncMock / MagicMock in tests: truthy mock means 1 row.
        try:
            if result:
                return 1
            return 0
        except Exception:
            return 0
    if not result:
        return 0
    try:
        parts = result.split()
    except Exception:
        return 1
    if not parts:
        return 0
    # The count is always the last token in asyncpg command results
    try:
        return int(parts[-1])
    except (ValueError, IndexError, TypeError):
        return 0


class LazyDb:
    """Lazy reference to the shared ``machina.db.connection.db`` singleton.

    Attribute access (including method calls and ``async with`` usage)
    delegates to the live singleton, and so does assignment: writes land on
    the shared object exactly as if this were the Database itself (lifecycle
    code and fixtures mutate the shared handle, e.g. pool swaps).
    """

    __slots__ = ()

    def __getattr__(self, name: str):
        from machina.db import connection as _connection

        return getattr(_connection.db, name)

    def __setattr__(self, name: str, value) -> None:
        from machina.db import connection as _connection

        setattr(_connection.db, name, value)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<LazyDb → machina.db.connection.db>"
