"""Session manager — create, resume, and track agent sessions."""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid

import asyncpg
import msgpack
from cachetools import TTLCache

from ah.core.models import Session
from ah.db.connection import db

__all__ = ["Session", "SessionManager", "session_manager"]

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages agent sessions in PostgreSQL with a 60-second LRU cache."""

    def __init__(self) -> None:
        # TTLCache: max 128 sessions, 60-second TTL
        self._cache: TTLCache = TTLCache(maxsize=128, ttl=60)
        self._cache_lock = asyncio.Lock()

    async def _cache_get(self, session_id: uuid.UUID) -> Session | None:
        async with self._cache_lock:
            cached = self._cache.get(session_id)
            return copy.deepcopy(cached) if cached is not None else None

    async def _cache_put(self, session: Session) -> None:
        async with self._cache_lock:
            # The cache owns its snapshot, including nested session state.
            self._cache[session.id] = copy.deepcopy(session)

    async def _cache_invalidate(self, session_id: uuid.UUID) -> None:
        async with self._cache_lock:
            self._cache.pop(session_id, None)

    @staticmethod
    def generate_title(message: str, max_length: int = 60) -> str:
        """Generate a concise title from the first message."""
        first_line = message.strip().split("\n")[0].strip()
        if len(first_line) > max_length:
            return first_line[: max_length - 3] + "..."
        return first_line or "Untitled"

    async def create(
        self,
        title: str | None = None,
        agent_id: str = "harness",
        state: dict | None = None,
        goal: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        context_budget: int = 8000,
    ) -> Session:
        """Create a new session."""
        # Auto-generate title from goal if not provided
        if title is None and goal:
            title = self.generate_title(goal)

        from ah.core.session_mode import get_global_default

        initial_mode = get_global_default()
        state_msgpack = msgpack.packb(state or {}, use_bin_type=True)
        row = await db.fetchrow(
            """
            INSERT INTO sessions (title, agent_id, state_msgpack, status, goal, model, provider, context_budget, execution_mode)
            VALUES ($1, $2, $3, 'active', $4, $5, $6, $7, $8)
            RETURNING id, title, agent_id, status, state_msgpack, goal, model, provider, context_budget, execution_mode, created_at, last_activity
            """,
            title,
            agent_id,
            state_msgpack,
            goal,
            model,
            provider,
            context_budget,
            initial_mode,
        )
        session = self._row_to_session(row)
        await self._cache_put(session)
        return session

    async def get(self, session_id: uuid.UUID) -> Session | None:
        """Get a session by ID (cached for 60 seconds).

        AH-AUDIT-019: the cached object is never shared mutably — hits
        return a copy, so callers cannot mutate the authority source.
        Execution-critical reads must use get_fresh() instead.
        """
        cached = await self._cache_get(session_id)
        if cached is not None:
            return cached
        row = await db.fetchrow(
            """
            SELECT id, title, agent_id, status, state_msgpack, goal, model, provider, context_budget, execution_mode, created_at, last_activity
            FROM sessions WHERE id = $1
            """,
            session_id,
        )
        if row is None:
            return None
        session = self._row_to_session(row)
        await self._cache_put(session)
        return session

    async def get_fresh(self, session_id: uuid.UUID) -> Session | None:
        """Bypass the cache for execution-critical reads (AH-AUDIT-019).

        Session execution, resume, provider/model resolution, and state
        updates must observe current durable state, not a up-to-60s-old
        cache entry written by another worker. The fresh row refreshes the
        cache for presentational readers.
        """
        row = await db.fetchrow(
            """
            SELECT id, title, agent_id, status, state_msgpack, goal, model, provider, context_budget, execution_mode, created_at, last_activity
            FROM sessions WHERE id = $1
            """,
            session_id,
        )
        if row is None:
            return None
        session = self._row_to_session(row)
        await self._cache_put(session)
        return session

    async def get_last_active(self) -> Session | None:
        """Get the most recently active session."""
        row = await db.fetchrow(
            """
            SELECT id, title, agent_id, status, state_msgpack, goal, model, provider, context_budget, execution_mode, created_at, last_activity
            FROM sessions
            WHERE status = 'active'
            ORDER BY last_activity DESC
            LIMIT 1
            """,
        )
        if row is None:
            return None
        session = self._row_to_session(row)
        await self._cache_put(session)
        return session

    async def update_state(self, session_id: uuid.UUID, state: dict) -> None:
        """Update session state (LangGraph checkpoint, etc.)."""
        state_msgpack = msgpack.packb(state, use_bin_type=True)
        await db.execute(
            """
            UPDATE sessions
            SET state_msgpack = $2, last_activity = now()
            WHERE id = $1
            """,
            session_id,
            state_msgpack,
        )
        await self._cache_invalidate(session_id)

    async def update_state_fields(self, session_id: uuid.UUID, fields: dict) -> dict:
        """Transactionally merge *fields* into session state (AH-AUDIT-018).

        The row is locked (SELECT ... FOR UPDATE) so concurrent writers
        updating independent fields serialize instead of clobbering each
        other — emotion, watermark, and compaction-state writes all survive.
        Unrelated existing keys are preserved; legacy non-dict state is
        treated as empty. Returns the merged state. Never relies on a
        cached snapshot for the write.
        """
        if not isinstance(fields, dict):
            raise ValueError("fields must be a dict")
        async with db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT state_msgpack FROM sessions WHERE id = $1 FOR UPDATE",
                    session_id,
                )
                if row is None:
                    raise ValueError(f"Session {session_id} not found")
                current: dict = {}
                raw = row.get("state_msgpack") if hasattr(row, "get") else row["state_msgpack"]
                if raw:
                    try:
                        unpacked = msgpack.unpackb(raw, raw=False)
                        if isinstance(unpacked, dict):
                            current = unpacked
                    except Exception:
                        current = {}
                merged = {**current, **fields}
                await conn.execute(
                    "UPDATE sessions SET state_msgpack = $2, last_activity = now() WHERE id = $1",
                    session_id,
                    msgpack.packb(merged, use_bin_type=True),
                )
        await self._cache_invalidate(session_id)
        return merged

    async def update_activity(self, session_id: uuid.UUID) -> None:
        """Touch last_activity timestamp."""
        await db.execute(
            "UPDATE sessions SET last_activity = now() WHERE id = $1",
            session_id,
        )
        # Invalidate so readers never see stale last_activity for up to 60s.
        await self._cache_invalidate(session_id)

    async def set_goal(self, session_id: uuid.UUID, goal: str) -> None:
        """Update session goal."""
        await db.execute(
            "UPDATE sessions SET goal = $2, last_activity = now() WHERE id = $1",
            session_id,
            goal,
        )
        await self._cache_invalidate(session_id)

    async def set_title(self, session_id: uuid.UUID, title: str) -> None:
        """Rename a session."""
        await db.execute(
            "UPDATE sessions SET title = $2 WHERE id = $1",
            session_id,
            title,
        )
        await self._cache_invalidate(session_id)

    async def archive(self, session_id: uuid.UUID) -> None:
        """Archive a session."""
        await db.execute(
            "UPDATE sessions SET status = 'archived' WHERE id = $1",
            session_id,
        )
        await self._cache_invalidate(session_id)

    async def delete(self, session_id: uuid.UUID) -> bool:
        """Permanently delete a session and its active and archived context."""
        async with db.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM context_archive WHERE session_id = $1", session_id)
                result = await conn.execute("DELETE FROM sessions WHERE id = $1", session_id)
        await self._cache_invalidate(session_id)
        from ah.db.connection import parse_command_count

        return parse_command_count(result) > 0

    async def search(self, query: str, limit: int = 20) -> list[Session]:
        """Full-text search over session titles."""
        rows = await db.fetch(
            """
            SELECT id, title, agent_id, status, goal, model, provider, context_budget, execution_mode, created_at, last_activity
            FROM sessions
            WHERE to_tsvector('english', COALESCE(title, '')) @@ plainto_tsquery('english', $1)
            ORDER BY ts_rank(to_tsvector('english', COALESCE(title, '')), plainto_tsquery('english', $1)) DESC, last_activity DESC
            LIMIT $2
            """,
            query,
            limit,
        )
        return [self._row_to_session(r) for r in rows]

    async def fork(self, session_id: uuid.UUID, title: str | None = None) -> Session:
        """Fork a session — create a new session with copied state and context.

        Atomic: session creation and the INSERT ... SELECT chunk copy run in
        a single transaction, so a copy failure cannot leave an orphan fork.
        """
        source = await self.get(session_id)
        if source is None:
            raise ValueError(f"Session {session_id} not found")

        fork_title = title or f"Fork of {source.title or 'untitled'}"
        state_msgpack = msgpack.packb(source.state or {}, use_bin_type=True)
        async with db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO sessions (title, agent_id, state_msgpack, status, goal, model, provider, context_budget, execution_mode)
                    VALUES ($1, $2, $3, 'active', $4, $5, $6, $7, $8)
                    RETURNING id, title, agent_id, status, state_msgpack, goal, model, provider, context_budget, execution_mode, created_at, last_activity
                    """,
                    fork_title,
                    source.agent_id,
                    state_msgpack,
                    source.goal,
                    source.model,
                    source.provider,
                    source.context_budget,
                    "ask",  # Forks never inherit another session's live authority.
                )
                new_session = self._row_to_session(row)

                # Copy context chunks, keeping their timestamps: a single INSERT ... SELECT
                # would otherwise stamp every copy with the same now(), scrambling order.
                await conn.execute(
                    """
                    INSERT INTO context_chunks (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, created_at, accessed_at)
                    SELECT $2, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, created_at, accessed_at
                    FROM context_chunks
                    WHERE session_id = $1
                    """,
                    session_id,
                    new_session.id,
                )
        await self._cache_put(new_session)
        return new_session

    async def list_sessions(
        self, status: str | None = None, limit: int = 20, offset: int = 0
    ) -> list[Session]:
        """List sessions (column projection: exclude state_msgpack for efficiency)."""
        if status:
            rows = await db.fetch(
                """
                SELECT id, title, agent_id, status, goal, model, provider, context_budget, execution_mode, created_at, last_activity
                FROM sessions
                WHERE status = $1
                ORDER BY last_activity DESC, id DESC
                LIMIT $2 OFFSET $3
                """,
                status,
                limit,
                offset,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id, title, agent_id, status, goal, model, provider, context_budget, execution_mode, created_at, last_activity
                FROM sessions
                ORDER BY last_activity DESC, id DESC
                LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )
        return [self._row_to_session(r) for r in rows]

    def _row_to_session(self, row: asyncpg.Record) -> Session:
        state = {}
        state_msgpack = row.get("state_msgpack")
        if state_msgpack:
            state = msgpack.unpackb(state_msgpack, raw=False)
        return Session(
            id=row["id"],
            title=row["title"],
            agent_id=row["agent_id"],
            status=row["status"],
            state=state,
            goal=row["goal"],
            model=row["model"],
            provider=row["provider"],
            context_budget=row["context_budget"],
            execution_mode=row.get("execution_mode", "ask"),
            created_at=row["created_at"],
            last_activity=row["last_activity"],
        )

    @classmethod
    def reset(cls) -> None:
        """Reset the global SessionManager singleton to a fresh instance."""
        global session_manager
        session_manager = cls()


session_manager = SessionManager()
