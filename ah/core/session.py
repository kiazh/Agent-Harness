"""Session manager — create, resume, and track agent sessions."""

from __future__ import annotations

import asyncio
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
            return self._cache.get(session_id)

    async def _cache_put(self, session: Session) -> None:
        async with self._cache_lock:
            self._cache[session.id] = session

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

        state_msgpack = msgpack.packb(state or {}, use_bin_type=True)
        row = await db.fetchrow(
            """
            INSERT INTO sessions (title, agent_id, state_msgpack, status, goal, model, provider, context_budget)
            VALUES ($1, $2, $3, 'active', $4, $5, $6, $7)
            RETURNING id, title, agent_id, status, state_msgpack, goal, model, provider, context_budget, created_at, last_activity
            """,
            title,
            agent_id,
            state_msgpack,
            goal,
            model,
            provider,
            context_budget,
        )
        session = self._row_to_session(row)
        await self._cache_put(session)
        return session

    async def get(self, session_id: uuid.UUID) -> Session | None:
        """Get a session by ID (cached for 60 seconds)."""
        cached = await self._cache_get(session_id)
        if cached is not None:
            return cached
        row = await db.fetchrow(
            """
            SELECT id, title, agent_id, status, state_msgpack, goal, model, provider, context_budget, created_at, last_activity
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
            SELECT id, title, agent_id, status, state_msgpack, goal, model, provider, context_budget, created_at, last_activity
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

    async def update_activity(self, session_id: uuid.UUID) -> None:
        """Touch last_activity timestamp."""
        await db.execute(
            "UPDATE sessions SET last_activity = now() WHERE id = $1",
            session_id,
        )
        # Don't invalidate cache — let it expire naturally

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

    async def set_status(self, session_id: uuid.UUID, status: str) -> None:
        """Update session status."""
        await db.execute(
            "UPDATE sessions SET status = $2 WHERE id = $1",
            session_id,
            status,
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
        """Permanently delete a session and all its context chunks."""
        result = await db.execute(
            "DELETE FROM sessions WHERE id = $1",
            session_id,
        )
        await self._cache_invalidate(session_id)
        from ah.db.connection import parse_command_count

        return parse_command_count(result) > 0

    async def search(self, query: str, limit: int = 20) -> list[Session]:
        """Full-text search over session titles."""
        rows = await db.fetch(
            """
            SELECT id, title, agent_id, status, goal, model, provider, context_budget, created_at, last_activity
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
        """Fork a session — create a new session with copied state and context."""
        source = await self.get(session_id)
        if source is None:
            raise ValueError(f"Session {session_id} not found")

        new_session = await self.create(
            title=title or f"Fork of {source.title or 'untitled'}",
            agent_id=source.agent_id,
            state=source.state,
            goal=source.goal,
            model=source.model,
            provider=source.provider,
            context_budget=source.context_budget,
        )

        # Copy context chunks, keeping their timestamps: a single INSERT ... SELECT
        # would otherwise stamp every copy with the same now(), scrambling order.
        await db.execute(
            """
            INSERT INTO context_chunks (session_id, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, created_at, accessed_at)
            SELECT $2, agent_id, chunk_type, payload_msgpack, token_count, embedding, search_text, created_at, accessed_at
            FROM context_chunks
            WHERE session_id = $1
            """,
            session_id,
            new_session.id,
        )

        return new_session

    async def list_sessions(self, status: str | None = None, limit: int = 20) -> list[Session]:
        """List sessions (column projection: exclude state_msgpack for efficiency)."""
        if status:
            rows = await db.fetch(
                """
                SELECT id, title, agent_id, status, goal, model, provider, context_budget, created_at, last_activity
                FROM sessions
                WHERE status = $1
                ORDER BY last_activity DESC
                LIMIT $2
                """,
                status,
                limit,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id, title, agent_id, status, goal, model, provider, context_budget, created_at, last_activity
                FROM sessions
                ORDER BY last_activity DESC
                LIMIT $1
                """,
                limit,
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
            created_at=row["created_at"],
            last_activity=row["last_activity"],
        )

    @classmethod
    def reset(cls) -> None:
        """Reset the global SessionManager singleton to a fresh instance."""
        global session_manager
        session_manager = cls()


session_manager = SessionManager()
