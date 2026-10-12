"""Machina memory system — long-term memory with consolidation, forgetting, and retrieval.

Architecture:
    MemoryEntry (dataclass) → MemoryStore (CRUD) → MemoryConsolidator (write path)
    ImportanceScorer → ForgettingModel → MemoryRetriever (read path)

Additional components:
    SecretRedactor — redacts secrets before memory storage
    MemoryApprovalGate — human-in-the-loop approval for memory writes
    UserProfile / UserProfileStore — persistent user modeling
"""

from __future__ import annotations

from machina.memory.approval import (
    ApprovalStatus,
    MemoryApprovalGate,
    PendingMemory,
    memory_approval_gate,
)
from machina.memory.consolidator import MemoryConsolidator
from machina.memory.forgetting import ForgettingModel
from machina.memory.identity import (
    AgentBelief,
    BeliefTransition,
    IdentityGate,
    MemoryProvenance,
    ValidationResult,
    identity_gate,
)
from machina.memory.models import MemoryEntry, RetrievedMemory
from machina.memory.persona import (
    EmotionTopology,
    PersonaMemory,
    PersonaMemoryStore,
    persona_memory_store,
)
from machina.memory.redaction import RedactionResult, SecretRedactor, redact_secrets
from machina.memory.retriever import MemoryRetriever
from machina.memory.rl import MemoryAction, MemoryDecision, MemoryReward, MemoryState
from machina.memory.scorer import ImportanceScorer
from machina.memory.store import MemoryStore, memory_store
from machina.memory.user_profile import (
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
    "MemoryAction",
    "MemoryDecision",
    "MemoryReward",
    "MemoryState",
    "PersonaMemory",
    "EmotionTopology",
    "PersonaMemoryStore",
    "persona_memory_store",
    "AgentBelief",
    "BeliefTransition",
    "IdentityGate",
    "MemoryProvenance",
    "ValidationResult",
    "identity_gate",
]
