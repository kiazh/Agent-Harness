"""PostgreSQL connection pool using asyncpg."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import asyncpg

from ah.core.config import config
from ah.core.exceptions import DatabaseError
from ah.core.metrics import metrics

logger = logging.getLogger(__name__)

DEFAULT_DSN = "postgresql://postgres:***@localhost:5432/agentharness"


class Database:
    """Manages asyncpg connection pool."""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or config.get("database_url") or DEFAULT_DSN
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Initialize the connection pool."""
        self._pool = await asyncpg.create_pool(
            self.dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )

    async def close(self) -> None:
        """Close the pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise DatabaseError("Database not connected. Call connect() first.")
        return self._pool

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[asyncpg.Connection, None]:
        """Acquire a connection from the pool."""
        async with self.pool.acquire() as conn:
            yield conn

    async def execute(self, query: str, *args) -> str:
        """Execute a query."""
        import time
        start = time.monotonic()
        try:
            async with self.acquire() as conn:
                result = await conn.execute(query, *args)
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("execute", duration_ms)
            return result
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("execute", duration_ms, is_error=True)
            raise

    async def fetch(self, query: str, *args) -> list[asyncpg.Record]:
        """Fetch rows."""
        import time
        start = time.monotonic()
        try:
            async with self.acquire() as conn:
                result = await conn.fetch(query, *args)
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("fetch", duration_ms)
            return result
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("fetch", duration_ms, is_error=True)
            raise

    async def fetchrow(self, query: str, *args) -> asyncpg.Record | None:
        """Fetch a single row."""
        import time
        start = time.monotonic()
        try:
            async with self.acquire() as conn:
                result = await conn.fetchrow(query, *args)
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("fetchrow", duration_ms)
            return result
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("fetchrow", duration_ms, is_error=True)
            raise

    async def fetchval(self, query: str, *args):
        """Fetch a single value."""
        import time
        start = time.monotonic()
        try:
            async with self.acquire() as conn:
                result = await conn.fetchval(query, *args)
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("fetchval", duration_ms)
            return result
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            metrics.record_db_call("fetchval", duration_ms, is_error=True)
            raise

    async def initialize_schema(self, schema_path: str | None = None) -> None:
        """Run schema.sql to create tables."""
        if schema_path is None:
            schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        with open(schema_path) as f:
            schema_sql = f.read()
        async with self.acquire() as conn:
            await conn.execute(schema_sql)

    @classmethod
    def reset(cls) -> None:
        """Reset the global Database singleton to a fresh instance."""
        global db
        db = cls()


# Global singleton
db = Database()
