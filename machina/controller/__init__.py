"""Deterministic turn-control helpers: triage, complexity, skill preselect.

Pure functions and small cached wrappers only — no network, database,
or model calls on any path. Behavior-changing wiring into the agent
loop is intentionally deferred (see README.md).
"""

from machina.controller.complexity import (
    SCORE_DIRECT_MAX,
    SCORE_SUBAGENTS_MIN,
    score_complexity,
)
from machina.controller.preselect import SkillPreselectCache
from machina.controller.triage import DEFAULT_FALLBACK, resolve_task

__all__ = [
    "DEFAULT_FALLBACK",
    "SCORE_DIRECT_MAX",
    "SCORE_SUBAGENTS_MIN",
    "SkillPreselectCache",
    "resolve_task",
    "score_complexity",
]
