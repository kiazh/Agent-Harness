"""Validated catalog metadata (skill schema v2) and bounded catalog diagnostics.

This module owns the *additive*, *inert* catalog metadata introduced for
deterministic skill routing (spec §7.2). It is a pure data layer: nothing here
grants authority, changes provenance/trust, or executes instructions. The
registry attaches the parsed object as ``Skill.skill_metadata`` and exposes the
bounded diagnostics through ``SkillRegistry.metadata_diagnostics()``.

Explicit policy — every point below is pinned by tests
(``tests/test_skills_metadata.py``):

* **v1 untouched.** A front matter without ``schema_version`` is v1: no
  metadata is invented (``skill_metadata is None``) and parsing is bit-for-bit
  the old behavior.
* **schema_version** must be the integer ``2``. Anything else (including
  ``true``, ``2.0``, ``3``) degrades to baseline with a
  ``metadata-unsupported-schema``/``metadata-invalid`` diagnostic.
* **Unknown top-level fields** are preserved verbatim as opaque
  ``extensions`` data — never interpreted as routing rules — with a
  ``metadata-unsupported-field`` diagnostic. Authority-shaped names
  (``permission*``, ``authority``, ``trust*``, ``grant*``, ``privilege*``,
  ``allowed_tool*``, ``*escalat*``) are dropped entirely with a
  ``metadata-authority-field`` diagnostic; they are never even preserved.
* **Closed vocabularies** (task families, capabilities, section stages) are
  listed below. Unknown values are dropped with a
  ``metadata-unknown-value`` diagnostic — never silently accepted into the
  vocabulary. Extending a vocabulary requires a code change and tests.
* **Bounds.** List length ``MAX_LIST_ITEMS``/``MAX_SECTIONS``/
  ``MAX_REQUIRES_PER_SECTION``, field sizes, extension count/size, and the
  dependency depth ``MAX_DEPENDENCY_DEPTH`` are enforced. Oversize is a
  rejection (baseline fallback) with ``metadata-oversized``.
* **Required-section graph.** Unknown targets are dropped with
  ``dependency-unknown`` (non-fatal). Cycles and over-depth chains reject the
  whole metadata (``dependency-cycle``/``dependency-depth``) so no consumer
  ever sees an unusable graph.
* **Section anchors.** Declared headings bind case-insensitively to the first
  matching content heading (``section-missing-anchor`` when absent,
  ``section-duplicate-heading`` for repeated content/declared headings). The
  line number is 1-based within the parsed body content (``skill.content``),
  not the raw file.
* **Sidecars** (spec §7.3). ``machina/skills/enrichment/<revision>.json``
  holds local routing enrichment for bundled ECC content, keyed by skill id
  and bound to the bundle revision. A mismatched revision/file name yields
  ``stale-enrichment`` and baseline fallback. Sidecar entries accept only the
  closed v2 field set (no extensions); authority-shaped fields reject the
  entry with ``enrichment-authority-field``. Sidecars cannot alter
  provenance/trust and grant no permissions — they only produce the same inert
  ``SkillMetadata`` objects as front matter, tagged ``origin="sidecar"``.
* **Diagnostics are bounded** to ``MAX_DIAGNOSTICS`` per load with a
  ``diagnostics-truncated`` marker, and messages are truncated to
  ``MAX_DIAGNOSTIC_MESSAGE`` characters.

The module imports nothing from ``machina``; the registry reaches it through a
deferred import to keep its own module-level edges pinned to
``skills.ports`` + ``observability.diagnostics`` (see
``tests/test_architecture_pairs.py``).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

#: Diagnostic sink signature used by the field validators.
_Warn = Callable[[str, str], None]

SCHEMA_VERSION = 2
#: Version of the JSON sidecar envelope (not of the per-skill metadata).
SIDECAR_SCHEMA_VERSION = 1

#: Closed vocabulary of routing task families. Extend only with tests.
TASK_FAMILIES = frozenset(
    {
        "backend-api",
        "data-analysis",
        "debugging",
        "deployment",
        "documentation",
        "frontend-review",
        "maintenance",
        "refactoring",
        "research",
        "security-review",
        "testing",
        "web-build",
    }
)

#: Closed vocabulary of concrete capabilities. Extend only with tests.
CAPABILITIES = frozenset(
    {
        "accessibility",
        "api-design",
        "authentication",
        "cli",
        "configuration",
        "database",
        "error-handling",
        "layout",
        "migration",
        "observability",
        "performance",
        "responsive-ui",
        "test-authoring",
        "type-safety",
    }
)

#: Closed vocabulary of instruction-section stages. Extend only with tests.
STAGES = frozenset(
    {"plan", "inspect", "implement", "verify", "debug", "review", "deliver", "maintain"}
)

#: v1 front-matter keys already consumed by ``SkillParser``; they are neither
#: v2 metadata nor extension data.
V1_FRONTMATTER_FIELDS = frozenset(
    {
        "name",
        "description",
        "triggers",
        "version",
        "enabled",
        "created_at",
        "source",
        "source_type",
    }
)

V2_FIELDS = frozenset(
    {
        "schema_version",
        "skill_id",
        "task_families",
        "capabilities",
        "aliases",
        "stack",
        "preconditions",
        "required_tools",
        "exclusions",
        "sections",
    }
)

#: Provenance recorded when catalog metadata is machine-generated at install
#: time (``machina.skills.enrich``). Recognized in front matter — never an
#: opaque extension warning, never routed on, never authority-shaped — and
#: deliberately excluded from the strict sidecar field set (sidecars are
#: machine-written routing data, not an install audit trail).
PROVENANCE_FIELDS = frozenset({"metadata_source", "metadata_generated_at"})
STACK_FIELDS = frozenset({"languages", "frameworks", "independent"})
SECTION_FIELDS = frozenset({"id", "heading", "stage", "requires_sections"})

MAX_LIST_ITEMS = 32
MAX_SECTIONS = 32
MAX_REQUIRES_PER_SECTION = 16
MAX_DEPENDENCY_DEPTH = 8
MAX_IDENTIFIER_LENGTH = 80
MAX_STRING_LENGTH = 300
MAX_EXTENSION_FIELDS = 16
MAX_EXTENSION_VALUE_BYTES = 2048
MAX_ENRICHMENT_SKILLS = 64
MAX_DIAGNOSTICS = 100
MAX_DIAGNOSTIC_MESSAGE = 300

# ─── diagnostic codes (closed vocabulary) ───────────────────────────────────

CODE_METADATA_INVALID = "metadata-invalid"
CODE_UNSUPPORTED_SCHEMA = "metadata-unsupported-schema"
CODE_UNSUPPORTED_FIELD = "metadata-unsupported-field"
CODE_AUTHORITY_FIELD = "metadata-authority-field"
CODE_OVERSIZED = "metadata-oversized"
CODE_UNKNOWN_VALUE = "metadata-unknown-value"
CODE_SECTION_MISSING_ANCHOR = "section-missing-anchor"
CODE_SECTION_DUPLICATE_HEADING = "section-duplicate-heading"
CODE_SECTION_DUPLICATE_ID = "section-duplicate-id"
CODE_DEPENDENCY_UNKNOWN = "dependency-unknown"
CODE_DEPENDENCY_CYCLE = "dependency-cycle"
CODE_DEPENDENCY_DEPTH = "dependency-depth"
CODE_DUPLICATE_SKILL_ID = "duplicate-skill-id"
CODE_SKILL_LOAD_FAILED = "skill-load-failed"
CODE_STALE_ENRICHMENT = "stale-enrichment"
CODE_ENRICHMENT_INVALID = "enrichment-invalid"
CODE_ENRICHMENT_AUTHORITY = "enrichment-authority-field"
CODE_ENRICHMENT_UNKNOWN_SKILL = "enrichment-unknown-skill"
CODE_DIAGNOSTICS_TRUNCATED = "diagnostics-truncated"

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,79}")
_SECTION_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")
_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_WHITESPACE_RE = re.compile(r"\s+")

_AUTHORITY_TOKENS = (
    "permission",
    "authority",
    "trust",
    "grant",
    "privilege",
    "escalat",
    "allowed_tool",
    "tool_grant",
)


def _looks_like_authority_field(key: str) -> bool:
    """True for field names that claim authority/trust (never preserved)."""
    folded = key.casefold()
    return any(token in folded for token in _AUTHORITY_TOKENS)


class _MetadataError(ValueError):
    """Internal fatal validation failure carrying its diagnostic code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ─── diagnostics ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MetadataDiagnostic:
    """One bounded, inspectable catalog diagnosis.

    ``severity`` is ``"warning"`` when the skill still loaded with usable
    metadata and ``"error"`` when data was rejected (baseline fallback or the
    skill was skipped entirely).
    """

    code: str
    message: str
    skill_id: str = ""
    file_path: str = ""
    severity: str = "warning"

    def __post_init__(self) -> None:
        if len(self.message) > MAX_DIAGNOSTIC_MESSAGE:
            object.__setattr__(self, "message", self.message[: MAX_DIAGNOSTIC_MESSAGE - 1] + "…")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "skill_id": self.skill_id,
            "file_path": self.file_path,
            "severity": self.severity,
        }


