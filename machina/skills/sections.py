"""Parsed instruction sections, deterministic anchors, version-bound reads (spec §10.4).

This module owns the *read* side of staged skill disclosure:

* headings are parsed **once per content revision** (:func:`index_sections`
  returns the same :class:`SectionIndex` while the revision is unchanged, so
  anchor line numbers can never be re-derived against different bytes);
* anchors are deterministic slugs with document-order duplicate suffixes
  (``run-tests``, ``run-tests-2``, ...) so a duplicate heading never makes a
  read ambiguous;
* declared schema-v2 sections bind through
  :func:`machina.skills.metadata.bind_section_anchors` against the **exact**
  parsed revision; missing/duplicate/undeclared resolutions surface
  :class:`~machina.skills.metadata.MetadataDiagnostic` values instead of
  silently serving the wrong passage;
* :func:`read_section` is *version-bound*: a continuation carries the
  ``content_revision`` it was produced against, and a read against edited
  content returns ``stale=True`` with empty text — never a different passage
  at the same offset. The existing character-offset reads in
  ``machina.tools.skills.skill_read`` keep their exact meaning; this module
  adds a parallel API rather than changing offset semantics.

Required-section edges form a bounded acyclic graph
(:func:`build_section_graph`); cycles are rejected with
:class:`SectionCycleError` and unknown targets are dropped with A1's
``dependency-unknown`` diagnostic. Both caches publish through
:func:`machina.skills.revisions.guarded_publish`: an invalidation that lands
mid-parse (``clear_section_cache``) prevents the stale index/binding from
being cached, and the next compute republishes (spec §11). The module is a
stdlib-only leaf: it imports ``machina.skills.metadata``,
``machina.skills.search``, and ``machina.skills.revisions`` (all machina-free)
and never a provider, embedding, or network path.
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from machina.skills.metadata import (
    CODE_DEPENDENCY_CYCLE,
    CODE_DEPENDENCY_UNKNOWN,
    CODE_SECTION_DUPLICATE_HEADING,
    MetadataDiagnostic,
    SkillMetadata,
    bind_section_anchors,
)
from machina.skills.revisions import GenerationCounter, guarded_publish
from machina.skills.search import content_revision as skill_content_revision

__all__ = [
    "CODE_SECTION_RANGE",
    "CODE_SECTION_STALE",
    "CODE_SECTION_UNDECLARED",
    "CODE_SECTION_UNKNOWN",
    "MAX_CACHED_REVISIONS",
    "ParsedSection",
    "ReadSectionResult",
    "SectionCycleError",
    "SectionGraph",
    "SectionIndex",
    "bound_sections",
    "build_section_graph",
    "clear_section_cache",
    "content_fingerprint",
    "index_sections",
    "read_section",
    "read_skill_section",
    "section_cache_generation",
    "skill_content_revision",
    "slugify_heading",
]

#: Closed diagnostic vocabulary added by the version-bound read path. Missing
#: anchors and duplicate headings reuse A1's codes (never re-invented).
CODE_SECTION_UNKNOWN = "section-unknown"
CODE_SECTION_UNDECLARED = "section-undeclared"
CODE_SECTION_STALE = "section-stale-content"
CODE_SECTION_RANGE = "section-continuation-range"

#: Bounded per-revision parse cache (spec §11 "revisioned in-process caching").
MAX_CACHED_REVISIONS = 8
#: Bounded declared-section binding cache (one A1 bind per revision+metadata).
MAX_CACHED_BINDINGS = 16

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_INDEX_CACHE: OrderedDict[str, SectionIndex] = OrderedDict()
_BINDINGS: OrderedDict[
    tuple[str, int], tuple[object, SkillMetadata, tuple[MetadataDiagnostic, ...]]
] = OrderedDict()
#: Generation bumped by every explicit invalidation (spec §11): a parse that
#: started before the bump must not publish its now-stale result into a cache.
_CACHE_GENERATION = GenerationCounter()


def content_fingerprint(content: str) -> str:
    """Stable 16-hex fingerprint of one exact content revision."""
    return hashlib.sha256((content or "").encode("utf-8", "replace")).hexdigest()[:16]


def slugify_heading(heading: str) -> str:
    """Deterministic anchor base for a heading (GitHub-style slug)."""
    slug = _SLUG_RE.sub("-", (heading or "").casefold()).strip("-")
    return slug or "section"


@dataclass(frozen=True)
class ParsedSection:
    """One heading-delimited section with exact character bounds."""

    section_id: str
    heading: str
    level: int
    line: int
    start: int
    end: int
    text: str
    occurrence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "heading": self.heading,
            "level": self.level,
            "line": self.line,
            "start": self.start,
            "end": self.end,
            "occurrence": self.occurrence,
        }


@dataclass(frozen=True)
class SectionIndex:
    """Parsed heading structure for one content revision (read-only)."""

    revision: str
    sections: tuple[ParsedSection, ...]
    preamble: str
    diagnostics: tuple[MetadataDiagnostic, ...] = ()

    def find(self, key: str) -> ParsedSection | None:
        """Resolve an anchor id, a heading text, or a slug to a section."""
        if not key:
            return None
        for section in self.sections:
            if section.section_id == key:
                return section
        slug = slugify_heading(key)
        key_folded = re.sub(r"\s+", " ", key.strip()).casefold()
        for section in self.sections:
            if section.section_id == slug:
                return section
            if re.sub(r"\s+", " ", section.heading.strip()).casefold() == key_folded:
                return section
        return None

    def find_by_line(self, line: int) -> ParsedSection | None:
        for section in self.sections:
            if section.line == line:
                return section
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "sections": [section.to_dict() for section in self.sections],
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


def _parse_index(content: str, revision: str) -> SectionIndex:
    headings: list[tuple[int, int, str, int]] = []  # (line, level, heading, start)
    offset = 0
    for line_number, raw_line in enumerate(content.splitlines(keepends=True), start=1):
        line = raw_line.rstrip("\r\n")
        match = _HEADING_RE.match(line)
        if match:
            heading = re.sub(r"\s+", " ", match.group(2).strip())
            headings.append((line_number, len(match.group(1)), heading, offset))
        offset += len(raw_line)
    used: dict[str, int] = {}
    diagnostics: list[MetadataDiagnostic] = []
    parsed: list[ParsedSection] = []
    for position, (line_number, level, heading, start) in enumerate(headings):
        end = len(content)
        for line_after, level_after, _heading_after, start_after in headings[position + 1 :]:
            if level_after <= level:
                end = start_after
                break
        base = slugify_heading(heading)
        occurrence = used.get(base, 0)
        used[base] = occurrence + 1
        section_id = base if occurrence == 0 else f"{base}-{occurrence + 1}"
        if occurrence:
            diagnostics.append(
                MetadataDiagnostic(
                    code=CODE_SECTION_DUPLICATE_HEADING,
                    message=(
                        f"heading {heading!r} appears more than once; "
                        f"anchors {base!r} and {section_id!r} stay deterministic"
                    ),
                )
            )
        parsed.append(
            ParsedSection(
                section_id=section_id,
                heading=heading,
                level=level,
                line=line_number,
                start=start,
                end=end,
                text=content[start:end],
                occurrence=occurrence,
            )
        )
    preamble = content[: headings[0][3]] if headings else content
    return SectionIndex(
        revision=revision, sections=tuple(parsed), preamble=preamble, diagnostics=tuple(diagnostics)
    )


def index_sections(content: str, *, refresh: bool = False) -> SectionIndex:
    """Parse headings once per content revision; same bytes -> same index.

    Publication is generation-guarded: if ``clear_section_cache`` runs while a
    parse is in flight, the stale index is returned to this caller but not
    cached, and the next call parses and publishes it again.
    """
    text = content or ""
    key = content_fingerprint(text)
    cached = None if refresh else _INDEX_CACHE.get(key)
    if cached is not None:
        _INDEX_CACHE.move_to_end(key)
        return cached

    def store(index: SectionIndex) -> None:
        _INDEX_CACHE[key] = index
        while len(_INDEX_CACHE) > MAX_CACHED_REVISIONS:
            _INDEX_CACHE.popitem(last=False)

    return guarded_publish(_CACHE_GENERATION, lambda: _parse_index(text, key), store)


def section_cache_generation() -> int:
    """Current section-cache generation (bumped by ``clear_section_cache``)."""
    return _CACHE_GENERATION.current()


def clear_section_cache() -> None:
    """Drop the per-revision parse and binding caches (explicit reload / tests).

    Bumps the generation first, so a compute that is already in flight can no
    longer publish its pre-invalidation result into the emptied caches.
    """
    _CACHE_GENERATION.bump()
    _INDEX_CACHE.clear()
    _BINDINGS.clear()


def bound_sections(
    metadata: SkillMetadata, content: str
) -> tuple[SkillMetadata, tuple[MetadataDiagnostic, ...]]:
    """Bind declared sections once per (content revision, metadata identity).

    The cached value holds the metadata object itself, so an id can never be
    reused for different metadata, and both the index and the declared
    headings are parsed once per content revision. Publication is
    generation-guarded like :func:`index_sections`.
    """
    key = (content_fingerprint(content), id(metadata))
    cached = _BINDINGS.get(key)
    if cached is not None and cached[0] is metadata:
        _BINDINGS.move_to_end(key)
        return cached[1], cached[2]

    def store(result: tuple[SkillMetadata, tuple[MetadataDiagnostic, ...]]) -> None:
        bound, diagnostics = result
        _BINDINGS[key] = (metadata, bound, tuple(diagnostics))
        while len(_BINDINGS) > MAX_CACHED_BINDINGS:
            _BINDINGS.popitem(last=False)

    bound, diagnostics = guarded_publish(
        _CACHE_GENERATION, lambda: bind_section_anchors(metadata, content), store
    )
    return bound, tuple(diagnostics)


def _relevant_diagnostics(
    diagnostics: tuple[MetadataDiagnostic, ...], section_id: str, heading: str
) -> tuple[MetadataDiagnostic, ...]:
    """Keep A1 binding diagnostics that name this declared section/heading."""
    relevant = []
    for diagnostic in diagnostics:
        if f"'{section_id}'" in diagnostic.message:
            relevant.append(diagnostic)
        elif heading and f"'{heading}'" in diagnostic.message:
            relevant.append(diagnostic)
    return tuple(relevant)


@dataclass(frozen=True)
class ReadSectionResult:
    """One version-bound section read (whole or a bounded continuation)."""

    skill_id: str
    content_revision: str
    section_id: str
    heading: str = ""
    text: str = ""
    start: int | None = None
    end: int | None = None
    total: int = 0
    section_total: int = 0
    line: int | None = None
    continuation: int | None = None
    stale: bool = False
    bound: bool = False
    anchor_id: str = ""
    diagnostics: tuple[MetadataDiagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "content_revision": self.content_revision,
            "section_id": self.section_id,
            "heading": self.heading,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "total": self.total,
            "section_total": self.section_total,
            "line": self.line,
            "continuation": self.continuation,
            "stale": self.stale,
            "bound": self.bound,
            "anchor_id": self.anchor_id,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


def _empty_result(
    *,
    index: SectionIndex,
    skill_id: str,
    section_id: str,
    diagnostics: tuple[MetadataDiagnostic, ...],
    stale: bool = False,
) -> ReadSectionResult:
    return ReadSectionResult(
        skill_id=skill_id,
        content_revision=index.revision,
        section_id=section_id,
        total=len(index.preamble) + sum(len(section.text) for section in index.sections),
        stale=stale,
        diagnostics=diagnostics,
    )


def read_section(
    content: str,
    section_id: str,
    *,
    skill_id: str = "",
    metadata: SkillMetadata | None = None,
    expected_revision: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> ReadSectionResult:
    """Read one section against an explicit content revision.

    Resolution order: a declared schema-v2 section id (bound through A1's
    heading anchors) wins; otherwise the parsed anchor/heading is used, and a
    diagnostic records that the read was undeclared. ``expected_revision``
    binds continuations: a mismatch returns ``stale=True`` with empty text
    instead of a passage from edited content.
    """
    text = content or ""
    index = index_sections(text)
    if expected_revision is not None and expected_revision != index.revision:
        return _empty_result(
            index=index,
            skill_id=skill_id,
            section_id=section_id,
            diagnostics=(
                MetadataDiagnostic(
                    code=CODE_SECTION_STALE,
                    message=(
                        f"section {section_id!r} was read against revision "
                        f"{index.revision}; the requested revision {expected_revision} "
                        "no longer matches the current content"
                    ),
                    skill_id=skill_id,
                ),
            ),
            stale=True,
        )

    section: ParsedSection | None = None
    address = section_id
    bound = False
    diagnostics: list[MetadataDiagnostic] = []
    if metadata is not None:
        bound_metadata, bind_diags = bound_sections(metadata, text)
        declared = bound_metadata.get_section(section_id)
        if declared is not None:
            address = declared.id
            diagnostics.extend(_relevant_diagnostics(bind_diags, declared.id, declared.heading))
            if declared.anchor is not None:
                section = index.find_by_line(declared.anchor.line)
                bound = section is not None
            if section is None:
                if declared.anchor is not None:
                    diagnostics.append(
                        MetadataDiagnostic(
                            code=CODE_SECTION_UNKNOWN,
                            message=(
                                f"section {declared.id!r} was bound to line "
                                f"{declared.anchor.line} but no parsed section matches"
                            ),
                            skill_id=skill_id,
                        )
                    )
                return _empty_result(
                    index=index,
                    skill_id=skill_id,
                    section_id=section_id,
                    diagnostics=tuple(diagnostics),
                )
        else:
            section = index.find(section_id)
            if section is not None:
                address = section.section_id
                diagnostics.append(
                    MetadataDiagnostic(
                        code=CODE_SECTION_UNDECLARED,
                        message=(
                            f"section {section_id!r} is not declared by the skill metadata; "
                            f"served the parsed anchor {section.section_id!r}"
                        ),
                        skill_id=skill_id,
                        severity="warning",
                    )
                )
            else:
                diagnostics.append(
                    MetadataDiagnostic(
                        code=CODE_SECTION_UNKNOWN,
                        message=(
                            f"neither the declared sections nor the parsed headings contain "
                            f"section {section_id!r}"
                        ),
                        skill_id=skill_id,
                    )
                )
                return _empty_result(
                    index=index,
                    skill_id=skill_id,
                    section_id=section_id,
                    diagnostics=tuple(diagnostics),
                )
    else:
        section = index.find(section_id)
        if section is None:
            diagnostics.append(
                MetadataDiagnostic(
                    code=CODE_SECTION_UNKNOWN,
                    message=(
                        f"parsed headings contain no section {section_id!r}; "
                        "the skill may be a v1 document without declarable sections"
                    ),
                    skill_id=skill_id,
                )
            )
            return _empty_result(
                index=index,
                skill_id=skill_id,
                section_id=section_id,
                diagnostics=tuple(diagnostics),
            )
        address = section.section_id

    section_total = len(section.text)
    if offset < 0 or offset > section_total or (limit is not None and limit < 0):
        diagnostics.append(
            MetadataDiagnostic(
                code=CODE_SECTION_RANGE,
                message=(
                    f"offset {offset} / limit {limit} are outside section "
                    f"{section.section_id!r} (0..{section_total})"
                ),
                skill_id=skill_id,
            )
        )
        return ReadSectionResult(
            skill_id=skill_id,
            content_revision=index.revision,
            section_id=address,
            heading=section.heading,
            start=section.start,
            end=section.end,
            total=len(text),
            section_total=section_total,
            line=section.line,
            bound=bound,
            anchor_id=section.section_id,
            diagnostics=tuple(diagnostics),
        )
    end = section_total if limit is None else min(section_total, offset + limit)
    served = section.text[offset:end]
    continuation = end if end < section_total else None
    return ReadSectionResult(
        skill_id=skill_id,
        content_revision=index.revision,
        section_id=address,
        heading=section.heading,
        text=served,
        start=section.start,
        end=section.end,
        total=len(text),
        section_total=section_total,
        line=section.line,
        continuation=continuation,
        bound=bound,
        anchor_id=section.section_id,
        diagnostics=tuple(diagnostics),
    )


def read_skill_section(
    skill: Any,
    section_id: str,
    *,
    content: str | None = None,
    expected_revision: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> ReadSectionResult:
    """Version-bound read for a loaded ``Skill`` object.

    ``content`` overrides the body when the real instructions live outside
    ``skill.content`` (bundled skills keep them in the pinned bundle); callers
    that have a registry should pass ``registry.get_skill_content(name)``.
    """
    body = content
    if body is None:
        body = getattr(skill, "content", "") or ""
    return read_section(
        body,
        section_id,
        skill_id=str(getattr(skill, "name", "")),
        metadata=getattr(skill, "skill_metadata", None),
        expected_revision=expected_revision,
        offset=offset,
        limit=limit,
    )


class SectionCycleError(ValueError):
    """Raised when declared ``requires_sections`` edges form a cycle."""

    def __init__(self, message: str, diagnostics: tuple[MetadataDiagnostic, ...] = ()) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class SectionGraph:
    """Bounded acyclic requires_sections graph (declared order preserved)."""

    edges: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def is_acyclic(self) -> bool:
        try:
            for section_id in self.edges:
                self.closure(section_id)
        except SectionCycleError:
            return False
        return True

    def closure(self, section_id: str) -> tuple[str, ...]:
        """Transitive prerequisites of *section_id*, prerequisites first."""
        ordered: list[str] = []
        state: dict[str, int] = {}

        def visit(identifier: str) -> None:
            status = state.get(identifier, 0)
            if status == 2:
                return
            if status == 1:
                raise SectionCycleError(f"requires_sections cycle through {identifier!r}")
            state[identifier] = 1
            for required in self.edges.get(identifier, ()):
                visit(required)
            state[identifier] = 2
            if identifier != section_id:
                ordered.append(identifier)

        visit(section_id)
        return tuple(ordered)


def build_section_graph(
    metadata: SkillMetadata | None,
) -> tuple[SectionGraph, tuple[MetadataDiagnostic, ...]]:
    """Build the declared dependency graph; reject cycles, drop unknown targets."""
    if metadata is None or not metadata.sections:
        return SectionGraph(), ()
    declared = {section.id for section in metadata.sections}
    edges: dict[str, tuple[str, ...]] = {}
    diagnostics: list[MetadataDiagnostic] = []
    for section in metadata.sections:
        required: list[str] = []
        for dependency in section.requires_sections:
            if dependency not in declared:
                diagnostics.append(
                    MetadataDiagnostic(
                        code=CODE_DEPENDENCY_UNKNOWN,
                        message=(
                            f"section {section.id!r} requires unknown section {dependency!r}; "
                            "the edge was dropped"
                        ),
                        skill_id=metadata.skill_id,
                    )
                )
                continue
            required.append(dependency)
        edges[section.id] = tuple(required)
    graph = SectionGraph(edges=edges)
    try:
        for section_id in edges:
            graph.closure(section_id)
    except SectionCycleError as exc:
        cycle_diags = [
            *diagnostics,
            MetadataDiagnostic(
                code=CODE_DEPENDENCY_CYCLE,
                message=str(exc),
                skill_id=metadata.skill_id,
                severity="error",
            ),
        ]
        raise SectionCycleError(str(exc), diagnostics=tuple(cycle_diags)) from None
    return graph, tuple(diagnostics)
