"""Compaction checkpoints extracted from ``machina/core/compression.py`` (moves-only).

Validated structured checkpoints travel alongside free-form summaries so a
compacted session retains its objective, expected artifact, constraints,
read-only scope, findings with exact counts, open questions, and next action.
Incomplete checkpoints are rejected at build/extract time rather than used
silently.

Every definition here is a verbatim move from ``compression.py`` — no logic
changes. ``compression.py`` re-exports all of these names so existing callers
(``tests/test_compaction_checkpoint.py``, ``machina/services.py``) keep
working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "CHECKPOINT_VERSION",
    "CompactionCheckpoint",
    "build_checkpoint",
    "effective_instruction",
    "extract_checkpoint",
    "validate_checkpoint",
]

CHECKPOINT_VERSION = 1


@dataclass
class CompactionCheckpoint:
    """Validated structured compaction checkpoint (alongside free-form summary).

    Fields: objective, expected artifact path, user constraints including an
    explicit read-only scope, findings WITH exact counts, unresolved
    questions, and the immediate next action. Incomplete checkpoints are
    rejected at build/extract time rather than used silently.
    """

    objective: str
    expected_artifact: str
    constraints: list[str]
    read_only_scope: str
    findings: list[dict[str, Any]]
    unresolved_questions: list[str]
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CHECKPOINT_VERSION,
            "objective": self.objective,
            "expected_artifact": self.expected_artifact,
            "constraints": list(self.constraints),
            "read_only_scope": self.read_only_scope,
            "findings": [dict(f) for f in self.findings],
            "unresolved_questions": list(self.unresolved_questions),
            "next_action": self.next_action,
        }


def validate_checkpoint(data: dict[str, Any]) -> dict[str, Any]:
    """Validate a checkpoint dict; raise ValueError on any incomplete part."""
    if not isinstance(data, dict):
        raise ValueError("checkpoint must be a mapping")
    required = (
        "objective",
        "expected_artifact",
        "constraints",
        "read_only_scope",
        "findings",
        "unresolved_questions",
        "next_action",
    )
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"incomplete checkpoint, missing: {', '.join(missing)}")
    if not isinstance(data["objective"], str) or not data["objective"].strip():
        raise ValueError("checkpoint objective must be a non-empty string")
    if not isinstance(data["expected_artifact"], str) or not data["expected_artifact"].strip():
        raise ValueError("checkpoint expected_artifact must be a non-empty string")
    if not isinstance(data["constraints"], list) or any(
        not isinstance(c, str) for c in data["constraints"]
    ):
        raise ValueError("checkpoint constraints must be a list of strings")
    if not isinstance(data["read_only_scope"], str):
        raise ValueError("checkpoint read_only_scope must be a string")
    if not isinstance(data["findings"], list):
        raise ValueError("checkpoint findings must be a list")
    for entry in data["findings"]:
        if not isinstance(entry, dict):
            raise ValueError("checkpoint findings entries must be mappings")
        if not isinstance(entry.get("finding"), str) or not entry["finding"].strip():
            raise ValueError("checkpoint findings entries need a non-empty finding")
        count = entry.get("count")
        if type(count) is not int or count < 0:
            raise ValueError("checkpoint findings entries need an exact count (int >= 0)")
        if set(entry) - {"finding", "count"}:
            raise ValueError("checkpoint findings entries allow only finding/count keys")
    if not isinstance(data["unresolved_questions"], list) or any(
        not isinstance(q, str) for q in data["unresolved_questions"]
    ):
        raise ValueError("checkpoint unresolved_questions must be a list of strings")
    if not isinstance(data["next_action"], str) or not data["next_action"].strip():
        raise ValueError("checkpoint next_action must be a non-empty string")
    normalized = {
        "objective": data["objective"],
        "expected_artifact": data["expected_artifact"],
        "constraints": list(data["constraints"]),
        "read_only_scope": data["read_only_scope"],
        "findings": [{"finding": f["finding"], "count": f["count"]} for f in data["findings"]],
        "unresolved_questions": list(data["unresolved_questions"]),
        "next_action": data["next_action"],
    }
    if "version" in data:
        normalized["version"] = data["version"]
    return normalized


def build_checkpoint(
    *,
    objective: str,
    expected_artifact: str,
    constraints: list[str],
    read_only_scope: str,
    findings: list[dict[str, Any]],
    unresolved_questions: list[str],
    next_action: str,
) -> dict[str, Any]:
    """Build and validate a checkpoint dict (rejects incomplete input)."""
    return validate_checkpoint(
        {
            "version": CHECKPOINT_VERSION,
            "objective": objective,
            "expected_artifact": expected_artifact,
            "constraints": constraints,
            "read_only_scope": read_only_scope,
            "findings": findings,
            "unresolved_questions": unresolved_questions,
            "next_action": next_action,
        }
    )


def extract_checkpoint(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a validated checkpoint from a compression_summary payload.

    Returns None when no checkpoint was persisted. Raises ValueError when a
    checkpoint is present but incomplete — callers must not use it silently.
    """
    if not isinstance(payload, dict) or "checkpoint" not in payload:
        return None
    data = payload["checkpoint"]
    if data is None:
        return None
    return validate_checkpoint(data)


def effective_instruction(
    checkpoint: dict[str, Any] | None, latest_instruction: str | None
) -> str | None:
    """Resolve which instruction governs: latest explicit input wins.

    Recovered checkpoint history never overrides a latest explicit user
    instruction. Returns the latest instruction when non-empty, else the
    checkpoint's next action, else None.
    """
    if isinstance(latest_instruction, str) and latest_instruction.strip():
        return latest_instruction
    if isinstance(checkpoint, dict):
        nxt = checkpoint.get("next_action")
        if isinstance(nxt, str) and nxt.strip():
            return nxt
    return None
