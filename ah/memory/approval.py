"""Memory approval gate — human-in-the-loop review for memory writes.

When enabled, memory writes go to a 'pending' state instead of being
directly persisted. The user must explicitly approve or reject pending
memories via CLI commands (`ah memory-pending`, `ah memory-approve`,
`ah memory-reject`).

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

from ah.core.provider import audit_log
from ah.core.serialization import embedding_to_str, str_to_embedding
from ah.db.connection import db
from ah.memory.models import MemoryEntry
from ah.memory.redaction import SecretRedactor
from ah.memory.store import memory_store

__all__ = [
    "ApprovalStatus",
    "PendingMemory",
    "MemoryApprovalGate",
    "memory_approval_gate",
]

logger = logging.getLogger(__name__)


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

        # Step 2: Create pending entry
        pending_id = uuid.uuid4()
        now = datetime.now(UTC)

        await db.execute(
            """
            INSERT INTO pending_memories (
                id, content, category, importance, agent_id, session_id,
                redactions, status, created_at, explicitly_important,
                base_strength, embedding
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
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
            embedding_to_str(embedding) if embedding is not None else None,
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
        if not self.enabled:
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
        # Lock the pending row and commit its memory in the same transaction.
        # A failed update or a concurrent approval cannot leave an orphan.
        async with db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT id, content, category, importance, agent_id, session_id,
                           redactions, status, created_at, explicitly_important,
                           base_strength, embedding
                    FROM pending_memories WHERE id = $1 FOR UPDATE
                    """,
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
                    explicitly_important=row["explicitly_important"],
                    base_strength=row["base_strength"],
                    connection=conn,
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
        async with db.acquire() as conn:
            async with conn.transaction():
                return await self._approve_all_locked(conn, agent_id)

    async def _approve_all_locked(self, conn: Any, agent_id: str | None) -> int:
        """Approve a locked batch; the caller owns the transaction."""
        if agent_id:
            rows = await conn.fetch(
                """
                SELECT id, content, category, importance, agent_id, session_id,
                       redactions, created_at, explicitly_important, base_strength, embedding
                FROM pending_memories
                WHERE status = $1 AND agent_id = $2
                ORDER BY created_at ASC FOR UPDATE
                """,
                ApprovalStatus.PENDING.value,
                agent_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, content, category, importance, agent_id, session_id,
                       redactions, created_at, explicitly_important, base_strength, embedding
                FROM pending_memories
                WHERE status = $1
                ORDER BY created_at ASC FOR UPDATE
                """,
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
                explicitly_important=row["explicitly_important"],
                base_strength=row["base_strength"],
                connection=conn,
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

        from ah.db.connection import parse_command_count

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
