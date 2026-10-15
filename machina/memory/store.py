"""Memory store — async CRUD for long-term memories using asyncpg.

Follows the same patterns as ContextManager: asyncpg, typed dataclasses,
structured logging, and audit logging.
"""

from __future__ import annotations

import importlib as _importlib
import logging
import sys as _sys
import uuid
from typing import Any

from machina.core.provider import audit_log
from machina.core.serialization import embedding_to_str, str_to_embedding
from machina.db.lazy import LazyDb, parse_command_count
from machina.memory.embeddings import embedding_space_id
from machina.memory.identity import MemoryProvenance, identity_gate
from machina.memory.models import MemoryEntry
from machina.memory.redaction import SecretRedactor
from machina.observability.diagnostics import record_failure
from machina.security.secrets import get_secret

__all__ = ["MemoryStore", "memory_store"]

logger = logging.getLogger(__name__)

db = LazyDb()

# H08: additive runtime migration for existing databases. ``schema.sql`` never
# runs on an already-initialized database, and machina/db/connection.py
# (``initialize_runtime_schema``) is owned by another lane, so this store
# applies the same idempotent DDL lazily before the first embedding-backed
# operation (tag on write / space filter on read). Keep in sync with the
# memories section of schema.sql; the M1 report specifies the matching
# connection.py addition for the boot-time migration path.
_MEMORY_EMBEDDING_SPACE_MIGRATION = """
ALTER TABLE memories ADD COLUMN IF NOT EXISTS embedding_space TEXT;
CREATE INDEX IF NOT EXISTS idx_memories_embedding_space ON memories(embedding_space);
"""


def _approval_gate():
    """SEAM: resolve the memory approval gate without a store↔approval cycle.

    ``machina.memory.approval`` imports ``memory_store`` at module level, so
    this module cannot import it back; the ``sys.modules``/``importlib``
    binding mirrors the leaf-core seam idiom and keeps the deferred-import
    census unchanged. By the time any write runs, ``machina.memory`` has
    finished importing, so the singleton is defined.
    """
    module = _sys.modules.get("machina.memory.approval")
    if module is None:
        module = _importlib.import_module("machina.memory.approval")
    return module.memory_approval_gate