class DiagnosticCollector:
    """Bounded diagnostic sink; never lets one bad skill flood the report."""

    def __init__(self, limit: int = MAX_DIAGNOSTICS) -> None:
        self._limit = max(1, int(limit))
        self._items: list[MetadataDiagnostic] = []
        self._dropped = 0

    @property
    def truncated(self) -> bool:
        return self._dropped > 0

    def add(self, diagnostic: MetadataDiagnostic) -> None:
        if len(self._items) < self._limit:
            self._items.append(diagnostic)
        else:
            self._dropped += 1

    def extend(self, diagnostics: Iterable[MetadataDiagnostic]) -> None:
        for diagnostic in diagnostics:
            self.add(diagnostic)

    def finish(self) -> list[MetadataDiagnostic]:
        items = list(self._items)
        if self._dropped:
            marker = MetadataDiagnostic(
                code=CODE_DIAGNOSTICS_TRUNCATED,
                message=(
                    f"{self._dropped} further diagnostic(s) dropped; "
                    f"the report is bounded at {self._limit}"
                ),
            )
            if len(items) >= self._limit:
                items[-1] = marker
            else:
                items.append(marker)
        return items


# ─── metadata objects ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SectionAnchor:
    """Where a declared section heading was found in the body content."""

    heading: str
    level: int
    line: int  # 1-based line within ``Skill.content`` (not the raw file)
    occurrence: int = 0  # 0 for the first matching heading; duplicates do not re-bind

    def to_dict(self) -> dict[str, Any]:
        return {
            "heading": self.heading,
            "level": self.level,
            "line": self.line,
            "occurrence": self.occurrence,
        }


