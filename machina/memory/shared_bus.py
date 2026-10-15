"""Durable, recipient-gated delivery of signed memories between agents."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from machina.core.provider import audit_log
from machina.db.connection import db, parse_command_count
from machina.memory.approval import PendingMemory
from machina.memory.identity import MemoryProvenance, identity_gate
from machina.memory.models import MemoryEntry
from machina.memory.store import memory_store

DeliveryStatus = Literal["accepted", "rejected", "quarantined"]


@dataclass(frozen=True)
class DeliveryReceipt:
    source_memory_id: uuid.UUID
    recipient_agent: str
    target_memory_id: uuid.UUID | None
    status: DeliveryStatus
    reason: str


class SharedMemoryBus:
    """Validate every recipient and keep one durable receipt per delivery.

    Conclusive outcomes (accepted / quarantined / durable rejections) are
    cached one-per-(source, recipient). A rejection caused by a missing
    provenance row is NOT cached: the lookup can transiently miss before the
    row is visible, and a durable rejection would permanently block every
    retry. Such a receipt is returned as retryable.
    """

    async def deliver(
        self,
        source_memory_id: uuid.UUID,
        recipient_agent: str,
        *,
        publisher_agent: str,
    ) -> DeliveryReceipt:
        if not recipient_agent.strip() or len(recipient_agent) > 128:
            raise ValueError("recipient_agent must contain 1 to 128 characters")
        if not publisher_agent.strip():
            raise ValueError("publisher_agent is required")

        async with db.acquire() as conn:
            async with conn.transaction():
                source_row = await conn.fetchrow(
                    """
                    SELECT id, session_id, agent_id, content, category, importance,
                           quarantined
                    FROM memories WHERE id = $1 FOR UPDATE
                    """,
                    source_memory_id,
                )
                if source_row is None:
                    raise ValueError("source memory does not exist")
                if source_row["agent_id"] != publisher_agent:
                    raise PermissionError("source memory belongs to another agent")
                previous = await conn.fetchrow(
                    """
                    SELECT source_memory_id, recipient_agent, target_memory_id, status, reason
                    FROM shared_memory_deliveries
                    WHERE source_memory_id = $1 AND recipient_agent = $2
                    """,
                    source_memory_id,
                    recipient_agent,
                )
                if previous is not None:
                    return DeliveryReceipt(**dict(previous))

                source = MemoryEntry(
                    id=source_row["id"],
                    session_id=source_row["session_id"],
                    agent_id=source_row["agent_id"],
                    content=source_row["content"],
                    category=source_row["category"],
                    importance=source_row["importance"],
                    quarantined=source_row["quarantined"],
                )
                provenance_row = await conn.fetchrow(
                    """
                    SELECT memory_id, source_agent, signature, parent_memory_id, created_at
                    FROM memory_provenance WHERE memory_id = $1
                    """,
                    source_memory_id,
                )
                provenance = MemoryProvenance(**dict(provenance_row)) if provenance_row else None
                target_id: uuid.UUID | None = None
                retryable = False
                if source.quarantined:
                    status: DeliveryStatus = "rejected"
                    reason = "source memory is quarantined"
                elif provenance is None:
                    # The provenance lookup can transiently miss: the source
                    # memory's provenance row may not be visible yet (e.g. the
                    # signing key was enabled after the write, or the source
                    # insert committed without its provenance row). Caching a
                    # rejection here would permanently block every retry once
                    # the row appears, so this receipt stays retryable.
                    status = "rejected"
                    reason = "missing or invalid source provenance"
                    retryable = True
                elif provenance.source_agent != source.agent_id or not provenance.verify(
                    source.id, source.content
                ):
                    status = "rejected"
                    reason = "missing or invalid source provenance"
                else:
                    result = await identity_gate.validate_incoming(
                        recipient_agent, source, provenance, connection=conn
                    )
                    if not result.is_valid:
                        status = "rejected"
                        reason = result.reason
                    else:
                        copy = await memory_store.add(
                            session_id=None,
                            agent_id=recipient_agent,
                            content=source.content,
                            category=source.category,
                            importance=source.importance,
                            connection=conn,
                            source_agent=source.agent_id,
                            parent_memory_id=source.id,
                            # The delivery copy is authorized by provenance +
                            # the recipient identity gate, and the receipt must
                            # reference the real copy; the memory approval gate
                            # must not re-route it to pending.
                            bypass_approval=True,
                        )
                        if isinstance(copy, PendingMemory):
                            # Unreachable by construction (bypass_approval):
                            # fail loudly instead of caching a bogus receipt.
                            raise RuntimeError("shared delivery requires a durable write")
                        status = "quarantined" if copy.quarantined else "accepted"
                        reason = (
                            "recipient validation changed during delivery"
                            if copy.quarantined
                            else ""
                        )
                        target_id = copy.id

                # Copy + receipt commit atomically: if the receipt insert
                # fails, the copied memory rolls back with it. Concurrent
                # deliveries for the same (source, recipient) serialize on
                # the source-row FOR UPDATE lock above, and the PK/ON
                # CONFLICT guard makes a duplicate receipt impossible.
                # Retryable (missing-provenance) rejections are returned
                # without a durable receipt.
                if not retryable:
                    insert_result = await conn.execute(
                        """
                        INSERT INTO shared_memory_deliveries
                            (source_memory_id, recipient_agent, target_memory_id, status, reason)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (source_memory_id, recipient_agent) DO NOTHING
                        """,
                        source_memory_id,
                        recipient_agent,
                        target_id,
                        status,
                        reason,
                    )
                    if parse_command_count(insert_result) == 0:
                        # ON CONFLICT DO NOTHING skipped the insert (a concurrent
                        # delivery won) — return the winner's receipt.
                        existing = await conn.fetchrow(
                            """
                            SELECT source_memory_id, recipient_agent, target_memory_id, status, reason
                            FROM shared_memory_deliveries
                            WHERE source_memory_id = $1 AND recipient_agent = $2
                            """,
                            source_memory_id,
                            recipient_agent,
                        )
                        return DeliveryReceipt(**dict(existing))

        receipt = DeliveryReceipt(source_memory_id, recipient_agent, target_id, status, reason)
        audit_log(
            "shared_memory_delivery",
            source_memory_id=str(source_memory_id),
            recipient_agent=recipient_agent,
            status=status,
        )
        return receipt


shared_memory_bus = SharedMemoryBus()
