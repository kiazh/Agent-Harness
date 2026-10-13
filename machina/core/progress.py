"""Progress monitor: stagnation detection + artifact-first helper (Stream D).

Pure functions over an explicit, immutable-style history. No I/O, no imports
from agent/orchestrator code, stdlib only.

Shared-context constraints (must preserve):
- The agent loop stays UNBOUNDED: stagnation here is *not* an iteration or
  token cap. It fires only when K consecutive rounds add no new evidence AND
  no artifact bytes, i.e. the loop is rediscovering instead of progressing.
- K is a named tunable constant (DECISION: do not relitigate).

Round model
-----------
``record(history, tool, target, new_bytes)`` appends one round (one tool
action) and returns a NEW history list; inputs are never mutated.
A round is stagnant when it adds no new file coverage (its normalized
``target`` was already covered by an earlier round, or it names no target)
AND it produced no artifact bytes (``new_bytes`` <= 0).

``is_stagnant(history)`` is True exactly when the trailing
``STAGNATION_ROUNDS_K`` rounds are all stagnant.
"""

from __future__ import annotations

from typing import Any, TypedDict

# DECISION (made, do not relitigate): K consecutive stagnant rounds force a
# synthesize/write attempt plus a truthful stop reason. Tunable here.
STAGNATION_ROUNDS_K: int = 3

# Minimum gathered evidence items before the artifact-first helper advises
# writing without an explicit user ask. Tunable here.
MIN_EVIDENCE_TO_WRITE_FIRST: int = 3


class Round(TypedDict):
    tool: str
    target: str
    new_bytes: int


def _normalize_target(target: Any) -> str:
    if target is None:
        return ""
    return str(target).strip()


def record(
    history: list[Round] | tuple[Round, ...] | None, tool: str, target: Any, new_bytes: int
) -> list[Round]:
    """Append one round and return a NEW history (input never mutated)."""
    hist = list(history) if history else []
    hist.append(
        Round(tool=str(tool), target=_normalize_target(target), new_bytes=max(0, int(new_bytes)))
    )
    return hist


def _covered_before(history: list[Round], index: int) -> bool:
    """Whether round ``index`` covers a target already covered earlier."""
    target = history[index]["target"]
    if not target:
        return True  # names no target -> adds no coverage
    return any(r["target"] == target for r in history[:index])


def round_is_stagnant(history: list[Round], index: int) -> bool:
    """A round is stagnant iff no new coverage AND no artifact bytes."""
    r = history[index]
    return _covered_before(history, index) and r["new_bytes"] <= 0


def stagnant_trailing_count(history: list[Round] | tuple[Round, ...] | None) -> int:
    """Count of consecutive stagnant rounds at the tail of history."""
    hist = list(history) if history else []
    count = 0
    for i in range(len(hist) - 1, -1, -1):
        if round_is_stagnant(hist, i):
            count += 1
        else:
            break
    return count


def is_stagnant(history: list[Round] | tuple[Round, ...] | None) -> bool:
    """True exactly when the trailing K rounds are all stagnant.

    Productive histories (any recent new coverage or artifact bytes) never
    trigger, no matter how long they run: this is not an iteration bound.
    """
    hist = list(history) if history else []
    if len(hist) < STAGNATION_ROUNDS_K:
        return False
    return stagnant_trailing_count(hist) >= STAGNATION_ROUNDS_K


def stop_reason(history: list[Round] | tuple[Round, ...] | None) -> str:
    """Truthful human-readable reason for stopping or continuing."""
    hist = list(history) if history else []
    if is_stagnant(hist):
        return (
            f"stagnant for {STAGNATION_ROUNDS_K} consecutive rounds: "
            "no new file coverage and no artifact bytes; "
            "forced synthesize/write attempt required"
        )
    if not hist:
        return "continuing: no rounds recorded yet"
    trailing = stagnant_trailing_count(hist)
    if trailing:
        return f"continuing: {trailing} stagnant round(s), below K={STAGNATION_ROUNDS_K}"
    return "continuing: recent rounds added new coverage or artifact bytes"


def should_write_first(evidence_count: int, explicit_ask: bool) -> bool:
    """Artifact-first helper: write the report first, then improve if needed.

    An explicit user ask to write always wins. Otherwise, advise writing once
    at least MIN_EVIDENCE_TO_WRITE_FIRST evidence items exist instead of
    delaying a useful artifact for exhaustive rediscovery.
    """
    if explicit_ask:
        return True
    return max(0, int(evidence_count)) >= MIN_EVIDENCE_TO_WRITE_FIRST
