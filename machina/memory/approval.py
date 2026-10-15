"""Memory approval gate — human-in-the-loop review for memory writes.

When enabled, memory writes go to a 'pending' state instead of being
directly persisted. The user must explicitly approve or reject pending
memories via CLI commands (`machina memory-pending`, `machina memory-approve`,
`machina memory-reject`).

The approval gate also integrates secret redaction: any secrets detected
in memory content are redacted before the content is shown to the user
for review.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from machina.core.config import config
from machina.core.provider import audit_log
from machina.core.serialization import embedding_to_str, str_to_embedding
from machina.db.lazy import LazyDb
from machina.memory.embeddings import embedding_space_id
from machina.memory.models import MemoryEntry
from machina.memory.redaction import SecretRedactor
from machina.memory.store import memory_store
from machina.observability.diagnostics import record_failure

__all__ = [
    "ApprovalStatus",
    "PendingMemory",
    "MemoryApprovalGate",
    "memory_approval_gate",
]

logger = logging.getLogger(__name__)

db = LazyDb()

# H08 follow-up: additive runtime migration for existing databases.
# ``schema.sql`` never runs on an already-initialized database, so the gate
# applies the same idempotent DDL lazily before the first embedding-backed
# pending write/read. Keep in sync with the pending_memories section of
# schema.sql; ``machina/db/connection.py`` is not the migration path for this
# table (contrast H08's ``memories.embedding_space`` boot migration).
_PENDING_EMBEDDING_SPACE_MIGRATION = """
ALTER TABLE pending_memories ADD COLUMN IF NOT EXISTS embedding_space TEXT;
"""


class ApprovalStatus(StrEnum):
    """Status of a pending memory approval."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class PendingMemory:
    """A memory awaiting user approval.

    Attributes:
        id: Unique pending-memory identifier.
        memory_id: The associated MemoryEntry ID (once approved).
        content: The (redacted) memory content.
        category: Memory category.
        importance: Importance score.
        agent_id: Agent that created the memory.
        session_id: Associated session.
        redactions: List of secret patterns that were redacted.
        status: Current approval status.
        created_at: When the pending entry was created.
        reviewed_at: When the user approved/rejected.
        review_note: Optional note from the reviewer.
    """

    id: uuid.UUID
    memory_id: uuid.UUID | None
    content: str
    category: str
    importance: float = 0.5
    agent_id: str = "harness"
    session_id: uuid.UUID | None = None
    redactions: list[str] = field(default_factory=list)
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    reviewed_at: datetime | None = None
    review_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "id": str(self.id),
            "memory_id": str(self.memory_id) if self.memory_id else None,
            "content": self.content,
            "category": self.category,
            "importance": self.importance,
            "agent_id": self.agent_id,
            "session_id": str(self.session_id) if self.session_id else None,
            "redactions": self.redactions,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "review_note": self.review_note,
        }