@dataclass(frozen=True)
class SectionMetadata:
    """One declared instruction section (id + heading + stage + prerequisites)."""

    id: str
    heading: str
    stage: str | None = None
    requires_sections: tuple[str, ...] = ()
    anchor: SectionAnchor | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "heading": self.heading,
            "requires_sections": list(self.requires_sections),
        }
        if self.stage is not None:
            data["stage"] = self.stage
        return data


@dataclass(frozen=True)
class StackMetadata:
    """Declared language/framework stack; empty lists mean "unspecified"."""

    languages: tuple[str, ...] = ()
    frameworks: tuple[str, ...] = ()
    independent: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "languages": list(self.languages),
            "frameworks": list(self.frameworks),
            "independent": self.independent,
        }


@dataclass(frozen=True)
class SkillMetadata:
    """Validated schema-v2 catalog metadata attached to a loaded ``Skill``.

    Fields are inert routing/description data. There is deliberately no
    permission, authority, or provenance surface here; ``origin`` records only
    where the metadata was read from (``"frontmatter"`` or sidecar).
    """

    schema_version: int = SCHEMA_VERSION
    skill_id: str = ""
    task_families: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    stack: StackMetadata = field(default_factory=StackMetadata)
    preconditions: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    sections: tuple[SectionMetadata, ...] = ()
    extensions: dict[str, Any] = field(default_factory=dict)
    origin: str = "frontmatter"

    def to_dict(self) -> dict[str, Any]:
        """Serialize back to a front-matter-shaped mapping.

        Opaque ``extensions`` are inlined at the top level so that
        ``parse_skill_metadata(meta.to_dict(), ...) == meta``. Section anchors
        are derived from content and therefore intentionally not serialized.
        """
        data: dict[str, Any] = dict(self.extensions)
        data.update(
            {
                "schema_version": self.schema_version,
                "skill_id": self.skill_id,
                "task_families": list(self.task_families),
                "capabilities": list(self.capabilities),
                "aliases": list(self.aliases),
                "stack": self.stack.to_dict(),
                "preconditions": list(self.preconditions),
                "required_tools": list(self.required_tools),
                "exclusions": list(self.exclusions),
                "sections": [section.to_dict() for section in self.sections],
            }
        )
        return data

    def get_section(self, section_id: str) -> SectionMetadata | None:
        for section in self.sections:
            if section.id == section_id:
                return section
        return None


