"""SoulSpec: Open Standard for Agent Configuration (Gap 5)."""

from __future__ import annotations

from machina.soulspec.adapters import MachinaAdapter, ClaudeCodeAdapter, CodexAdapter
from machina.soulspec.conformance import TestSoulSpecConformance, ValidationResult
from machina.soulspec.merge import SoulSpecMerger
from machina.soulspec.schema import SoulSpec

__all__ = [
    "MachinaAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "SoulSpec",
    "TestSoulSpecConformance",
    "SoulSpecMerger",
    "ValidationResult",
]