class MemoryApprovalGate:
    """Human-in-the-loop approval gate for memory writes.

    When enabled, memory writes are intercepted and stored as pending
    entries. The user must approve or reject them via CLI.

    The gate also performs secret redaction before storing pending
    content, so sensitive data is never shown in plain text during
    review.
    """

    def __init__(
        self,
        enabled: bool = False,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.enabled = enabled
        self.redactor = redactor or SecretRedactor()
        # H08 follow-up: None until the additive pending-space migration is
        # resolved; True = column available, False = pre-migration shapes.
        self._pending_embedding_space_ready: bool | None = None

    async def _ensure_pending_embedding_space(self, executor: Any, *, durable: bool) -> bool:
        """Apply the pending_memories space migration once; report availability.

        Mirrors ``MemoryStore._ensure_embedding_space`` (H08): ``durable``
        marks DDL applied over the base pool handle (autocommit), so a later
        rollback of a caller-owned transaction cannot invalidate it. The DDL
        is idempotent and safe to race across processes. A failed DDL never
        raises: the gate degrades to the pre-migration shapes (submit stores
        no space, approval follows the configured embedder).
        """
        if self._pending_embedding_space_ready is not None:
            return self._pending_embedding_space_ready
        try:
            await executor.execute(_PENDING_EMBEDDING_SPACE_MIGRATION)
        except Exception as e:
            record_failure("memory_approval.embedding_space_migration", e)
            logger.warning(
                "pending_memories embedding-space migration unavailable; "
                "keeping pre-migration approval behavior: %s",
                e,
            )
            if durable:
                self._pending_embedding_space_ready = False
            return False
        if durable:
            self._pending_embedding_space_ready = True
        return True

    def is_enabled(self) -> bool:
        """Effective gate state: explicit flag OR ``memory_approval_enabled``.

        The constructor flag stays the direct/test control; the config key is
        the production switch (default OFF preserves direct writes). Both the
        write router (``MemoryStore.add``) and ``submit``'s auto-approve use
        this single resolution so an enabled request can never be
        auto-approved by a default-constructed singleton.
        """
        if self.enabled:
            return True
        return bool(config.get("memory_approval_enabled"))

    async def submit(
        self,
        content: str,
        category: str,
        importance: float = 0.5,
        agent_id: str = "harness",
        session_id: uuid.UUID | None = None,
        explicitly_important: bool = False,
        base_strength: float = 1.0,
        embedding: list[float] | None = None,
    ) -> PendingMemory:
        """Submit a memory for approval.

        If the gate is disabled, this still creates a pending record
        but immediately approves it (for audit purposes).

        Returns the PendingMemory entry.
        """
        # Step 1: Redact secrets
        redaction_result = self.redactor.redact(content)
        redacted_content = redaction_result.text
        redactions = redaction_result.redactions

        if redactions:
            logger.info(
                "Redacted %d secret patterns from memory content: %s",
                len(redactions),
                redactions,
            )

        # Step 2: Create pending entry. An empty embedding is not a usable
        # vector ("[]" would be stored as an invalid vector literal) — treat
        # it exactly like None (N13).
        pending_id = uuid.uuid4()
        now = datetime.now(UTC)
        embedding_str = embedding_to_str(embedding) if embedding else None

        # H08 follow-up: the pending row carries the space of the vector it
        # was submitted with, so approval completes it under the ORIGINAL
        # space even if the configured embedder changes while it is pending.
        # No vector (or unavailable DDL) keeps the pre-migration INSERT shape.
        space_id = embedding_space_id() if embedding else None
        if space_id is not None and not await self._ensure_pending_embedding_space(
            db, durable=True
        ):
            space_id = None

        columns = (
            "id, content, category, importance, agent_id, session_id, "
            "redactions, status, created_at, explicitly_important, base_strength"
        )
        values = "$1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11"
        params: list[Any] = [
            pending_id,
            redacted_content,
            category,
            importance,
            agent_id,
            session_id,
            redactions,
            ApprovalStatus.PENDING.value,
            now,
            explicitly_important,
            base_strength,
        ]
        if space_id is not None:
            # Space identity before the vector keeps the embedding the
            # trailing bound parameter of the INSERT.
            columns += ", embedding_space"
            values += ", $12"
            params.append(space_id)
        columns += ", embedding"
        values += f", ${len(params) + 1}"
        params.append(embedding_str)

        insert_sql = f"INSERT INTO pending_memories ({columns}) VALUES ({values})"  # nosec B608 # Fixed column/placeholder fragments; values are bound parameters.
        await db.execute(
            insert_sql,
            *params,
        )

        pending = PendingMemory(
            id=pending_id,
            memory_id=None,
            content=redacted_content,
            category=category,
            importance=importance,
            agent_id=agent_id,
            session_id=session_id,
            redactions=redactions,
            status=ApprovalStatus.PENDING,
            created_at=now,
        )

        audit_log(
            "memory_pending_create",
            pending_id=str(pending.id),
            agent_id=agent_id,
            category=category,
            redactions=redactions,
        )

        # Step 3: If gate is disabled, auto-approve
        if not self.is_enabled():
            await self.approve(pending_id, auto_approved=True)

        return pending

    async def approve(
        self,
        pending_id: uuid.UUID,
        review_note: str = "",
        auto_approved: bool = False,
    ) -> MemoryEntry | None:
        """Approve a pending memory.

        Creates the actual MemoryEntry in the memories table and
        updates the pending record's status.

        Returns the created MemoryEntry, or None if not found.
        """
        # H08 follow-up: resolve the additive pending-space migration so the
        # completion can inherit the submission space (degrade: pre-migration
        # rows and databases follow the configured embedder, as before).
        space_ready = await self._ensure_pending_embedding_space(db, durable=True)
        space_column = ", embedding_space" if space_ready else ""
        # Lock the pending row and commit its memory in the same transaction.
        # A failed update or a concurrent approval cannot leave an orphan.
        async with db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""
                    SELECT id, content, category, importance, agent_id, session_id,
                           redactions, status, created_at, explicitly_important,
                           base_strength, embedding{space_column}
                    FROM pending_memories WHERE id = $1 FOR UPDATE
                    """,  # nosec B608 # Fixed column fragments; values are bound parameters.
                    pending_id,
                )
                if row is None or row["status"] != ApprovalStatus.PENDING.value:
                    return None
                memory = await memory_store.add(
                    session_id=row["session_id"],
                    agent_id=row["agent_id"],
                    content=row["content"],
                    category=row["category"],
                    importance=row["importance"],
                    embedding=str_to_embedding(row["embedding"])
                    if row.get("embedding") is not None
                    else None,
                    embedding_space=row.get("embedding_space"),
                    explicitly_important=row["explicitly_important"],
                    base_strength=row["base_strength"],
                    connection=conn,
                    # Completing an approved row must never re-enter the gate.
                    bypass_approval=True,
                )
                await conn.execute(
                    """
                    UPDATE pending_memories
                    SET status = $2, memory_id = $3, reviewed_at = $4, review_note = $5
                    WHERE id = $1
                    """,
                    pending_id,
                    ApprovalStatus.APPROVED.value,
                    memory.id,
                    datetime.now(UTC),
                    review_note,
                )

        audit_log(
            "memory_approved",
            pending_id=str(pending_id),
            memory_id=str(memory.id),
            auto_approved=auto_approved,
        )

        logger.info(
            "Approved pending memory %s → memory %s",
            pending_id,
            memory.id,
        )
        return memory

    async def reject(
        self,
        pending_id: uuid.UUID,
        review_note: str = "",
    ) -> bool:
        """Reject a pending memory.

        Updates the pending record's status to 'rejected'.
        No MemoryEntry is created.

        Returns True if the pending record was found and rejected.
        """
        # Lock the row and update in the same transaction to prevent
        # race conditions where concurrent approvals/rejections could
        # leave the record in an inconsistent state.
        async with db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status FROM pending_memories WHERE id = $1 FOR UPDATE",
                    pending_id,
                )
                if row is None:
                    logger.warning("Pending memory %s not found for rejection", pending_id)
                    return False

                if row["status"] != ApprovalStatus.PENDING.value:
                    logger.warning("Pending memory %s already %s", pending_id, row["status"])
                    return False

                now = datetime.now(UTC)
                await conn.execute(
                    """
                    UPDATE pending_memories
                    SET status = $2, reviewed_at = $3, review_note = $4
                    WHERE id = $1
                    """,
                    pending_id,
                    ApprovalStatus.REJECTED.value,
                    now,
                    review_note,
                )

        audit_log(
            "memory_rejected",
            pending_id=str(pending_id),
            review_note=review_note,
        )

        logger.info("Rejected pending memory %s", pending_id)
        return True

    async def list_pending(
        self,
        status: ApprovalStatus = ApprovalStatus.PENDING,
        limit: int = 50,
    ) -> list[PendingMemory]:
        """List pending memories filtered by status."""
        rows = await db.fetch(
            """
            SELECT id, memory_id, content, category, importance, agent_id,
                   session_id, redactions, status, created_at, reviewed_at,
                   review_note
            FROM pending_memories
            WHERE status = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            status.value,
            limit,
        )
        return [self._row_to_pending(row) for row in rows]

    async def approve_all(self, agent_id: str | None = None) -> int:
        """Approve all pending memories, optionally filtered by agent.

        Uses a batch UPDATE to mark all pending records as approved in a
        single query, then creates memory entries for each.

        Returns the count of approved memories.
        """
        space_ready = await self._ensure_pending_embedding_space(db, durable=True)
        async with db.acquire() as conn:
            async with conn.transaction():
                return await self._approve_all_locked(conn, agent_id, space_ready=space_ready)

    async def _approve_all_locked(
        self, conn: Any, agent_id: str | None, *, space_ready: bool
    ) -> int:
        """Approve a locked batch; the caller owns the transaction."""
        space_column = ", embedding_space" if space_ready else ""
        if agent_id:
            rows = await conn.fetch(
                f"""
                SELECT id, content, category, importance, agent_id, session_id,
                       redactions, created_at, explicitly_important, base_strength,
                       embedding{space_column}
                FROM pending_memories
                WHERE status = $1 AND agent_id = $2
                ORDER BY created_at ASC FOR UPDATE
                """,  # nosec B608 # Fixed column fragments; values are bound parameters.
                ApprovalStatus.PENDING.value,
                agent_id,
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT id, content, category, importance, agent_id, session_id,
                       redactions, created_at, explicitly_important, base_strength,
                       embedding{space_column}
                FROM pending_memories
                WHERE status = $1
                ORDER BY created_at ASC FOR UPDATE
                """,  # nosec B608 # Fixed column fragments; values are bound parameters.
                ApprovalStatus.PENDING.value,
            )

        if not rows:
            return 0

        now = datetime.now(UTC)
        pending_ids = [row["id"] for row in rows]
        memory_ids: list[uuid.UUID] = []
        for row in rows:
            memory = await memory_store.add(
                session_id=row["session_id"],
                agent_id=row["agent_id"],
                content=row["content"],
                category=row["category"],
                importance=row["importance"],
                embedding=str_to_embedding(row["embedding"])
                if row.get("embedding") is not None
                else None,
                embedding_space=row.get("embedding_space"),
                explicitly_important=row["explicitly_important"],
                base_strength=row["base_strength"],
                connection=conn,
                # Completing approved rows must never re-enter the gate.
                bypass_approval=True,
            )
            memory_ids.append(memory.id)

        # Single batch UPDATE to mark all pending records as approved
        await conn.execute(
            """
            UPDATE pending_memories
            SET status = 'approved',
                memory_id = approved_memory_ids.mem_id,
                reviewed_at = $3,
                review_note = ''
            FROM (
                SELECT unnest($1::uuid[]) AS pending_id,
                       unnest($2::uuid[]) AS mem_id
            ) AS approved_memory_ids
            WHERE pending_memories.id = approved_memory_ids.pending_id
            """,
            pending_ids,
            memory_ids,
            now,
        )

        audit_log(
            "memory_approve_all",
            count=len(pending_ids),
            agent_id=agent_id,
        )

        return len(pending_ids)

    async def reject_all(
        self,
        agent_id: str | None = None,
        review_note: str = "",
    ) -> int:
        """Reject all pending memories, optionally filtered by agent.

        Uses a single batch UPDATE to mark all pending records as rejected.

        Returns the count of rejected memories.
        """
        now = datetime.now(UTC)
        if agent_id:
            result = await db.execute(
                """
                UPDATE pending_memories
                SET status = $2, reviewed_at = $3, review_note = $4
                WHERE status = $1 AND agent_id = $5
                """,
                ApprovalStatus.PENDING.value,
                ApprovalStatus.REJECTED.value,
                now,
                review_note,
                agent_id,
            )
        else:
            result = await db.execute(
                """
                UPDATE pending_memories
                SET status = $2, reviewed_at = $3, review_note = $4
                WHERE status = $1
                """,
                ApprovalStatus.PENDING.value,
                ApprovalStatus.REJECTED.value,
                now,
                review_note,
            )

        from machina.db.connection import parse_command_count

        count = parse_command_count(result)
        if count > 0:
            audit_log(
                "memory_reject_all",
                count=count,
                agent_id=agent_id,
            )
        return count

    async def get_stats(self) -> dict[str, int]:
        """Get counts of pending memories by status."""
        rows = await db.fetch(
            """
            SELECT status, COUNT(*) as count
            FROM pending_memories
            GROUP BY status
            """,
        )
        stats = {"pending": 0, "approved": 0, "rejected": 0}
        for row in rows:
            stats[row["status"]] = row["count"]
        return stats

    def _row_to_pending(self, row: Any) -> PendingMemory:
        """Convert a database row to a PendingMemory."""
        return PendingMemory(
            id=row["id"],
            memory_id=row["memory_id"],
            content=row["content"],
            category=row["category"],
            importance=row["importance"],
            agent_id=row["agent_id"],
            session_id=row["session_id"],
            redactions=row["redactions"] or [],
            status=ApprovalStatus(row["status"]),
            created_at=row["created_at"],
            reviewed_at=row["reviewed_at"],
            review_note=row["review_note"] or "",
        )


# Global singleton
memory_approval_gate = MemoryApprovalGate()