# ─── parse + validate ────────────────────────────────────────────────────────


def parse_skill_metadata(
    frontmatter: Mapping[str, Any],
    *,
    skill_name: str,
    content: str = "",
    file_path: str = "",
) -> tuple[SkillMetadata | None, list[MetadataDiagnostic]]:
    """Parse front matter into validated metadata.

    Returns ``(None, [])`` for v1 documents (no ``schema_version``) so the old
    behavior is untouched. Any invalid v2 document returns ``(None, diags)``:
    the skill still loads with baseline metadata and one bad document never
    raises (spec §7.2, §7.4). ``content`` anchors declared sections; pass
    ``content=None`` to skip anchor binding when the body is not available
    (sidecar enrichment for lazily-read bundle content).
    """
    if "schema_version" not in frontmatter:
        return None, []
    return _build_metadata(
        frontmatter,
        skill_name=skill_name,
        origin="frontmatter",
        heading_content=content,
        extensions_policy="preserve",
        file_path=file_path,
    )


def _build_metadata(
    values: Mapping[str, Any],
    *,
    skill_name: str,
    origin: str,
    heading_content: str | None,
    extensions_policy: str,
    file_path: str,
) -> tuple[SkillMetadata | None, list[MetadataDiagnostic]]:
    diags: list[MetadataDiagnostic] = []

    def warn(code: str, message: str) -> None:
        diags.append(
            MetadataDiagnostic(code=code, message=message, skill_id=skill_name, file_path=file_path)
        )

    try:
        declared = values.get("schema_version", SCHEMA_VERSION)
        if (
            isinstance(declared, bool)
            or not isinstance(declared, int)
            or declared != SCHEMA_VERSION
        ):
            raise _MetadataError(
                CODE_UNSUPPORTED_SCHEMA,
                f"schema_version {declared!r} is not supported; expected {SCHEMA_VERSION}",
            )

        skill_id = values.get("skill_id")
        if not isinstance(skill_id, str) or not _NAME_RE.fullmatch(skill_id):
            raise _MetadataError(
                CODE_METADATA_INVALID,
                "skill_id must be a short alphanumeric slug of at least 3 characters",
            )
        if skill_id.casefold() != skill_name.casefold():
            raise _MetadataError(
                CODE_METADATA_INVALID,
                f"skill_id {skill_id!r} does not match the skill name {skill_name!r}",
            )

        task_families = _enum_list(values, "task_families", TASK_FAMILIES, warn)
        capabilities = _enum_list(values, "capabilities", CAPABILITIES, warn)
        aliases = _alias_list(values.get("aliases"))
        stack = _stack(value=values.get("stack"), warn=warn)
        preconditions = _text_list(values, "preconditions")
        required_tools = _text_list(values, "required_tools")
        exclusions = _text_list(values, "exclusions")
        sections = _sections(values.get("sections"), warn=warn)

        ids = {section.id for section in sections}
        resolved: list[SectionMetadata] = []
        for section in sections:
            kept: list[str] = []
            for target in section.requires_sections:
                if target in ids:
                    kept.append(target)
                else:
                    warn(
                        CODE_DEPENDENCY_UNKNOWN,
                        f"section {section.id!r} requires unknown section {target!r}; edge dropped",
                    )
            resolved.append(replace(section, requires_sections=tuple(kept)))
        sections = _check_dependency_depth(tuple(resolved))

        extensions = _extensions(values, policy=extensions_policy, warn=warn)
    except _MetadataError as exc:
        diags.append(
            MetadataDiagnostic(
                code=exc.code,
                message=str(exc),
                skill_id=skill_name,
                file_path=file_path,
                severity="error",
            )
        )
        return None, diags

    metadata = SkillMetadata(
        skill_id=skill_id,
        task_families=task_families,
        capabilities=capabilities,
        aliases=aliases,
        stack=stack,
        preconditions=preconditions,
        required_tools=required_tools,
        exclusions=exclusions,
        sections=sections,
        extensions=extensions,
        origin=origin,
    )
    if heading_content is not None:
        metadata, anchor_diags = bind_section_anchors(metadata, heading_content)
        for diagnostic in anchor_diags:
            if not diagnostic.file_path:
                diagnostic = replace(diagnostic, file_path=file_path)
            diags.append(diagnostic)
    return metadata, diags


