"""Turn-scoped cache over the shared BM25 skill ranking.

Routes ranking through the :mod:`machina.skills.ports` seam
(``SkillSource`` + ``rank_skills``) — never past it into
``skills.registry``/``skills.runtime`` directly. Ranking itself still lives
in ``skills.runtime``; the cache only avoids re-ranking identical normalized
queries within a turn.

Progressive-disclosure contract (per the skills-system skill): this layer
caches metadata ranking only. Skill bodies still load on demand through
``skill_read``; nothing here widens automatic disclosure.
"""

import hashlib
import re

from machina.skills.ports import SkillSource, rank_skills

_WS_RUN = re.compile(r"\s+")


def normalize_query(query: str) -> str:
    """Canonical form for cache keys: casefolded, trimmed, collapsed."""
    return _WS_RUN.sub(" ", query.casefold().strip())


class SkillPreselectCache:
    """Per-turn cache keyed by normalized-query hash (+ weights, if any)."""

    def __init__(self, skills: SkillSource):
        self._skills = skills
        self._cache: dict = {}
        self.rank_calls = 0

    def _key(self, query: str, weights) -> tuple:
        digest = hashlib.sha256(normalize_query(query).encode("utf-8")).hexdigest()
        wkey = tuple(sorted((weights or {}).items())) if weights else None
        return (digest, wkey)

    def rank(self, query: str, weights=None):
        """Ranked skills for *query*; identical queries rank exactly once."""
        key = self._key(query, weights)
        if key not in self._cache:
            self._cache[key] = rank_skills(self._skills, query, weights=weights)
            self.rank_calls += 1
        return self._cache[key]

    def invalidate(self, query: str | None = None, weights=None) -> None:
        """Drop one cached query, or the whole turn cache when *query* is None."""
        if query is None:
            self._cache.clear()
        else:
            self._cache.pop(self._key(query, weights), None)
