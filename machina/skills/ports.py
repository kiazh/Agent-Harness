"""Consumer-facing skills seam (Stream B).

Narrow interface the agent loop / controller need: skill lookup by name,
trigger-rank query, content fetch. Everything behind this seam (SKILL.md
parsing, telemetry, BM25 ranking, ECC bundle, hub, curator) keeps its
locality in :mod:`machina.skills.registry` / :mod:`machina.skills.runtime`.

Deletion test: deleting this module must break every ``core``/``controller``
consumer — the complexity (ranking, disclosure budgets) reappears across N
callers, so the seam earns its keep. Conversely, deleting any one consumer
leaves the seam unchanged.

Trigger / progressive-disclosure contracts (per the skills-system skill) are
unchanged: this layer exposes metadata lookup and ranking only. Skill bodies
still load on demand through ``skill_read`` / ``get_skill_content``; nothing
here widens automatic disclosure.

Cycle discipline: this module imports NOTHING from ``machina`` at runtime —
not even for typing (``Skill`` is referenced as ``Any``). That keeps the
``registry -> ports`` conformance edge and the ``controller -> ports`` consumer
edge cycle-free by construction; see ``tests/test_architecture_pairs.py``.
The BM25 rank dispatch below imports ``skills.runtime`` lazily, at call time.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SkillSource(Protocol):
    """Structural interface for skill lookup / rank / content fetch.

    Mirrors the exact signatures currently used via ``SkillRegistry`` —
    parameter names, defaults, and keyword-only markers are part of the
    contract and are locked by ``tests/test_architecture_pairs.py``.
    """

    def load_all(self, *, refresh: bool = False) -> None:
        """(Re)load skills; cheap no-op when the on-disk signature matches."""
        ...

    def list_skills(self) -> list[Any]:
        """All loaded skills (enabled and disabled)."""
        ...

    def get(self, name: str) -> Any | None:
        """Skill lookup by name, or None when unknown."""
        ...

    def match_triggers(self, query: str) -> list[Any]:
        """Whole-word trigger matches for *query* (enabled skills only)."""
        ...

    def get_skill_content(self, name: str) -> str | None:
        """Full instruction body for *name*, or None when unknown/disabled."""
        ...


def rank_skills(
    skills: SkillSource, query: str, *, weights: dict[str, float] | None = None
) -> list[Any]:
    """BM25 trigger-rank query over a ``SkillSource``.

    Same signature as ``machina.skills.runtime.rank_skills``; the ranking
    implementation is never reimplemented here — this seam only reroutes the
    call so ``controller`` depends on ``skills.ports`` instead of reaching
    past it into ``skills.runtime``. Lazy import keeps this module free of
    module-level ``machina`` edges.
    """
    from machina.skills.runtime import rank_skills as _rank

    return _rank(skills, query, weights=weights)