def bind_section_anchors(
    metadata: SkillMetadata, content: str
) -> tuple[SkillMetadata, list[MetadataDiagnostic]]:
    """Bind declared sections to content headings (deterministic first match).

    Reusable by consumers that read content later (for example sidecar
    enrichment over lazily-read bundle instructions): the returned metadata is
    identical apart from resolved ``anchor`` values, and repeated or missing
    headings produce identical diagnostics every time.
    """
    index = _heading_index(content)
    claimed: dict[str, str] = {}
    diags: list[MetadataDiagnostic] = []
    resolved: list[SectionMetadata] = []
    for section in metadata.sections:
        key = _heading_key(section.heading)
        matches = index.get(key, ())
        anchor = matches[0] if matches else None
        if anchor is None:
            diags.append(
                MetadataDiagnostic(
                    code=CODE_SECTION_MISSING_ANCHOR,
                    message=(
                        f"section {section.id!r} heading {section.heading!r} "
                        "was not found in the instruction content"
                    ),
                    skill_id=metadata.skill_id,
                )
            )
        elif len(matches) > 1:
            diags.append(
                MetadataDiagnostic(
                    code=CODE_SECTION_DUPLICATE_HEADING,
                    message=(
                        f"heading {section.heading!r} appears {len(matches)} times; "
                        f"section {section.id!r} bound to the first occurrence (line {anchor.line})"
                    ),
                    skill_id=metadata.skill_id,
                )
            )
        if key in claimed:
            diags.append(
                MetadataDiagnostic(
                    code=CODE_SECTION_DUPLICATE_HEADING,
                    message=(
                        f"heading {section.heading!r} is declared by sections "
                        f"{claimed[key]!r} and {section.id!r}; both bind to the first occurrence"
                    ),
                    skill_id=metadata.skill_id,
                )
            )
        else:
            claimed[key] = section.id
        resolved.append(replace(section, anchor=anchor))
    return replace(metadata, sections=tuple(resolved)), diags


def _heading_key(heading: str) -> str:
    return _WHITESPACE_RE.sub(" ", heading.strip()).casefold()


def _heading_index(content: str) -> dict[str, tuple[SectionAnchor, ...]]:
    index: dict[str, list[SectionAnchor]] = {}
    for line_number, line in enumerate(content.splitlines(), start=1):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        heading = _WHITESPACE_RE.sub(" ", match.group(2).strip())
        key = heading.casefold()
        entries = index.setdefault(key, [])
        entries.append(
            SectionAnchor(
                heading=heading,
                level=len(match.group(1)),
                line=line_number,
                occurrence=len(entries),
            )
        )
    return {key: tuple(entries) for key, entries in index.items()}


# ─── field helpers ───────────────────────────────────────────────────────────


def _list(value: Any, field_name: str, *, limit: int = MAX_LIST_ITEMS) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _MetadataError(CODE_METADATA_INVALID, f"{field_name} must be a list")
    if len(value) > limit:
        raise _MetadataError(
            CODE_OVERSIZED, f"{field_name} has {len(value)} items; the maximum is {limit}"
        )
    return value


def _text_list(values: Mapping[str, Any], field_name: str) -> tuple[str, ...]:
    out: list[str] = []
    for item in _list(values.get(field_name), field_name):
        if not isinstance(item, str) or not item.strip() or len(item) > MAX_STRING_LENGTH:
            raise _MetadataError(
                CODE_METADATA_INVALID,
                f"{field_name} items must be non-empty strings of at most "
                f"{MAX_STRING_LENGTH} characters",
            )
        out.append(item)
    return tuple(out)


