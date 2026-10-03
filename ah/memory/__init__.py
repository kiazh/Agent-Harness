"""AgentHarness memory system — long-term memory with consolidation, forgetting, and retrieval.

Architecture:
    MemoryEntry (dataclass) → MemoryStore (CRUD) → MemoryConsolidator (write path)
    ImportanceScorer → ForgettingModel → MemoryRetriever (read path)

Additional components:
    SecretRedactor — redacts secrets before memory storage
    MemoryApprovalGate — human-in-the-loop approval for memory writes
    UserProfile / UserProfileStore — persistent user modeling
"""

from __future__ import annotations

from ah.memory.approval import (
    ApprovalStatus,
    MemoryApprovalGate,
    PendingMemory,
    memory_approval_gate,
)
from ah.memory.consolidator import MemoryConsolidator
from ah.memory.forgetting import ForgettingModel
from ah.memory.models import MemoryEntry, RetrievedMemory
from ah.memory.redaction import RedactionResult, SecretRedactor, redact_secrets
from ah.memory.retriever import MemoryRetriever
from ah.memory.scorer import ImportanceScorer
from ah.memory.store import MemoryStore, memory_store
from ah.memory.user_profile import (
    UserProfile,
    UserProfileStore,
    user_profile_store,
)

__all__ = [
    "MemoryEntry",
    "RetrievedMemory",
    "MemoryStore",
    "memory_store",
    "ImportanceScorer",
    "ForgettingModel",
    "MemoryRetriever",
    "MemoryConsolidator",
    "SecretRedactor",
    "RedactionResult",
    "redact_secrets",
    "ApprovalStatus",
    "PendingMemory",
    "MemoryApprovalGate",
    "memory_approval_gate",
    "UserProfile",
    "UserProfileStore",
    "user_profile_store",
]
