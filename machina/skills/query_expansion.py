"""One bounded model call that routes a request into catalog vocabulary.

This module is the single LLM seam in automatic skill selection, and it never
selects anything: it only splits the user request into the closed schema-v2
routing vocabulary (:data:`machina.skills.metadata.TASK_FAMILIES` /
``CAPABILITIES``) plus short related terms. The deterministic picker
(:func:`machina.skills.runtime.automatic_skills`) stays the only selector;
:func:`merge_into_query` merely enriches the text it scores.

Contracts:

* exactly one model call per turn, cached by normalized query (LRU + TTL), so
  repeated turns are free;
* fail-open: any provider error, timeout, non-mapping answer, or validation
  failure yields an empty ``Expansion(source="fallback")`` and never raises;
* the expansion is turn-scoped through a ``ContextVar``; the sync getter
  :func:`current_expansion` never calls the model;
* instrumentation receives only query length/hash and counts — never raw text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

import yaml

from machina.memory.redaction import redact_secrets
from machina.observability.instrumentation import log_event
from machina.skills.metadata import CAPABILITIES, TASK_FAMILIES

__all__ = [
    "Expansion",
    "EMPTY_EXPANSION",
    "build_expansion_prompt",
    "clear_cache",
    "current_expansion",
    "expand_query",
    "merge_into_query",
    "prepare_turn",
]

#: Injected model seam: one prompt in, one raw completion out.
ExpansionSeam = Callable[[str], Awaitable[str]]

MAX_QUERY_CHARS = 2000
MAX_KEYWORDS = 12
MAX_FAMILIES = 3
MAX_CAPABILITIES = 4
MAX_STACK_ITEMS = 8
MAX_TERM_CHARS = 60
#: Upper bound on the scorer query after merging; the raw request is never
#: truncated, only the appended routing terms are bounded.
MAX_MERGED_QUERY_CHARS = 600
CACHE_MAX_ENTRIES = 256
CACHE_TTL_SECONDS = 600.0
DEFAULT_TIMEOUT_SECONDS = 5.0

_WS_RUN = re.compile(r"\s+")


@dataclass(frozen=True)
class Expansion:
    """Validated routing vocabulary for one turn (never authority)."""

    keywords: tuple[str, ...] = ()
    task_families: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    stack_languages: tuple[str, ...] = ()
    stack_frameworks: tuple[str, ...] = ()
    source: str = "fallback"

    @property
    def is_empty(self) -> bool:
        return not (
            self.keywords
            or self.task_families
            or self.capabilities
            or self.stack_languages
            or self.stack_frameworks
        )


EMPTY_EXPANSION = Expansion()

#: (normalized query, expansion) for the current turn; overwritten per turn.
_CURRENT: ContextVar[tuple[str, Expansion] | None] = ContextVar(
    "skill_query_expansion", default=None
)
#: normalized query -> (monotonic timestamp, expansion); OrderedDict is the LRU.
_CACHE: OrderedDict[str, tuple[float, Expansion]] = OrderedDict()


def _normalize(query: str) -> str:
    """Canonical cache/turn key: casefolded, trimmed, whitespace-collapsed."""
    return _WS_RUN.sub(" ", (query or "").casefold().strip())


def _query_hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()[:16]


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _enum_list(value: Any, allowed: frozenset[str], *, limit: int) -> tuple[str, ...]:
    out: list[str] = []
    for raw in _as_list(value):
        if not isinstance(raw, str):
            continue
        term = raw.strip().casefold().replace(" ", "-")
        if term in allowed and term not in out:
            out.append(term)
        if len(out) >= limit:
            break
    return tuple(out)


def _clean_terms(value: Any, *, limit: int, max_chars: int = MAX_TERM_CHARS) -> tuple[str, ...]:
    """Bound, dedupe (casefolded), and secret-redact free-text routing terms."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _as_list(value):
        if not isinstance(raw, str):
            continue
        text = " ".join(raw.split()).strip()
        if not text:
            continue
        try:
            text = redact_secrets(text).text.strip()
        except Exception:  # redaction must fail closed without losing routing
            text = ""
        text = text[:max_chars].strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return tuple(out)