def _enum_list(
    values: Mapping[str, Any], field_name: str, vocabulary: frozenset[str], warn: _Warn
) -> tuple[str, ...]:
    out: list[str] = []
    for item in _list(values.get(field_name), field_name):
        if not isinstance(item, str) or not item.strip() or len(item) > MAX_IDENTIFIER_LENGTH:
            raise _MetadataError(
                CODE_METADATA_INVALID, f"{field_name} items must be short non-empty strings"
            )
        canonical = item.casefold()
        if canonical not in vocabulary:
            warn(
                CODE_UNKNOWN_VALUE,
                f"{field_name} value {item!r} is not in the documented vocabulary; dropped",
            )
            continue
        if canonical not in out:
            out.append(canonical)
    return tuple(out)


def _alias_list(value: Any) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for item in _list(value, "aliases"):
        if not isinstance(item, str) or not _ALIAS_RE.fullmatch(item):
            raise _MetadataError(CODE_METADATA_INVALID, "aliases must be short alphanumeric slugs")
        folded = item.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        out.append(item)
    return tuple(out)


def _string_tuple(
    value: Any,
    field_name: str,
    *,
    limit: int = MAX_LIST_ITEMS,
    max_length: int = MAX_IDENTIFIER_LENGTH,
) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for item in _list(value, field_name, limit=limit):
        if not isinstance(item, str) or not item.strip() or len(item) > max_length:
            raise _MetadataError(
                CODE_METADATA_INVALID, f"{field_name} items must be short non-empty strings"
            )
        folded = item.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        out.append(item)
    return tuple(out)


def _stack(value: Any, *, warn: _Warn) -> StackMetadata:
    if value is None:
        return StackMetadata()
    if not isinstance(value, dict):
        raise _MetadataError(CODE_METADATA_INVALID, "stack must be a mapping")
    for key in sorted(key for key in value if key not in STACK_FIELDS):
        warn(CODE_UNSUPPORTED_FIELD, f"stack field {key!r} is not part of schema v2; ignored")
    languages = _string_tuple(value.get("languages"), "stack.languages")
    frameworks = _string_tuple(value.get("frameworks"), "stack.frameworks")
    independent = value.get("independent", True)
    if not isinstance(independent, bool):
        raise _MetadataError(CODE_METADATA_INVALID, "stack.independent must be a boolean")
    return StackMetadata(languages=languages, frameworks=frameworks, independent=independent)


def _sections(value: Any, *, warn: _Warn) -> tuple[SectionMetadata, ...]:
    out: list[SectionMetadata] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(_list(value, "sections", limit=MAX_SECTIONS)):
        if not isinstance(item, dict):
            raise _MetadataError(CODE_METADATA_INVALID, f"sections[{index}] must be a mapping")
        for key in sorted(key for key in item if key not in SECTION_FIELDS):
            warn(
                CODE_UNSUPPORTED_FIELD,
                f"sections[{index}] field {key!r} is not part of schema v2; ignored",
            )
        section_id = item.get("id")
        if not isinstance(section_id, str) or not _SECTION_ID_RE.fullmatch(section_id):
            raise _MetadataError(
                CODE_METADATA_INVALID, f"sections[{index}].id must be a lowercase slug"
            )
        if section_id in seen_ids:
            raise _MetadataError(CODE_SECTION_DUPLICATE_ID, f"duplicate section id {section_id!r}")
        seen_ids.add(section_id)
        heading = item.get("heading")
        if not isinstance(heading, str) or not heading.strip() or len(heading) > MAX_STRING_LENGTH:
            raise _MetadataError(
                CODE_METADATA_INVALID,
                f"sections[{index}].heading must be a non-empty string of at most "
                f"{MAX_STRING_LENGTH} characters",
            )
        stage = item.get("stage")
        if stage is not None:
            if not isinstance(stage, str) or len(stage) > MAX_IDENTIFIER_LENGTH:
                raise _MetadataError(
                    CODE_METADATA_INVALID,
                    f"sections[{index}].stage must be a short string when present",
                )
            canonical = stage.casefold()
            if canonical in STAGES:
                stage = canonical
            else:
                warn(
                    CODE_UNKNOWN_VALUE,
                    f"sections[{index}].stage value {stage!r} is not in the documented "
                    "vocabulary; treated as unspecified",
                )
                stage = None
        requires = _string_tuple(
            item.get("requires_sections", []),
            f"sections[{index}].requires_sections",
            limit=MAX_REQUIRES_PER_SECTION,
        )
        out.append(
            SectionMetadata(id=section_id, heading=heading, stage=stage, requires_sections=requires)
        )
    return tuple(out)


