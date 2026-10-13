"""Authority-aware demotion for memory and RAG ranking (FM5 W1/W4).

Ported from :func:`machina.core.session_recall._is_catalog_or_echo`: catalog
listings, skill pages, and prior-recall echoes repeat query terms and
dominate raw keyword/ts_rank scores. They are demoted — never dropped — so
real instructions and user content rank first.

Recency and importance are tiebreakers in the retrieval ranking; this module
only classifies content authority. Kept stdlib-only so both ``machina.rag``
and ``machina.memory`` can depend on it without import cycles.
"""

from __future__ import annotations

__all__ = ["CATALOG_DEMOTION_FACTOR", "authority_tier", "is_catalog_or_echo"]

#: Relevance multiplier for catalog/echo content. Small enough that a demoted
#: catalog cannot outrank a real memory with meaningfully lower keyword
#: overlap; large enough that catalog evidence still surfaces when it is the
#: only match (demoted, never dropped).
CATALOG_DEMOTION_FACTOR = 0.25

#: Lower tier sorts first. Ordinary content (including user instructions)
#: stays in tier 0; recognized catalog/echo content is tier 1.
_TIER_ORDINARY = 0
_TIER_CATALOG = 1


def is_catalog_or_echo(text: str, chunk_type: str | None = None) -> bool:
    """Detect repeated catalog listings and recall-echo text (precision filter).

    Conservative by construction: ordinary conversation and documents do not
    match. Mirrors the session_recall heuristics so memory, RAG, and recall
    paths agree on what counts as catalog noise.
    """
    p = text or ""
    pl = p.lower()
    if "session=" in pl and "chunk=" in pl:
        return True
    if p.startswith("Skill ") and "[" in p and "]:" in p:
        return True
    if p.startswith("ECC resource"):
        return True
    if p.startswith("[live] prior"):
        return True
    if "available agents" in pl or "no agents are available" in pl:
        return True
    if (chunk_type or "") == "tool_call" and (
        "skill_list" in pl or "skill_read" in pl or "session_recall" in pl
    ):
        return True
    return False


def authority_tier(text: str, chunk_type: str | None = None) -> int:
    """Return the authority tier for *text* (0 = ordinary, 1 = catalog/echo)."""
    return _TIER_CATALOG if is_catalog_or_echo(text, chunk_type) else _TIER_ORDINARY
