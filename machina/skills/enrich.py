"""Once-only generation of validated schema-v2 catalog metadata at install time.

Deterministic skill routing reads ``Skill.skill_metadata`` (see
``machina.skills.metadata``). Authored skills carry it in front matter and
bundled ECC content carries it in sidecars, but a freshly installed or learned
skill typically arrives as a v1 SKILL.md with no metadata at all. This module
fills that gap exactly ONCE per skill: on install/learn the harness asks the
model for the v2 metadata, validates it through
``metadata.parse_skill_metadata``, and merges it into the SKILL.md front matter
(the instruction body is preserved byte-for-byte). Because the persisted
document then carries ``schema_version``, every later load and reinstall sees
existing metadata and never regenerates it.

The model call is injected as ``agent_seam`` — an async ``prompt -> text``
callable — so tests run against a fake and never touch a provider or the
network. Generation is fail-open: an invalid answer, a provider error, or a
persist failure leaves the installed skill exactly as it was (legacy baseline
behavior) and is reported, never silently swallowed.

Nothing here grants authority, changes provenance/trust, or executes
instructions: it only produces the same inert ``SkillMetadata`` objects the
front-matter and sidecar paths already produce.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import yaml

from machina.observability.diagnostics import record_failure
from machina.skills.metadata import (
    CAPABILITIES,
    CODE_METADATA_INVALID,
    MAX_LIST_ITEMS,
    MAX_SECTIONS,
    MAX_STRING_LENGTH,
    STAGES,
    TASK_FAMILIES,
    MetadataDiagnostic,
    SkillMetadata,
    parse_skill_metadata,
)
from machina.skills.registry import SkillParser, skill_registry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from machina.skills.registry import Skill, SkillRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "MetadataSeam",
    "default_agent_seam",
    "ensure_skill_metadata",
    "ensure_skill_metadata_sync",
    "generate_skill_metadata",
]

#: Injected model seam: one prompt in, one raw completion out.
MetadataSeam = Callable[[str], Awaitable[str]]

#: Generation bounds. Deliberately small: the answer is a compact YAML mapping
#: and the prompt is a single bounded request, so install latency stays modest.
MAX_CONTENT_CHARS = 6000
MAX_OUTPUT_TOKENS = 900
GENERATION_TIMEOUT_SECONDS = 30.0

_SHAPE = (
    "schema_version: 2\n"
    "skill_id: <the exact skill name>\n"
    "task_families: [1-3 allowed families]\n"
    "capabilities: [0-4 allowed capabilities]\n"
    "aliases: [short alternate slugs, optional]\n"
    "stack: {languages: [], frameworks: [], independent: true}\n"
    "preconditions: [short strings, optional]\n"
    "required_tools: [short strings, optional]\n"
    "exclusions: [short phrases for when NOT to use it, optional]\n"
    "sections:\n"
    "  - id: <lowercase slug>\n"
    "    heading: <exact heading text copied from the body>\n"
    "    stage: <one allowed stage>\n"
    "    requires_sections: []\n"
)


def build_metadata_prompt(skill_content: str, *, skill_name: str) -> str:
    """Build the single bounded metadata-generation prompt for one skill."""
    families = ", ".join(sorted(TASK_FAMILIES))
    capabilities = ", ".join(sorted(CAPABILITIES))
    stages = ", ".join(sorted(STAGES))
    body = (skill_content or "")[:MAX_CONTENT_CHARS]
    return (
        "Label exactly one skill for a deterministic catalog. Return ONLY a YAML "
        "mapping, no prose and no code fences, in this shape:\n"
        f"{_SHAPE}\n"
        f"Allowed task_families (choose 1-3): {families}.\n"
        f"Allowed capabilities (choose 0-4): {capabilities}.\n"
        f"Allowed section stages: {stages}.\n"
        "Rules: skill_id must equal the skill name exactly; every section heading "
        "must be copied verbatim from the body; list every claimed section in order. "
        f"Use at most {MAX_LIST_ITEMS} items per list and {MAX_SECTIONS} sections, and "
        f"keep every string under {MAX_STRING_LENGTH} characters. Do not invent steps.\n"
        f"Skill name: {skill_name}\n"
        "--- BEGIN SKILL BODY ---\n"
        f"{body}\n"
        "--- END SKILL BODY ---\n"
    )


def _decode_mapping(raw: Any) -> dict[str, Any] | None:
    """Decode a raw completion into a YAML mapping (tolerating code fences)."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0] if "```" in text else text
        text = text.strip()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _metadata_text(metadata: SkillMetadata) -> list[str]:
    """The catalog text of *metadata* that must survive the injection scan."""
    return [
        *metadata.aliases,
        *metadata.preconditions,
        *metadata.required_tools,
        *metadata.exclusions,
        *metadata.stack.languages,
        *metadata.stack.frameworks,
        *(section.heading for section in metadata.sections),
    ]


async def generate_skill_metadata(
    skill_content: str,
    *,
    skill_name: str,
    agent_seam: MetadataSeam,
    file_path: str = "",
    diagnostics: list[MetadataDiagnostic] | None = None,
) -> SkillMetadata | None:
    """Ask the model once for v2 metadata and validate it.

    Returns validated ``SkillMetadata`` or ``None``. Any failure (a provider
    error, a timeout, a non-mapping answer, or validation that rejects the
    document) appends one bounded diagnostic to *diagnostics* and returns
    ``None``; the caller keeps the skill on its baseline path. ``skill_id`` and
    ``schema_version`` are set from the real identity, never trusted to the
    model.
    """

    def _fail(message: str) -> None:
        diagnostic = MetadataDiagnostic(
            code=CODE_METADATA_INVALID,
            message=message,
            skill_id=skill_name,
            file_path=file_path,
            severity="error",
        )
        if diagnostics is not None:
            diagnostics.append(diagnostic)
        logger.warning("skill metadata generation rejected %s: %s", skill_name, message)

    prompt = build_metadata_prompt(skill_content, skill_name=skill_name)
    try:
        raw = await asyncio.wait_for(agent_seam(prompt), timeout=GENERATION_TIMEOUT_SECONDS)
    except Exception as exc:  # any provider/timeout failure is fail-open
        _fail(f"metadata generation did not complete ({type(exc).__name__})")
        return None
    mapping = _decode_mapping(raw)
    if mapping is None:
        _fail("metadata generation did not return a YAML mapping")
        return None
    mapping["schema_version"] = 2
    mapping["skill_id"] = skill_name
    metadata, parse_diags = parse_skill_metadata(
        mapping, skill_name=skill_name, content=skill_content, file_path=file_path
    )
    if diagnostics is not None:
        diagnostics.extend(parse_diags)
    if metadata is None:
        _fail("metadata generation failed schema-v2 validation")
        return None
    try:
        for value in _metadata_text(metadata):
            SkillParser._scan_injection(value)
    except ValueError as exc:
        _fail(f"generated metadata failed the injection scan ({exc})")
        return None
    return metadata


async def ensure_skill_metadata(
    skill: Skill,
    *,
    agent_seam: MetadataSeam,
    registry: SkillRegistry | None = None,
) -> bool:
    """Generate and persist metadata for *skill* exactly once.

    Idempotent and cheap: returns ``False`` and does nothing when the skill
    already carries metadata (``skill_metadata`` with a schema version),
    however it got there — authored front matter, sidecar enrichment, or a
    previous generation. Only a missing metadata document triggers one model
    call. The result is persisted through the registry's front-matter merge,
    which preserves the instruction body byte-for-byte.

    Fail-open: a failed generation or persist leaves the skill unchanged and
    returns ``False`` after reporting the failure.
    """
    existing = getattr(skill, "skill_metadata", None)
    if existing is not None and getattr(existing, "schema_version", None):
        return False

    diagnostics: list[MetadataDiagnostic] = []
    metadata = await generate_skill_metadata(
        getattr(skill, "content", "") or "",
        skill_name=skill.name,
        agent_seam=agent_seam,
        file_path=getattr(skill, "file_path", "") or "",
        diagnostics=diagnostics,
    )
    if metadata is None:
        reason = diagnostics[0].message if diagnostics else "no metadata produced"
        record_failure("skills.enrich.generate", ValueError(f"{skill.name}: {reason}"))
        return False

    target = registry if registry is not None else skill_registry
    try:
        target.persist_skill_metadata(skill, metadata)
    except Exception as exc:
        # Persistence can raise ValueError/YAML/encoding errors as well as
        # OSError. The documented contract is fail-open: report and leave the
        # skill unchanged (combined #49 / original #62).
        record_failure("skills.enrich.persist", exc)
        return False
    return True


def ensure_skill_metadata_sync(
    skill: Skill,
    *,
    agent_seam: MetadataSeam,
    registry: SkillRegistry | None = None,
) -> bool:
    """Blocking bridge for sync install surfaces (CLI, repo worker thread).

    Runs :func:`ensure_skill_metadata` on a private loop. When a loop is
    already running this returns ``False`` and reports it: blocking the caller
    would deadlock, and skipping keeps the install fail-open.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(ensure_skill_metadata(skill, agent_seam=agent_seam, registry=registry))
    record_failure(
        "skills.enrich.sync_bridge",
        RuntimeError("cannot generate skill metadata from inside a running event loop"),
    )
    return False


def default_agent_seam(
    *, provider: str | None = None, model: str | None = None
) -> MetadataSeam:
    """Production seam: one small provider call through the usage store.

    The provider is resolved lazily from configuration (or the explicit
    override) and closed after the single call. Nothing is sent to a provider
    until the returned callable is awaited.
    """

    async def _call(prompt: str) -> str:
        from machina.core.config import config as _config
        from machina.core.provider import get_provider
        from machina.core.usage import usage_store

        selected = provider or _config.get("provider")
        selected_model = model or _config.get("model")
        llm = get_provider(provider=selected, model=selected_model)
        try:
            response = await usage_store.complete_call(
                llm,
                None,
                "",
                [{"role": "user", "content": prompt}],
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=0,
                purpose="skill_metadata",
            )
        finally:
            closer = getattr(llm, "close", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:  # closing must never mask the result
                    pass
        return str(getattr(response, "content", "") or "")

    return _call