def build_expansion_prompt(query: str) -> str:
    """Build the single bounded routing prompt for one user request."""
    families = ", ".join(sorted(TASK_FAMILIES))
    capabilities = ", ".join(sorted(CAPABILITIES))
    body = (query or "")[:MAX_QUERY_CHARS]
    return (
        "You are a catalog router. Split the user request into routing vocabulary for a "
        "deterministic skill picker. Return ONLY a YAML (or JSON) mapping, no prose and no "
        "code fences, in this shape:\n"
        "task_families: [1-3 from the closed list]\n"
        "capabilities: [0-4 from the closed list]\n"
        "keywords: [0-12 short related terms, including synonyms and hidden intents]\n"
        "stack_languages: [0-8 languages named or implied, optional]\n"
        "stack_frameworks: [0-8 frameworks named or implied, optional]\n"
        f"Allowed task_families (choose 1-3): {families}.\n"
        f"Allowed capabilities (choose 0-4): {capabilities}.\n"
        "Rules: every value must come from the closed lists above; unknown values are "
        "dropped. Keywords are short routing terms (at most 60 characters each), not "
        "sentences — include stemmed synonyms and the unwritten intent of the request. "
        "Do not answer the task, do not choose a skill, do not request tools, and do not "
        "invent stack items. Use empty lists when unsure.\n"
        f"User request: {body}\n"
    )


