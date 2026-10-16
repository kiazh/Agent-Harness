"""PostgreSQL connection pool using asyncpg."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import asyncpg

from machina.core.config import config
from machina.core.exceptions import DatabaseError
from machina.core.metrics import metrics
from machina.db.lazy import parse_command_count  # noqa: F401  (single impl; re-export)

logger = logging.getLogger(__name__)

# HELD-55: additive runtime migration for existing databases. schema.sql never
# runs on an already-initialized database, so this must be applied by
# ``initialize_runtime_schema`` too. Keep in sync with the context_chunks
# section of schema.sql. Idempotent (IF NOT EXISTS / SET DEFAULT).
_CONTEXT_EVENT_ORDER_MIGRATION = """
CREATE SEQUENCE IF NOT EXISTS context_chunks_event_seq;
ALTER TABLE context_chunks ADD COLUMN IF NOT EXISTS event_seq BIGINT;
ALTER TABLE context_chunks ALTER COLUMN event_seq
    SET DEFAULT nextval('context_chunks_event_seq');
ALTER SEQUENCE context_chunks_event_seq OWNED BY context_chunks.event_seq;
CREATE INDEX IF NOT EXISTS idx_context_chunks_session_created_event
    ON context_chunks(session_id, created_at DESC, event_seq DESC NULLS LAST, id DESC);
"""

# H08: additive runtime migration for existing databases. schema.sql never
# runs on an already-initialized database, so this must be applied by
# ``initialize_runtime_schema`` too. Keep in sync with the memories section of
# schema.sql. Idempotent (IF NOT EXISTS); MemoryStore's lazy ensure remains as
# defense-in-depth for databases that skip boot migrations.
_MEMORY_EMBEDDING_SPACE_MIGRATION = """
ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_space TEXT;
CREATE INDEX IF NOT EXISTS idx_memories_embedding_space ON memories(embedding_space);
"""

# Token-efficiency ledger + artifact store (2026-10-16). schema.sql never runs
# on an already-initialized database, so existing installations get the same
# additive DDL here. Keep in sync with the llm_usage / tool_artifacts sections
# of schema.sql. Idempotent (IF NOT EXISTS).
_USAGE_LEDGER_MIGRATION = """
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS requested_model TEXT;
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS cache_read_tokens INT CHECK (cache_read_tokens >= 0);
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS cache_write_tokens INT CHECK (cache_write_tokens >= 0);
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS usage_basis TEXT;
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION CHECK (cost_usd >= 0);
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS price_version TEXT;
ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS accepted BOOLEAN;
"""

_TOOL_ARTIFACTS_MIGRATION = """
CREATE TABLE IF NOT EXISTS tool_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_version TEXT,
    status TEXT,
    summary TEXT,
    content TEXT NOT NULL,
    content_bytes INT NOT NULL CHECK (content_bytes >= 0),
    token_estimate INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    access_count INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tool_artifacts_session
    ON tool_artifacts(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_artifacts_expiry
    ON tool_artifacts(expires_at) WHERE expires_at IS NOT NULL;
"""


class Database:
    """Manages asyncpg connection pool."""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = (
            dsn
            or config.get("database_url")
            or os.environ.get("DATABASE_URL")
            or "postgresql://postgres@127.0.0.1:5432/machina"
        )
        self._pool: asyncpg.Pool | None = None
        # Connect mutex: overlapping first connects must not each build a pool
        # (the loser's pool would leak its connections). asyncio locks bind to
        # the event loop of first use, so the lock is recreated per loop —
        # a Database instance reused across test/dev loops still serializes
        # within each loop.
        self._connect_lock: asyncio.Lock | None = None
        self._connect_lock_loop: asyncio.AbstractEventLoop | None = None

    def _connect_mutex(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._connect_lock is None or self._connect_lock_loop is not loop:
            self._connect_lock = asyncio.Lock()
            self._connect_lock_loop = loop
        return self._connect_lock

    async def connect(self) -> None:
        """Initialize the connection pool with configurable sizes.

        Concurrent first connects are serialized per event loop: the double
        check after acquiring the mutex guarantees exactly one pool is ever
        created and assigned, so no overlapping caller can leak a second
        pool's connections.
        """
        if self._pool is not None:
            return
        async with self._connect_mutex():
            if self._pool is not None:
                return
            if not self.dsn:
                raise ValueError(
                    "Database DSN not configured. Set DATABASE_URL or database_url in config."
                )
            min_size = int(os.environ.get("MACHINA_DB_MIN_POOL", "2"))
            max_size = int(os.environ.get("MACHINA_DB_MAX_POOL", "10"))
            command_timeout = int(os.environ.get("MACHINA_DB_TIMEOUT", "30"))

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
        default_schema = schema_path is None
        if schema_path is None:
            schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        with open(schema_path, encoding="utf-8") as f:
            schema_sql = f.read()
        async with self.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended('machina:runtime_migrations', 0))"
                )
                await conn.execute(schema_sql)
                if default_schema:
                    for name in (
                        "growth_schema.sql",
                        "delegation_schema.sql",
                        "image_context_schema.sql",
                        "soulspec_schema.sql",
                    ):
                        with open(
                            os.path.join(os.path.dirname(__file__), name), encoding="utf-8"
                        ) as f:
                            await conn.execute(f.read())

    async def initialize_runtime_schema(self) -> None:
        """Upgrade required runtime features on an initialized native database."""
        async with self.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('machina:runtime_migrations', 0))"
            )
            for name in (
                "growth_schema.sql",
                "delegation_schema.sql",
                "image_context_schema.sql",
                "soulspec_schema.sql",
            ):
                with open(os.path.join(os.path.dirname(__file__), name), encoding="utf-8") as f:
                    await conn.execute(f.read())
            await conn.execute(_CONTEXT_EVENT_ORDER_MIGRATION)
            await conn.execute(_MEMORY_EMBEDDING_SPACE_MIGRATION)
            await conn.execute(_USAGE_LEDGER_MIGRATION)
            await conn.execute(_TOOL_ARTIFACTS_MIGRATION)

    # Standalone growth-migration entry point; migration tests apply it, no production caller. Kept (R4-106).
    async def initialize_growth_schema(self) -> None:
        """Apply only the additive growth migration on existing installations."""
        with open(
            os.path.join(os.path.dirname(__file__), "growth_schema.sql"), encoding="utf-8"
        ) as f:
            schema_sql = f.read()
        async with self.acquire() as conn, conn.transaction():
            await conn.execute(schema_sql)

    @classmethod
    def reset(cls) -> None:
        """Reset the global Database singleton to a fresh instance.

        RF2-N9: modules that bound the singleton at import time with
        ``from machina.db.connection import db`` keep the replaced object and
        silently diverge from the new one. Log every known stale holder so an
        operator can see it; those modules must switch to
        ``machina.db.lazy.LazyDb`` (or re-import after reset). audit.py is a
        known holder owned elsewhere; this warning is the visibility seam.
        """
        global db
        previous = db
        stale: list[str] = []
        for name, module in list(sys.modules.items()):
            if name == __name__ or module is None:
                continue
            try:
                if getattr(module, "db", None) is previous:
                    stale.append(name)
            except Exception:
                continue
        if stale:
            logger.warning(
                "Database.reset(): %d module(s) still hold the replaced DB handle: %s",
                len(stale),
                ", ".join(sorted(stale)),
            )
        db = cls()


# Global singleton
db = Database()
