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
from datetime import datetime
from enum import StrEnum
from typing import Any

from ah.core.provider import audit_log
from ah.db.connection import db
from ah.memory.models import MemoryEntry
from ah.memory.redaction import SecretRedactor

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
    created_at: datetime = field(default_factory=datetime.utcnow)
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
        now = datetime.utcnow()

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
            None,  # embedding stored separately if needed
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
        # Step 1: Get the pending record
        row = await db.fetchrow(
            """
            SELECT id, content, category, importance, agent_id, session_id,
                   redactions, status, created_at, explicitly_important,
                   base_strength
            FROM pending_memories WHERE id = $1
            """,
            pending_id,
        )
        if row is None:
            logger.warning("Pending memory %s not found for approval", pending_id)
            return None

        if row["status"] != ApprovalStatus.PENDING.value:
            logger.warning("Pending memory %s already %s", pending_id, row["status"])
            return None

        # Step 2: Create the actual memory entry
        from ah.memory.store import memory_store

        memory = await memory_store.add(
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            content=row["content"],
            category=row["category"],
            importance=row["importance"],
            explicitly_important=row["explicitly_important"],
            base_strength=row["base_strength"],
        )

        # Step 3: Update pending record
        now = datetime.utcnow()
        await db.execute(
            """
            UPDATE pending_memories
            SET status = $2, memory_id = $3, reviewed_at = $4, review_note = $5
            WHERE id = $1
            """,
            pending_id,
            ApprovalStatus.APPROVED.value,
            memory.id,
            now,
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
        row = await db.fetchrow(
            "SELECT status FROM pending_memories WHERE id = $1",
            pending_id,
        )
        if row is None:
            logger.warning("Pending memory %s not found for rejection", pending_id)
            return False

        if row["status"] != ApprovalStatus.PENDING.value:
            logger.warning("Pending memory %s already %s", pending_id, row["status"])
            return False

        now = datetime.utcnow()
        await db.execute(
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

    async def get_pending(self, pending_id: uuid.UUID) -> PendingMemory | None:
        """Get a single pending memory by ID."""
        row = await db.fetchrow(
            """
            SELECT id, memory_id, content, category, importance, agent_id,
                   session_id, redactions, status, created_at, reviewed_at,
                   review_note
            FROM pending_memories WHERE id = $1
            """,
            pending_id,
        )
        if row is None:
            return None
        return self._row_to_pending(row)

    async def approve_all(self, agent_id: str | None = None) -> int:
        """Approve all pending memories, optionally filtered by agent.

        Returns the count of approved memories.
        """
        if agent_id:
            rows = await db.fetch(
                """
                SELECT id FROM pending_memories
                WHERE status = $1 AND agent_id = $2
                ORDER BY created_at ASC
                """,
                ApprovalStatus.PENDING.value,
                agent_id,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id FROM pending_memories
                WHERE status = $1
                ORDER BY created_at ASC
                """,
                ApprovalStatus.PENDING.value,
            )

        count = 0
        for row in rows:
            result = await self.approve(row["id"], auto_approved=True)
            if result is not None:
                count += 1

        return count

    async def reject_all(
        self,
        agent_id: str | None = None,
        review_note: str = "",
    ) -> int:
        """Reject all pending memories, optionally filtered by agent.

        Returns the count of rejected memories.
        """
        if agent_id:
            rows = await db.fetch(
                """
                SELECT id FROM pending_memories
                WHERE status = $1 AND agent_id = $2
                ORDER BY created_at ASC
                """,
                ApprovalStatus.PENDING.value,
                agent_id,
            )
        else:
            rows = await db.fetch(
                """
                SELECT id FROM pending_memories
                WHERE status = $1
                ORDER BY created_at ASC
                """,
                ApprovalStatus.PENDING.value,
            )

        count = 0
        for row in rows:
            if await self.reject(row["id"], review_note=review_note):
                count += 1

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
