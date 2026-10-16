"""Revision identities and generation-guarded cache publication (spec §11).

Five identity classes are first-class values, each derived from exactly one
facet of the skills system:

* :func:`content_revision` — one skill's exact bytes + front-matter metadata;
* :func:`catalog_revision` — the effective catalog (name / content / enabled /
  source precedence per skill);
* :func:`telemetry_revision` — activity counters only (``usage_count``,
  ``view_count``, ``last_activity_at``);
* :data:`POLICY_REVISION` — the deterministic ranking/applicability policy
  (re-exported from :mod:`machina.skills.search`);
* :func:`task_state_revision` — the caller's explicit task-state revision.

:class:`RevisionRegistry` snapshots those identities for one scope (a single
skill or the whole catalog) and :meth:`RevisionRegistry.identity_for` maps a
feature area to exactly the identities that affect it, so caches can be keyed
by precisely those values:

* ``section_index`` / ``section_bindings`` — content only;
* ``search_results`` — catalog + ranking policy;
* ``disclosure_plan`` — catalog + ranking policy + task state;
* ``administrative_reads`` (browse/preview) — catalog only;
* ``telemetry_reads`` — telemetry only.

The §11 separation this encodes: **telemetry updates MUST NOT invalidate
content-derived data**. Recording a use/view changes only ``telemetry`` — a
section index or disclosure plan cache keyed through ``identity_for`` keeps
hitting — while a content edit moves the content (and catalog) identity and
therefore invalidates every content-derived cache.

Generation-guarded publication (no watchers, no daemon threads):

* :class:`GenerationCounter` — a monotonic counter bumped on every explicit
  invalidation (e.g. ``sections.clear_section_cache``);
* :func:`guarded_publish` — captures the generation before computing and
  rechecks it, under the same lock a bump takes, before publishing: an
  invalidation that lands mid-compute makes the publication a no-op, and the
  stale value is returned to the caller but never cached. The next compute
  republishes against the new generation;
* :class:`RevisionCache` — a bounded identity-keyed cache on top of both: the
  cache key is the identity tuple read before computing, rechecked before
  publishing, plus an explicit ``invalidate()`` fence for reload paths.

Fingerprint policy: stat size/mtime signatures (``SkillRegistry._signature``)
are a fast PRE-FILTER for reload decisions only — they are not integrity
evidence. Integrity is bound to the content fingerprint (sha256 over the exact
bytes; :func:`content_revision` and ``sections.content_fingerprint``), and both
the section index cache and continuation reads are keyed/bound by it, so a hit
can never serve bytes other than the ones fingerprinted. Existing integrity
checks (bundle-revision matching, enrichment sidecar revisions, injection
scanning) keep their homes unchanged.

This module is a stdlib-only leaf: it imports only
:mod:`machina.skills.search` (itself machina-free) and never a provider,
embedding, or network path.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from machina.skills.search import (
    POLICY_REVISION,
    catalog_revision,
    content_revision,
)

__all__ = [
    "ADMINISTRATIVE_READS",
    "CATALOG",
    "CONTENT",
    "DISCLOSURE_PLAN",
    "IDENTITY_NAMES",
    "RANKING_POLICY",
    "SEARCH_RESULTS",
    "SECTION_BINDINGS",
    "SECTION_INDEX",
    "TASK_STATE",
    "TELEMETRY",
    "TELEMETRY_READS",
    "GenerationCounter",
    "RevisionCache",
    "RevisionRegistry",
    "catalog_revision",
    "content_revision",
    "content_set_revision",
    "guarded_publish",
    "task_state_revision",
    "telemetry_revision",
]

T = TypeVar("T")

#: Identity names, in the fixed order `RevisionRegistry.as_dict` reports them.
CONTENT = "content"
CATALOG = "catalog"
TELEMETRY = "telemetry"
RANKING_POLICY = "ranking_policy"
TASK_STATE = "task_state"
IDENTITY_NAMES = (CONTENT, CATALOG, TELEMETRY, RANKING_POLICY, TASK_STATE)

#: Feature areas with a pinned identity mapping (closed vocabulary; extending
#: it is a code change with tests, never a silent guess).
SECTION_INDEX = "section_index"
SECTION_BINDINGS = "section_bindings"
SEARCH_RESULTS = "search_results"
DISCLOSURE_PLAN = "disclosure_plan"
TELEMETRY_READS = "telemetry_reads"
ADMINISTRATIVE_READS = "administrative_reads"

_FEATURE_IDENTITIES: dict[str, tuple[str, ...]] = {
    SECTION_INDEX: (CONTENT,),
    SECTION_BINDINGS: (CONTENT,),
    SEARCH_RESULTS: (CATALOG, RANKING_POLICY),
    DISCLOSURE_PLAN: (CATALOG, RANKING_POLICY, TASK_STATE),
    TELEMETRY_READS: (TELEMETRY,),
    # Browse/preview is administrative: it depends on the catalog snapshot but
    # never on activity counters, so admin reads cannot churn content caches.
    ADMINISTRATIVE_READS: (CATALOG,),
}


def content_set_revision(skills: Iterable[Any]) -> str:
    """Content-only digest of a skill set (name + per-skill content revision).

    Unlike :func:`catalog_revision` this ignores ``enabled`` and
    ``source_type``: toggling a skill off or moving it between sources moves
    the catalog identity, never the content-of-record identity.
    """
    lines = sorted(
        f"{getattr(skill, 'name', '').casefold()}:{content_revision(skill)}" for skill in skills
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def _counter(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _stamp(value: Any) -> str:
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else ""


def telemetry_revision(skills: Iterable[Any]) -> str:
    """Digest of activity counters only — never of instruction content."""
    lines = []
    for skill in skills:
        name = str(getattr(skill, "name", "")).casefold()
        lines.append(
            f"{name}:{_counter(getattr(skill, 'usage_count', 0))}:"
            f"{_counter(getattr(skill, 'view_count', 0))}:"
            f"{_stamp(getattr(skill, 'last_activity_at', None))}"
        )
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()[:16]


def task_state_revision(raw: Any) -> int:
    """Explicit task-state revision from a mapping (0 when absent/invalid).

    Mirrors ``machina.skills.planned_steps.task_revision_from`` rather than
    importing it: ``planned_steps`` imports ``sections``, which imports this
    module — importing it back would create a cycle. Pinned by a parity test.
    """
    if not isinstance(raw, Mapping):
        return 0
    try:
        return max(0, int(raw.get("revision", 0)))
    except (TypeError, ValueError):
        return 0


def _task_state_text(value: Any) -> str:
    try:
        return str(max(0, int(value)))
    except (TypeError, ValueError):
        return "0"


@dataclass(frozen=True)
class RevisionRegistry:
    """The five §11 identities for one scope (a skill or a catalog).

    Construct with :meth:`for_skill` (one skill's content identity) or
    :meth:`for_skills` (a whole-catalog snapshot); then key caches through
    :meth:`identity_for` so administrative activity can never invalidate
    content-derived data.
    """

    content: str = ""
    catalog: str = ""
    telemetry: str = ""
    ranking_policy: str = POLICY_REVISION
    task_state: str = "0"

    @classmethod
    def for_skill(cls, skill: Any, *, task_revision: int = 0) -> RevisionRegistry:
        return cls(
            content=content_revision(skill),
            catalog=catalog_revision([skill]),
            telemetry=telemetry_revision([skill]),
            task_state=_task_state_text(task_revision),
        )

    @classmethod
    def for_skills(cls, skills: Iterable[Any], *, task_revision: int = 0) -> RevisionRegistry:
        materialized = list(skills)
        return cls(
            content=content_set_revision(materialized),
            catalog=catalog_revision(materialized),
            telemetry=telemetry_revision(materialized),
            task_state=_task_state_text(task_revision),
        )

    def identity_for(self, feature_area: str) -> tuple[str, ...]:
        """Identity values affecting *feature_area*, in a stable order.

        Unknown feature areas raise: a cache must never guess which identities
        it depends on.
        """
        names = _FEATURE_IDENTITIES.get(feature_area)
        if names is None:
            expected = ", ".join(sorted(_FEATURE_IDENTITIES))
            raise ValueError(f"unknown feature area {feature_area!r}; expected one of {expected}")
        return tuple(getattr(self, name) for name in names)

    def as_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in IDENTITY_NAMES}


@dataclass
class GenerationCounter:
    """Monotonic generation counter for guarded cache publication (spec §11).

    ``bump()`` is the explicit invalidation signal (no watchers, no daemon).
    ``publish()`` rechecks the generation under the same lock a bump takes, so
    a check-then-store pair is atomic with respect to invalidation.
    """

    _generation: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def current(self) -> int:
        return self._generation

    def bump(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def publish(self, generation: int, value: Any, store: Callable[[Any], None]) -> bool:
        """Store *value* only while *generation* is still current."""
        with self._lock:
            if self._generation != generation:
                return False
            store(value)
            return True


def guarded_publish(
    generation: GenerationCounter,
    compute: Callable[[], T],
    store: Callable[[T], None],
    *,
    on_stale: Callable[[], None] | None = None,
) -> T:
    """Compute guarded by *generation*; publish only if it did not move.

    Returns the computed value either way. When an invalidation landed while
    computing, the value is deliberately NOT stored (the next compute
    republishes) and ``on_stale`` — when given — observes the dropped
    publication.
    """
    start = generation.current()
    value = compute()
    if not generation.publish(start, value, store) and on_stale is not None:
        on_stale()
    return value


class RevisionCache:
    """Bounded cache keyed by revision identities, guarded on publish.

    ``revisions`` is a callable returning a fresh :class:`RevisionRegistry`
    (e.g. ``registry.revisions``). Every miss computes, then rechecks both the
    live identity and the explicit generation before publishing: a source edit
    or ``invalidate()`` that lands mid-compute wins — the stale value is
    returned to the caller but never cached.
    """

    def __init__(self, revisions: Callable[[], RevisionRegistry], *, max_entries: int = 64) -> None:
        self._revisions = revisions
        self._max_entries = max(1, int(max_entries))
        self._generation = GenerationCounter()
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[Any, ...], Any] = OrderedDict()

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def generation(self) -> int:
        return self._generation.current()

    def identity_of(self, feature_area: str) -> tuple[str, ...]:
        return self._revisions().identity_for(feature_area)

    def invalidate(self) -> None:
        """Drop every entry; an in-flight compute can no longer republish."""
        self._generation.bump()
        with self._lock:
            self._entries.clear()

    def get_or_compute(self, feature_area: str, key: Any, compute: Callable[[], T]) -> T:
        """Return the cached value for (*feature_area*, *key*) or compute it.

        ``key`` must be hashable; it names the request within the feature area
        (query, budget, stage, ...). Published values are keyed by the
        identity tuple read before computing, so even a racing store can never
        serve one identity's value under another identity's key.
        """
        identity = self.identity_of(feature_area)
        cache_key = (feature_area, identity, key)
        with self._lock:
            cached = self._entries.get(cache_key)
            if cached is not None:
                self._entries.move_to_end(cache_key)
                return cached
        start = self._generation.current()
        value = compute()
        if self.identity_of(feature_area) != identity:
            return value

        def store(new_value: T) -> None:
            with self._lock:
                self._entries[cache_key] = new_value
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)

        self._generation.publish(start, value, store)
        return value
