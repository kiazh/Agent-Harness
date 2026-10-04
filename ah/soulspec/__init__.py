"""SoulSpec: Open Standard for Agent Configuration (Gap 5)."""

from __future__ import annotations

from ah.soulspec.adapters import AgentHarnessAdapter, ClaudeCodeAdapter, CodexAdapter
from ah.soulspec.conformance import SoulSpecConformance, ValidationResult
from ah.soulspec.merge import SoulSpecMerger
from ah.soulspec.schema import SoulSpec

__all__ = [
    "AgentHarnessAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "SoulSpec",
    "SoulSpecConformance",
    "SoulSpecMerger",
    "ValidationResult",
]
