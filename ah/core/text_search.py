"""Bounded PostgreSQL full-text query construction for conversation recall."""

from __future__ import annotations

import re

_SEARCH_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does",
    "for", "from", "how", "in", "is", "it", "of", "on", "or", "the", "to",
    "was", "were", "what", "when", "where", "which", "who", "why", "with",
}


def build_or_tsquery(query: str) -> str:
    """Build a sanitized OR query with at most 16 distinct non-stopword terms."""
    words = [
        word
        for word in dict.fromkeys(re.findall(r"[a-z0-9]+", query.lower()))
        if word not in _SEARCH_STOPWORDS
    ]
    return " | ".join(words[:16])