def _check_dependency_depth(
    sections: tuple[SectionMetadata, ...],
) -> tuple[SectionMetadata, ...]:
    requires = {section.id: section.requires_sections for section in sections}
    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def depth(section_id: str) -> int:
        if section_id in memo:
            return memo[section_id]
        if section_id in visiting:
            raise _MetadataError(
                CODE_DEPENDENCY_CYCLE,
                f"required-section dependency cycle through {section_id!r}",
            )
        visiting.add(section_id)
        longest = 1
        for target in requires[section_id]:
            longest = max(longest, 1 + depth(target))
        visiting.discard(section_id)
        memo[section_id] = longest
        return longest

    for section_id in requires:
        if depth(section_id) > MAX_DEPENDENCY_DEPTH:
            raise _MetadataError(
                CODE_DEPENDENCY_DEPTH,
                f"required-section dependency depth exceeds {MAX_DEPENDENCY_DEPTH}",
            )
    return sections


def _extensions(values: Mapping[str, Any], *, policy: str, warn: _Warn) -> dict[str, Any]:
    # Front matter may carry any v1 key (consumed by SkillParser) plus opaque
    # extension data. Sidecar entries are restricted to the closed v2 field
    # set: a smuggled ``description``/``source_type`` must be rejected, not
    # silently accepted and ignored.
    known = V1_FRONTMATTER_FIELDS | V2_FIELDS if policy != "reject" else V2_FIELDS
    if policy != "reject":
        known |= PROVENANCE_FIELDS
    extensions: dict[str, Any] = {}
    dropped_for_size = 0
    for key in sorted(key for key in values if key not in known):
        if not isinstance(key, str):
            if policy == "reject":
                raise _MetadataError(CODE_ENRICHMENT_INVALID, "sidecar fields must be strings")
            continue
        if _looks_like_authority_field(key):
            if policy == "reject":
                raise _MetadataError(
                    CODE_ENRICHMENT_AUTHORITY,
                    f"sidecar field {key!r} is authority-shaped; entry rejected",
                )
            warn(
                CODE_AUTHORITY_FIELD,
                f"field {key!r} is authority-shaped and dropped; metadata grants nothing",
            )
            continue
        if policy == "reject":
            raise _MetadataError(
                CODE_ENRICHMENT_INVALID, f"field {key!r} is not part of the sidecar schema"
            )
        if len(extensions) >= MAX_EXTENSION_FIELDS:
            dropped_for_size += 1
            continue
        try:
            payload = json.dumps(values[key])
        except (TypeError, ValueError):
            warn(
                CODE_UNSUPPORTED_FIELD,
                f"field {key!r} is not JSON-serializable; dropped",
            )
            continue
        if len(payload.encode("utf-8")) > MAX_EXTENSION_VALUE_BYTES:
            warn(
                CODE_OVERSIZED,
                f"field {key!r} exceeds {MAX_EXTENSION_VALUE_BYTES} bytes; dropped",
            )
            continue
        extensions[key] = values[key]
        warn(
            CODE_UNSUPPORTED_FIELD,
            f"field {key!r} is not part of schema v2; preserved as opaque extension "
            "data, never interpreted",
        )
    if dropped_for_size:
        warn(
            CODE_OVERSIZED,
            f"{dropped_for_size} further unknown field(s) exceed the "
            f"{MAX_EXTENSION_FIELDS}-field extension bound; dropped",
        )
    return extensions


# ─── ECC enrichment sidecars (spec §7.3) ─────────────────────────────────────
#
# Storage: one JSON file per bundle revision at
# ``machina/skills/enrichment/<revision>.json`` (override with
# ``MACHINA_SKILL_ENRICHMENT_DIR``), where the file name and the body's
# ``revision`` must both equal the ECC manifest revision the enrichment was
# written against. Envelope:
#
#     {
#       "schema_version": 1,
#       "revision": "<ecc manifest revision>",
#       "skills": {
#         "<bundled skill id>": {"task_families": [...], "capabilities": [...], ...}
#       }
#     }
#
# Entries accept only the closed v2 field set (``schema_version``/``skill_id``
# optional; ``skill_id`` must match the key). Unknown keys reject the entry —
# sidecars are machine-written, so strictness is cheap and safe. Nothing here
# touches ``Skill.source``/``source_type`` or any permission surface.


