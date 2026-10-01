"""Session manager — create, resume, and track agent sessions."""
from __future__ import annotations

import logging
import uuid
from typing import Optional

import asyncpg
import msgpack
from cachetools import TTLCache

from ah.db.connection import db
from ah.core.models import Session

__all__ = ["Session", "SessionManager", "session_manager"]

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages agent sessions in PostgreSQL with a 5-second LRU cache."""

    def __init__(self) -> None:
        # TTLCache: max 128 sessions, 5-second TTL
        self._cache: TTLCache = TTLCache(maxsize=128, ttl=5)

    def _cache_get(self, session_id: uuid.UUID) -> Session | None:
        return self._cache.get(session_id)

    def _cache_put(self, session: Session) -> None:
        self._cache[session.id] = session

    def _cache_invalidate(self, session_id: uuid.UUID) -> None:
        self._cache.pop(session_id, None)

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
        self._cache_put(session)
        return session

    async def get(self, session_id: uuid.UUID) -> Session | None:
        """Get a session by ID (cached for 5 seconds)."""
        cached = self._cache_get(session_id)
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
        self._cache_put(session)
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
        self._cache_put(session)
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
        self._cache_invalidate(session_id)

    async def update_activity(self, session_id: uuid.UUID) -> None:
        """Touch last_activity timestamp."""
        await db.execute(
            "UPDATE sessions SET last_activity = now() WHERE id = $1",
            session_id,
        )
        self._cache_invalidate(session_id)

    async def set_goal(self, session_id: uuid.UUID, goal: str) -> None:
        """Update session goal."""
        await db.execute(
            "UPDATE sessions SET goal = $2, last_activity = now() WHERE id = $1",
            session_id,
            goal,
        )
        self._cache_invalidate(session_id)

    async def set_status(self, session_id: uuid.UUID, status: str) -> None:
        """Update session status."""
        await db.execute(
            "UPDATE sessions SET status = $2 WHERE id = $1",
            session_id,
            status,
        )
        self._cache_invalidate(session_id)

    async def archive(self, session_id: uuid.UUID) -> None:
        """Archive a session."""
        await db.execute(
            "UPDATE sessions SET status = 'archived' WHERE id = $1",
            session_id,
        )
        self._cache_invalidate(session_id)

    async def list_sessions(
        self, status: str | None = None, limit: int = 20
    ) -> list[Session]:
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
        if row["state_msgpack"]:
            state = msgpack.unpackb(row["state_msgpack"], raw=False)
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