def _decode_mapping(raw: Any) -> dict[str, Any] | None:
    """Decode a raw completion into a mapping (YAML or JSON, fences tolerated)."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0] if "```" in text else text
        text = text.strip()
    if not text:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        data = None
    if isinstance(data, dict):
        return data
    # Tolerate one JSON object wrapped in prose, but nothing looser.
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            candidate = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
        return candidate if isinstance(candidate, dict) else None
    return None


def _parse_expansion(raw: Any) -> Expansion:
    """Validate a raw completion; raises ``ValueError`` on an unusable answer."""
    mapping = _decode_mapping(raw)
    if mapping is None:
        raise ValueError("expansion answer is not a mapping")
    families = _enum_list(mapping.get("task_families"), TASK_FAMILIES, limit=MAX_FAMILIES)
    if not families:
        raise ValueError("expansion answer declared no known task family")
    capabilities = _enum_list(mapping.get("capabilities"), CAPABILITIES, limit=MAX_CAPABILITIES)
    return Expansion(
        keywords=_clean_terms(mapping.get("keywords"), limit=MAX_KEYWORDS),
        task_families=families,
        capabilities=capabilities,
        stack_languages=_clean_terms(mapping.get("stack_languages"), limit=MAX_STACK_ITEMS),
        stack_frameworks=_clean_terms(mapping.get("stack_frameworks"), limit=MAX_STACK_ITEMS),
        source="llm",
    )


def _cache_get(normalized: str, now: float) -> Expansion | None:
    entry = _CACHE.get(normalized)
    if entry is None:
        return None
    created, expansion = entry
    if now - created > CACHE_TTL_SECONDS:
        _CACHE.pop(normalized, None)
        return None
    _CACHE.move_to_end(normalized)
    return expansion


def _cache_put(normalized: str, expansion: Expansion, now: float) -> None:
    _CACHE[normalized] = (now, expansion)
    _CACHE.move_to_end(normalized)
    while len(_CACHE) > CACHE_MAX_ENTRIES:
        _CACHE.popitem(last=False)


def clear_cache() -> None:
    """Drop the query-expansion LRU (test isolation and explicit resets)."""
    _CACHE.clear()


def _default_seam() -> ExpansionSeam:
    from machina.skills.enrich import default_agent_seam

    return default_agent_seam()


def _event_fields(normalized: str, **counts: int | str) -> dict[str, int | str]:
    return {
        "query_len": len(normalized),
        "query_hash": _query_hash(normalized),
        **counts,
    }


async def expand_query(
    query: str,
    *,
    seam: ExpansionSeam | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Expansion:
    """Return the turn's routing expansion with exactly one model call.

    Cache hits are free. Everything else is fail-open: an exception, timeout,
    non-mapping answer, or empty closed vocabulary returns ``EMPTY_EXPANSION``
    and is reported. ``asyncio.CancelledError`` is never swallowed.
    """
    normalized = _normalize(query)
    if not normalized:
        return EMPTY_EXPANSION
    now = asyncio.get_running_loop().time()
    cached = _cache_get(normalized, now)
    if cached is not None:
        hit = replace(cached, source="cache") if cached.source == "llm" else cached
        log_event(
            "skills.expand.cache_hit",
            **_event_fields(
                normalized,
                source=hit.source,
                task_families=len(hit.task_families),
                capabilities=len(hit.capabilities),
                keywords=len(hit.keywords),
            ),
        )
        return hit

    prompt = build_expansion_prompt(query)
    try:
        call = seam or _default_seam()
        raw = await asyncio.wait_for(call(prompt), timeout=timeout)
        expansion = _parse_expansion(raw)
    except Exception as exc:  # provider/timeout/validation are all fail-open
        log_event(
            "skills.expand.error",
            level="warning",
            **_event_fields(normalized, error=type(exc).__name__),
        )
        expansion = EMPTY_EXPANSION
        _cache_put(normalized, expansion, now)
        return expansion

    _cache_put(normalized, expansion, now)
    log_event(
        "skills.expand.finish",
        **_event_fields(
            normalized,
            source=expansion.source,
            task_families=len(expansion.task_families),
            capabilities=len(expansion.capabilities),
            keywords=len(expansion.keywords),
            stack_languages=len(expansion.stack_languages),
            stack_frameworks=len(expansion.stack_frameworks),
        ),
    )
    return expansion


async def prepare_turn(
    query: str,
    *,
    seam: ExpansionSeam | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Expansion:
    """Resolve (or reuse) one turn's expansion and publish it to the context.

    Called once per turn before the system prompt is built. The previous
    turn's value is cleared first, so a failed/absent expansion can never leak
    into this turn's deterministic routing.
    """
    normalized = _normalize(query)
    _CURRENT.set(None)
    expansion = await expand_query(query, seam=seam, timeout=timeout)
    _CURRENT.set((normalized, expansion))
    return expansion


def current_expansion(query: str) -> Expansion:
    """Sync getter for the current turn's expansion (never calls the model)."""
    stored = _CURRENT.get()
    if stored is None:
        return EMPTY_EXPANSION
    normalized, expansion = stored
    if _normalize(query) != normalized:
        return EMPTY_EXPANSION
    return expansion


def merge_into_query(query: str, expansion: Expansion | None) -> str:
    """Append sanitized routing terms to *query* for the deterministic scorer.

    The raw request is preserved verbatim; appended keywords and quoted
    family/capability terms are deduped, redacted, and bounded so a hostile or
    verbose expansion cannot grow the scoring query without limit.
    """
    base = query or ""
    if expansion is None or expansion.is_empty:
        return base
    seen = {word for word in _normalize(base).split() if word}
    extras: list[str] = []

    def add(term: str, *, key: str | None = None) -> None:
        text = " ".join(str(term).split()).strip()
        if not text:
            return
        try:
            text = redact_secrets(text).text.strip()
        except Exception:
            return
        text = text[:MAX_TERM_CHARS].strip()
        canonical = key or text.casefold()
        if not text or canonical in seen:
            return
        seen.add(canonical)
        extras.append(text)

    for keyword in expansion.keywords:
        add(keyword)
    for family in expansion.task_families:
        add(f"\u201c{family}\u201d", key=family.casefold())
    for capability in expansion.capabilities:
        add(f"\u201c{capability}\u201d", key=capability.casefold())

    merged = base
    for term in extras:
        candidate = f"{merged} {term}"
        if len(candidate) > MAX_MERGED_QUERY_CHARS:
            break
        merged = candidate
    return merged
