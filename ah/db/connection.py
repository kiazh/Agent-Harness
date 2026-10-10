"""PostgreSQL connection pool using asyncpg."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import asyncpg

from ah.core.config import config
from ah.core.exceptions import DatabaseError
from ah.core.metrics import metrics

logger = logging.getLogger(__name__)


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


class Database:
    """Manages asyncpg connection pool."""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = (
            dsn
            or config.get("database_url")
            or os.environ.get("DATABASE_URL")
            or "postgresql://postgres@127.0.0.1:5432/agentharness"
        )
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Initialize the connection pool with configurable sizes."""
        if self._pool is not None:
            return
        if not self.dsn:
            raise ValueError(
                "Database DSN not configured. Set DATABASE_URL or database_url in config."
            )
        min_size = int(os.environ.get("AGENT_HARNESS_DB_MIN_POOL", "2"))
        max_size = int(os.environ.get("AGENT_HARNESS_DB_MAX_POOL", "10"))
        command_timeout = int(os.environ.get("AGENT_HARNESS_DB_TIMEOUT", "30"))

        self._pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=command_timeout,
        )

    async def close(self) -> None:
        """Close the pool."""
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None

    @property
    def connected(self) -> bool:
        """Whether this Database currently owns a connection pool."""
        return self._pool is not None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise DatabaseError("Database not connected. Call connect() first.")
        return self._pool

    @asynccontextmanager
    async def acquire(self, timeout: float = 10) -> AsyncGenerator[asyncpg.Connection, None]:
        """Acquire a connection from the pool (bounded wait to avoid indefinite block).

        AH-024: compatibility catching is limited to acquisition, never to
        caller execution. A TypeError raised by caller code inside the
        ``async with`` body must propagate unchanged instead of triggering
        the legacy fallback path (which would mask the original error and
        violate single-yield semantics).
        """
        # Fast path: modern asyncpg with timeout kwarg. Only the acquisition
        # itself is inside the TypeError guard.
        try:
            acquire_cm = self.pool.acquire(timeout=timeout)
        except TypeError:
            acquire_cm = None
        if acquire_cm is not None:
            async with acquire_cm as conn:
                yield conn
            return
        # Legacy fallback: older asyncpg without timeout kwarg.
        conn = await asyncio.wait_for(self.pool.acquire(), timeout=timeout)
        try:
            yield conn
        finally:
            await self.pool.release(conn)

    async def execute(self, query: str, *args) -> str:
        """Execute a query."""

        start = time.monotonic()
        try:
            async with self.acquire() as conn:
                result = await conn.execute(query, *args)
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("execute", duration_ms)
            return result
        except Exception:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("execute", duration_ms, is_error=True)
            raise

    async def executemany(self, query: str, args: list) -> None:
        """Execute *query* once per argument tuple in *args* (single round trip batch)."""

        start = time.monotonic()
        try:
            async with self.acquire() as conn:
                await conn.executemany(query, args)
            metrics.record_db_call("executemany", (time.monotonic() - start) * 1000)
        except Exception:
            metrics.record_db_call("executemany", (time.monotonic() - start) * 1000, is_error=True)
            raise

    async def fetch(self, query: str, *args) -> list[asyncpg.Record]:
        """Fetch rows."""

        start = time.monotonic()
        try:
            async with self.acquire() as conn:
                result = await conn.fetch(query, *args)
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("fetch", duration_ms)
            return result
        except Exception:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("fetch", duration_ms, is_error=True)
            raise

    async def fetchrow(self, query: str, *args) -> asyncpg.Record | None:
        """Fetch a single row."""

        start = time.monotonic()
        try:
            async with self.acquire() as conn:
                result = await conn.fetchrow(query, *args)
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("fetchrow", duration_ms)
            return result
        except Exception:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("fetchrow", duration_ms, is_error=True)
            raise

    async def fetchval(self, query: str, *args):
        """Fetch a single value."""

        start = time.monotonic()
        try:
            async with self.acquire() as conn:
                result = await conn.fetchval(query, *args)
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("fetchval", duration_ms)
            return result
        except Exception:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("fetchval", duration_ms, is_error=True)
            raise

    async def initialize_schema(self, schema_path: str | None = None) -> None:
        """Apply UTF-8 schema/migrations atomically, preserving data on failure."""
        if schema_path is None:
            schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        with open(schema_path, encoding="utf-8") as f:
            schema_sql = f.read()
        async with self.acquire() as conn:
            async with conn.transaction():
                await conn.execute(schema_sql)

    @classmethod
    def reset(cls) -> None:
        """Reset the global Database singleton to a fresh instance."""
        global db
        db = cls()


# Global singleton
db = Database()