def default_enrichment_dir() -> Path:
    """Module-owned sidecar directory (``machina/skills/enrichment/``)."""
    override = os.environ.get("MACHINA_SKILL_ENRICHMENT_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "enrichment"


def load_enrichment_sidecar(
    revision: str,
    *,
    skill_ids: Iterable[str],
    sidecar_dir: str | Path | None = None,
) -> tuple[dict[str, SkillMetadata], list[MetadataDiagnostic]]:
    """Load validated enrichment for *revision*, or safe baseline on mismatch.

    Returns ``({}, [])`` when no sidecar exists. A stale/mismatched revision,
    invalid JSON, or an invalid envelope returns ``({}, diags)`` so the caller
    falls back to baseline metadata (spec §7.3).
    """
    diags: list[MetadataDiagnostic] = []
    directory = Path(sidecar_dir) if sidecar_dir is not None else default_enrichment_dir()
    path = directory / f"{revision}.json"
    try:
        raw = path.read_bytes()
    except OSError:
        return {}, diags
    source = str(path)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        diags.append(
            MetadataDiagnostic(
                code=CODE_ENRICHMENT_INVALID,
                message=f"enrichment sidecar is not valid JSON: {exc}",
                file_path=source,
                severity="error",
            )
        )
        return {}, diags
    if not isinstance(data, dict) or data.get("schema_version") != SIDECAR_SCHEMA_VERSION:
        diags.append(
            MetadataDiagnostic(
                code=CODE_ENRICHMENT_INVALID,
                message=(
                    "enrichment sidecar must be a JSON object with "
                    f"schema_version {SIDECAR_SCHEMA_VERSION}"
                ),
                file_path=source,
                severity="error",
            )
        )
        return {}, diags
    declared = data.get("revision")
    if declared != revision:
        diags.append(
            MetadataDiagnostic(
                code=CODE_STALE_ENRICHMENT,
                message=(
                    f"enrichment sidecar was built for revision {declared!r} but the "
                    f"bundle revision is {revision!r}; falling back to baseline metadata"
                ),
                file_path=source,
            )
        )
        return {}, diags
    entries = data.get("skills")
    if not isinstance(entries, dict):
        diags.append(
            MetadataDiagnostic(
                code=CODE_ENRICHMENT_INVALID,
                message="enrichment sidecar 'skills' must be a mapping",
                file_path=source,
                severity="error",
            )
        )
        return {}, diags

    known = {candidate.casefold(): candidate for candidate in skill_ids}
    result: dict[str, SkillMetadata] = {}
    for index, (key, entry) in enumerate(sorted(entries.items(), key=lambda pair: str(pair[0]))):
        if index >= MAX_ENRICHMENT_SKILLS:
            diags.append(
                MetadataDiagnostic(
                    code=CODE_OVERSIZED,
                    message=(
                        f"enrichment sidecar exceeds {MAX_ENRICHMENT_SKILLS} skill entries; "
                        "the remainder is ignored"
                    ),
                    file_path=source,
                )
            )
            break
        if not isinstance(key, str) or not isinstance(entry, dict):
            diags.append(
                MetadataDiagnostic(
                    code=CODE_ENRICHMENT_INVALID,
                    message="enrichment entries must be mappings keyed by skill id",
                    file_path=source,
                    severity="error",
                )
            )
            continue
        canonical = known.get(key.casefold())
        if canonical is None:
            diags.append(
                MetadataDiagnostic(
                    code=CODE_ENRICHMENT_UNKNOWN_SKILL,
                    message=(
                        f"enrichment sidecar references unknown bundled skill {key!r}; "
                        "entry ignored"
                    ),
                    file_path=source,
                )
            )
            continue
        candidate = dict(entry)
        candidate.setdefault("skill_id", canonical)
        metadata, entry_diags = _build_metadata(
            candidate,
            skill_name=canonical,
            origin="sidecar",
            heading_content=None,
            extensions_policy="reject",
            file_path=source,
        )
        diags.extend(entry_diags)
        if metadata is not None:
            result[canonical] = metadata
    return result, diags