class MemoryStore:
    """CRUD for long-term memories stored in PostgreSQL with pgvector."""

    def __init__(self) -> None:
        self._redactor = SecretRedactor()
        # H08: None until the additive embedding-space migration is resolved;
        # True = column available, False = DDL unavailable (pre-H08 shapes).
        self._embedding_space_ready: bool | None = None

    async def _ensure_embedding_space(self, executor: Any, *, durable: bool) -> bool:
        """Apply the H08 additive migration once; report column availability.

        ``durable`` marks DDL applied over the base pool handle (autocommit),
        so a later rollback of a caller-owned transaction cannot invalidate
        it. The DDL is idempotent and safe to race across processes. A failed
        DDL never raises: the store degrades to the pre-H08 shapes (untagged
        writes, no query filter) instead of failing memory operations.
        """
        if self._embedding_space_ready is not None:
            return self._embedding_space_ready
        try:
            await executor.execute(_MEMORY_EMBEDDING_SPACE_MIGRATION)
        except Exception as e:
            record_failure("memory_store.embedding_space_migration", e)
            logger.warning(
                "memory embedding-space migration unavailable; keeping pre-H08 vector behavior: %s",
                e,
            )
            if durable:
                self._embedding_space_ready = False
            return False
        if durable:
            self._embedding_space_ready = True
        return True

    async def add(
        self,
        session_id: uuid.UUID | None,
        agent_id: str,
        content: str,
        category: str,
        importance: float = 0.5,
        embedding: list[float] | None = None,
        embedding_space: str | None = None,
        explicitly_important: bool = False,
        base_strength: float = 1.0,
        connection: Any | None = None,
        source_agent: str | None = None,
        parent_memory_id: uuid.UUID | None = None,
        bypass_approval: bool = False,
    ) -> MemoryEntry:
        """Add a new memory entry. Secrets are redacted before storage.

        When the approval gate is enabled (``memory_approval_enabled`` config
        or an explicitly enabled gate), the write is routed to
        ``pending_memories`` instead of ``memories`` and the returned entry
        mirrors the pending record (its ``id`` is the pending id) — it is not
        yet durable; ``approve()``/``reject()`` complete the flow. Internal
        completions of that flow (``approve``/``approve_all``), and the
        shared-bus delivery copy, pass ``bypass_approval=True``: they own
        their durable write semantics (an open transaction / a receipt that
        must reference the real copy).

        The IdentityGate validates the memory against the agent's belief
        model before storage. If validation fails, the memory is still
        stored but flagged as quarantined.

        H08: a vector write also persists the embedding-space identifier of
        the embedder that produced it (``embedding_space`` override, else the
        shared embedder's current space). Dense reads only compare rows from
        the same space; rows written before this column existed stay usable
        through the documented ``embedding_space IS NULL`` fallback.
        """
        if not bypass_approval:
            gate = _approval_gate()
            if gate.is_enabled():
                pending = await gate.submit(
                    content=content,
                    category=category,
                    importance=importance,
                    agent_id=agent_id,
                    session_id=session_id,
                    explicitly_important=explicitly_important,
                    base_strength=base_strength,
                    embedding=embedding,
                )
                logger.info(
                    "Memory write routed to approval gate: pending %s (agent %s)",
                    pending.id,
                    agent_id,
                )
                return MemoryEntry(
                    id=pending.id,
                    session_id=session_id,
                    agent_id=agent_id,
                    content=pending.content,
                    category=category,
                    importance=importance,
                    embedding=embedding,
                    explicitly_important=explicitly_important,
                    base_strength=base_strength,
                )

        # Redact secrets from content before storing
        redaction_result = self._redactor.redact(content)
        content = redaction_result.text

        # The ID used for signing must be the ID actually inserted.
        memory_id = uuid.uuid4()
        source = source_agent or agent_id
        provenance = None
        if get_secret("MACHINA_PROVENANCE_KEY"):
            provenance = MemoryProvenance(
                memory_id=memory_id,
                source_agent=source,
                signature=MemoryProvenance.compute_signature(memory_id, content, source),
                parent_memory_id=parent_memory_id,
            )

        # Validate against the identity gate of the *target* agent (the memory's
        # owner). The memory's attribution stays with the source, so a shared
        # delivery without valid provenance is quarantined instead of accepted.
        pre_memory = MemoryEntry(
            id=memory_id,
            session_id=session_id,
            agent_id=source,
            content=content,
            category=category,
            importance=importance,
        )
        validation = await identity_gate.validate_incoming(
            agent_id, pre_memory, provenance, connection=connection
        )
        quarantined = not validation.is_valid

        embedding_str = None
        space_id = None
        if embedding is not None:
            embedding_str = embedding_to_str(embedding)
            space_id = embedding_space if embedding_space is not None else embedding_space_id()
            if space_id is not None:
                space_executor = connection if connection is not None else db
                if not await self._ensure_embedding_space(
                    space_executor, durable=connection is None
                ):
                    # Legacy database where the additive DDL cannot run: keep
                    # the pre-H08 write shape rather than fail the write.
                    space_id = None

        async def insert(executor: Any) -> Any:
            columns = (
                "id, session_id, agent_id, content, category, importance, "
                "embedding, explicitly_important, base_strength, quarantined"
            )
            values = "$1, $2, $3, $4, $5, $6, $7, $8, $9, $10"
            params: list[Any] = [
                memory_id,
                session_id,
                agent_id,
                content,
                category,
                importance,
                embedding_str,
                explicitly_important,
                base_strength,
                quarantined,
            ]
            if space_id is not None:
                columns += ", embedding_space"
                values += ", $11"
                params.append(space_id)
            row = await executor.fetchrow(
                f"""
            INSERT INTO memories ({columns})
            VALUES ({values})
            RETURNING id, session_id, agent_id, content, category, importance,
                      created_at, last_accessed, access_count, embedding,
                      explicitly_important, base_strength, quarantined
            """,  # nosec B608 # Fixed column/placeholder fragments; values are bound parameters.
                *params,
            )
            if provenance is not None:
                try:
                    await executor.execute(
                        """
                    INSERT INTO memory_provenance
                        (memory_id, source_agent, signature, parent_memory_id)
                    VALUES ($1, $2, $3, $4)
                    """,
                        memory_id,
                        source,
                        provenance.signature,
                        parent_memory_id,
                    )
                except AttributeError:
                    # Mocks without execute (e.g. CapturingMock with fetchrow only)
                    pass
            return row

        if connection is not None:
            row = await insert(connection)
        else:
            # Use db directly for compat with mocks; when provenance exists
            # try transactional acquire, falling back to direct on mocks.
            try:
                acquire = getattr(db, "acquire", None)
                if provenance is not None and callable(acquire):
                    async with db.acquire() as conn:
                        try:
                            async with conn.transaction():
                                row = await insert(conn)
                        except AttributeError:
                            row = await insert(conn)
                else:
                    row = await insert(db)
            except (TypeError, AttributeError):
                row = await insert(db)
        memory = self._row_to_entry(row)
        audit_log(
            "memory_add",
            memory_id=str(memory.id),
            agent_id=agent_id,
            category=category,
            importance=importance,
            quarantined=quarantined,
            validation_reason=validation.reason,
        )
        return memory

    async def get(
        self, memory_id: uuid.UUID, include_quarantined: bool = False
    ) -> MemoryEntry | None:
        """Get a memory by ID."""
        if include_quarantined:
            row = await db.fetchrow(
                """
            SELECT id, session_id, agent_id, content, category, importance,
                   created_at, last_accessed, access_count, embedding,
                   explicitly_important, base_strength, quarantined
            FROM memories WHERE id = $1
            """,
                memory_id,
            )
        else:
            row = await db.fetchrow(
                """
            SELECT id, session_id, agent_id, content, category, importance,
                   created_at, last_accessed, access_count, embedding,
                   explicitly_important, base_strength, quarantined
            FROM memories WHERE id = $1 AND quarantined = FALSE
            """,
                memory_id,
            )
        if row is None:
            return None
        return self._row_to_entry(row)

    async def search(
        self,
        agent_id: str | None = None,
        category: str | None = None,
        limit: int = 20,
        offset: int = 0,
        session_id: uuid.UUID | None = None,
    ) -> list[MemoryEntry]:
        """Search memories with optional filters (fully parameterized)."""
        conditions = ["quarantined = FALSE"]
        params: list[Any] = []
        param_idx = 1

        if agent_id is not None:
            conditions.append(f"agent_id = ${param_idx}")
            params.append(agent_id)
            param_idx += 1

        if category is not None:
            conditions.append(f"category = ${param_idx}")
            params.append(category)
            param_idx += 1

        if session_id is not None:
            conditions.append(f"session_id = ${param_idx}")
            params.append(session_id)
            param_idx += 1

        where_clause = " AND ".join(conditions) if conditions else "TRUE"

        params.extend([limit, offset])
        rows = await db.fetch(
            f"""
            SELECT id, session_id, agent_id, content, category, importance,
                   created_at, last_accessed, access_count, embedding,
                   explicitly_important, base_strength, quarantined
            FROM memories
            WHERE {where_clause}
            ORDER BY importance DESC, created_at DESC
            LIMIT ${param_idx} OFFSET ${param_idx + 1}
            """,  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
            *params,
        )
        return [self._row_to_entry(row) for row in rows]

    async def search_by_embedding(
        self,
        embedding: list[float],
        agent_id: str | None = None,
        category: str | None = None,
        limit: int = 5,
        include_quarantined: bool = False,
        embedding_space: str | None = None,
    ) -> list[tuple[MemoryEntry, float]]:
        """Search memories by embedding similarity (cosine distance).

        Returns list of (MemoryEntry, similarity_score) tuples, sorted by
        similarity descending.

        H08: only rows from the same embedding space are comparable. Rows
        written by another backend/model are excluded at the SQL level (their
        stored ``embedding_space`` differs from the query's); rows written
        before the column existed (``NULL``) stay candidates through the
        documented compatibility fallback and are upgraded by re-embedding.
        When no space is resolvable (no embedder configured), the pre-H08
        unfiltered behavior is kept.
        """
        embedding_str = embedding_to_str(embedding)

        space_id = embedding_space if embedding_space is not None else embedding_space_id()
        if space_id is not None and not await self._ensure_embedding_space(db, durable=True):
            # Additive DDL unavailable on this database: keep pre-H08 reads.
            space_id = None

        conditions = ["embedding IS NOT NULL"]
        if not include_quarantined:
            conditions.append("quarantined = FALSE")
        params: list[Any] = [embedding_str, limit]
        param_idx = 3

        if agent_id is not None:
            conditions.append(f"agent_id = ${param_idx}")
            params.append(agent_id)
            param_idx += 1

        if category is not None:
            conditions.append(f"category = ${param_idx}")
            params.append(category)
            param_idx += 1

        if space_id is not None:
            conditions.append(f"(embedding_space = ${param_idx} OR embedding_space IS NULL)")
            params.append(space_id)
            param_idx += 1

        where_clause = " AND ".join(conditions)

        rows = await db.fetch(
            f"""
            SELECT id, session_id, agent_id, content, category, importance,
                   created_at, last_accessed, access_count, embedding,
                   explicitly_important, base_strength, quarantined,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM memories
            WHERE {where_clause}
            ORDER BY embedding <=> $1::vector
            LIMIT $2
            """,  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
            *params,
        )
        results = []
        for row in rows:
            entry = self._row_to_entry(row)
            similarity = row["similarity"]
            results.append((entry, similarity))
        return results

    async def delete(self, memory_id: uuid.UUID) -> bool:
        """Delete a memory by ID. Returns True if deleted."""
        result = await db.execute(
            "DELETE FROM memories WHERE id = $1",
            memory_id,
        )
        deleted = parse_command_count(result) > 0
        if deleted:
            audit_log("memory_delete", memory_id=str(memory_id))
        return deleted

    async def update_access(self, memory_id: uuid.UUID) -> None:
        """Update last_accessed and increment access_count.

        Quarantined memories are excluded, matching ``batch_update_access``:
        they are not retrievable, so they must not accrue access evidence.
        """
        await db.execute(
            """
            UPDATE memories
            SET last_accessed = now(), access_count = access_count + 1
            WHERE id = $1 AND quarantined = FALSE
            """,
            memory_id,
        )

    async def batch_update_access(self, memory_ids: list[uuid.UUID]) -> None:
        """Batch update last_accessed and increment access_count for multiple memories."""
        if not memory_ids:
            return
        await db.execute(
            """
            UPDATE memories
            SET last_accessed = now(), access_count = access_count + 1
            WHERE id = ANY($1::uuid[]) AND quarantined = FALSE
            """,
            memory_ids,
        )

    # Direct importance update; memory tests drive it, no production caller. Kept (R4-106).
    async def update_importance(self, memory_id: uuid.UUID, importance: float) -> None:
        """Update the importance score of a memory."""
        importance = max(0.0, min(1.0, importance))
        await db.execute(
            "UPDATE memories SET importance = $1 WHERE id = $2",
            importance,
            memory_id,
        )

    async def list_all(
        self,
        agent_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryEntry]:
        """List all memories with optional agent filter."""
        if agent_id:
            return await self.search(agent_id=agent_id, limit=limit, offset=offset)
        return await self.search(limit=limit, offset=offset)

    async def count(
        self,
        agent_id: str | None = None,
        session_id: uuid.UUID | None = None,
        exclude_quarantined: bool = True,
    ) -> int:
        """Count memories, optionally filtered by agent and session."""
        q_filter = " AND quarantined = FALSE" if exclude_quarantined else ""
        if agent_id is not None and session_id is not None:
            result = await db.fetchval(
                f"SELECT COUNT(*) FROM memories WHERE agent_id = $1 AND session_id = $2{q_filter}",  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
                agent_id,
                session_id,
            )
        elif session_id is not None:
            where = "WHERE session_id = $1" + q_filter
            result = await db.fetchval(
                f"SELECT COUNT(*) FROM memories {where}",  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
                session_id,
            )
        elif agent_id is not None:
            where = "WHERE agent_id = $1" + q_filter
            result = await db.fetchval(
                f"SELECT COUNT(*) FROM memories {where}",  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
                agent_id,
            )
        else:
            where = "WHERE quarantined = FALSE" if exclude_quarantined else ""
            result = await db.fetchval(f"SELECT COUNT(*) FROM memories {where}".strip())  # nosec B608 # Fixed SQL fragments; external values use bound parameters.
        return result or 0

    # Session-scoped deletion; memory tests drive it, no production caller. Kept (R4-106).
    async def delete_by_session(self, session_id: uuid.UUID) -> int:
        """Delete all memories associated with a session. Returns count deleted."""
        result = await db.execute(
            "DELETE FROM memories WHERE session_id = $1",
            session_id,
        )
        # Parse "DELETE N" format
        return parse_command_count(result)

    async def evict_weak_memories(
        self,
        threshold: float = 0.05,
        agent_id: str | None = None,
        max_memories: int | None = None,
    ) -> int:
        """Evict weak/unimportant memories to prevent unbounded growth.

        Args:
            threshold: Importance threshold below which memories are evicted.
            agent_id: Optional agent ID filter.
            max_memories: If set, evict down to this many memories per agent.

        Returns:
            Number of memories evicted.
        """
        if max_memories is not None:
            # Use a single CTE to atomically count and delete, avoiding race conditions
            # where concurrent inserts could cause over/under-deletion.
            # Quarantined and explicitly-important memories are preserved.
            if agent_id:
                result = await db.execute(
                    """
                    WITH target AS (
                        SELECT id FROM memories
                        WHERE agent_id = $1 AND quarantined = FALSE
                          AND explicitly_important = FALSE
                        ORDER BY importance ASC, explicitly_important ASC, access_count ASC, created_at ASC
                        LIMIT GREATEST((SELECT COUNT(*) FROM memories WHERE agent_id = $1 AND quarantined = FALSE AND explicitly_important = FALSE) - $2, 0)
                    )
                    DELETE FROM memories WHERE id IN (SELECT id FROM target)
                    """,
                    agent_id,
                    max_memories,
                )
            else:
                result = await db.execute(
                    """
                    WITH target AS (
                        SELECT id FROM memories
                        WHERE quarantined = FALSE AND explicitly_important = FALSE
                        ORDER BY importance ASC, explicitly_important ASC, access_count ASC, created_at ASC
                        LIMIT GREATEST((SELECT COUNT(*) FROM memories WHERE quarantined = FALSE AND explicitly_important = FALSE) - $1, 0)
                    )
                    DELETE FROM memories WHERE id IN (SELECT id FROM target)
                    """,
                    max_memories,
                )
            return parse_command_count(result)
        else:
            # Evict by importance threshold (quarantined + explicitly-important preserved)
            if agent_id:
                result = await db.execute(
                    "DELETE FROM memories WHERE agent_id = $1 AND importance < $2 AND quarantined = FALSE AND explicitly_important = FALSE",
                    agent_id,
                    threshold,
                )
            else:
                result = await db.execute(
                    "DELETE FROM memories WHERE importance < $1 AND quarantined = FALSE AND explicitly_important = FALSE",
                    threshold,
                )
            return parse_command_count(result)

    async def get_weak_memories(
        self,
        threshold: float = 0.05,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[MemoryEntry]:
        """Get memories with low importance (candidates for forgetting).

        This is a simple importance-based query. The full ForgettingModel
        computes time-decayed strength, but for DB-level filtering we use
        importance as a proxy.
        """
        if agent_id:
            rows = await db.fetch(
                """
                SELECT id, session_id, agent_id, content, category, importance,
                       created_at, last_accessed, access_count, embedding,
                       explicitly_important, base_strength, quarantined
                FROM memories
                WHERE agent_id = $1 AND importance < $2 AND quarantined = FALSE
                  AND explicitly_important = FALSE
                ORDER BY importance ASC, access_count ASC
                LIMIT $3
                """,
                agent_id,
                threshold,
                limit,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id, session_id, agent_id, content, category, importance,
                       created_at, last_accessed, access_count, embedding,
                       explicitly_important, base_strength, quarantined
                FROM memories
                WHERE importance < $1 AND quarantined = FALSE
                  AND explicitly_important = FALSE
                ORDER BY importance ASC, access_count ASC
                LIMIT $2
                """,
                threshold,
                limit,
            )
        return [self._row_to_entry(row) for row in rows]

    def _row_to_entry(self, row: Any) -> MemoryEntry:
        """Convert a database row to a MemoryEntry."""
        embedding = None
        if row["embedding"] is not None:
            embedding = str_to_embedding(row["embedding"])

        return MemoryEntry(
            id=row["id"],
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            content=row["content"],
            category=row["category"],
            importance=row["importance"],
            created_at=row["created_at"],
            last_accessed=row["last_accessed"],
            access_count=row["access_count"],
            embedding=embedding,
            explicitly_important=row["explicitly_important"],
            base_strength=row["base_strength"],
            quarantined=row.get("quarantined", False),
        )


# Global singleton
memory_store = MemoryStore()
