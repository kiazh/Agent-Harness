"""Machina memory system — long-term memory with consolidation, forgetting, and retrieval.

Architecture:
    MemoryEntry (dataclass) → MemoryStore (CRUD) → MemoryConsolidator (write path)
    ImportanceScorer → ForgettingModel → MemoryRetriever (read path)

Additional components:
    SecretRedactor — redacts secrets before memory storage
    MemoryApprovalGate — human-in-the-loop approval for memory writes
    UserProfile / UserProfileStore — persistent user modeling

Re-exports are lazy (PEP 562, mirroring ``machina.core``): ``Config.load``
validates ``persona_description`` through ``machina.memory.redaction``, and
eagerly initializing this package pulled ``approval → machina.core.provider``
while the provider package could still be mid-import (``from
machina.core.provider.copilot import …`` first), silently discarding the
configured persona. Keep this body free of heavy imports; the map below is
the single source for the public surface.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

# name -> (submodule, attribute) for the public re-export surface.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ApprovalStatus": ("machina.memory.approval", "ApprovalStatus"),
    "MemoryApprovalGate": ("machina.memory.approval", "MemoryApprovalGate"),
    "PendingMemory": ("machina.memory.approval", "PendingMemory"),
    "memory_approval_gate": ("machina.memory.approval", "memory_approval_gate"),
    "MemoryConsolidator": ("machina.memory.consolidator", "MemoryConsolidator"),
    "ForgettingModel": ("machina.memory.forgetting", "ForgettingModel"),
    "AgentBelief": ("machina.memory.identity", "AgentBelief"),
    "BeliefTransition": ("machina.memory.identity", "BeliefTransition"),
    "IdentityGate": ("machina.memory.identity", "IdentityGate"),
    "MemoryProvenance": ("machina.memory.identity", "MemoryProvenance"),
    "ValidationResult": ("machina.memory.identity", "ValidationResult"),
    "identity_gate": ("machina.memory.identity", "identity_gate"),
    "MemoryEntry": ("machina.memory.models", "MemoryEntry"),
    "RetrievedMemory": ("machina.memory.models", "RetrievedMemory"),
    "EmotionTopology": ("machina.memory.persona", "EmotionTopology"),
    "PersonaMemory": ("machina.memory.persona", "PersonaMemory"),
    "PersonaMemoryStore": ("machina.memory.persona", "PersonaMemoryStore"),
    "persona_memory_store": ("machina.memory.persona", "persona_memory_store"),
    "RedactionResult": ("machina.memory.redaction", "RedactionResult"),
    "SecretRedactor": ("machina.memory.redaction", "SecretRedactor"),
    "redact_secrets": ("machina.memory.redaction", "redact_secrets"),
    "MemoryRetriever": ("machina.memory.retriever", "MemoryRetriever"),
    "MemoryAction": ("machina.memory.rl", "MemoryAction"),
    "MemoryDecision": ("machina.memory.rl", "MemoryDecision"),
    "MemoryReward": ("machina.memory.rl", "MemoryReward"),
    "MemoryState": ("machina.memory.rl", "MemoryState"),
    "ImportanceScorer": ("machina.memory.scorer", "ImportanceScorer"),
    "MemoryStore": ("machina.memory.store", "MemoryStore"),
    "memory_store": ("machina.memory.store", "memory_store"),
    "UserProfile": ("machina.memory.user_profile", "UserProfile"),
    "UserProfileStore": ("machina.memory.user_profile", "UserProfileStore"),
    "user_profile_store": ("machina.memory.user_profile", "user_profile_store"),
}

# Submodules the eager package bound as attributes; keep attribute access
# (``machina.memory.store`` after ``import machina.memory``) working.
_SUBMODULES = frozenset(
    {
        "approval",
        "consolidator",
        "forgetting",
        "identity",
        "models",
        "persona",
        "redaction",
        "retriever",
        "rl",
        "scorer",
        "store",
        "user_profile",
    }
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


def __getattr__(name: str) -> Any:
    """Lazily resolve the public surface (PEP 562)."""
    if name in _LAZY_EXPORTS:
        module_name, attr = _LAZY_EXPORTS[name]
        value = getattr(import_module(module_name), attr)
    elif name in _SUBMODULES:
        value = import_module(f"machina.memory.{name}")
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value  # cache so __getattr__ runs once per name
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | _SUBMODULES)
