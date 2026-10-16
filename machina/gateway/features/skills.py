"""Skills-related feature handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from machina import services
from machina.gateway.errors import INVALID_PARAMS, NOT_FOUND, OPERATION_FAILED, RpcError
from machina.gateway.features._common import _bool, _int, _skill, _str, _uuid

if TYPE_CHECKING:
    from machina.gateway.server import Gateway

# ─── deterministic catalog surface limits (spec §12.1) ───────────────────────

#: ``skills.search`` page bounds: default 10, hard cap 50 rows per response.
SKILLS_SEARCH_DEFAULT_LIMIT = 10
SKILLS_SEARCH_MAX_LIMIT = 50
SKILLS_SEARCH_MAX_QUERY = 2000
SKILLS_SEARCH_MAX_CURSOR = 128
#: Closed filter vocabulary; unknown keys/types fail closed.
SKILLS_SEARCH_FILTER_KEYS = (
    "task_family",
    "capability",
    "source",
    "enabled",
    "include_excluded",
)
SKILLS_SEARCH_MAX_SOURCES = 8
#: Known registry provenance values; unknown sources are invalid params, never
#: silently empty results.
SKILL_SOURCE_TYPES = ("builtin-ecc", "local", "hub", "url", "learned")
#: Display-only trust label per provenance (never an authorization input).
_SOURCE_TRUST = {"builtin-ecc": "bundled", "local": "local"}
_SOURCE_TRUST_DEFAULT = "external"
#: Fallback canonical family vocabulary; the search module owns the live sets.
CANONICAL_TASK_FAMILIES = (
    "debugging",
    "testing",
    "implementing",
    "reviewing",
    "researching",
    "deploying",
    "documenting",
    "optimizing",
    "securing",
    "planning",
)

#: Deterministic preview budget, matching the automatic disclosure path.
SKILLS_PREVIEW_BUDGET = 1200
#: Bounded task-state context keys for previews (spec §12.2). Stage facts are
#: task-state fields on the automatic path too (``normalize_stage_state``).
SKILLS_PREVIEW_CONTEXT_KEYS = (
    "active_class",
    "active_families",
    "stack",
    "constraints",
    "capabilities",
    "revision",
    "stage",
    "stage_source",
    "stage_revision",
)
SKILLS_PREVIEW_MAX_LIST = 16
SKILLS_PREVIEW_MAX_CONSTRAINTS = 8
SKILLS_PREVIEW_MAX_CONSTRAINT_CHARS = 120

#: ``skills.section`` budget bounds, matching the native ``skill_read`` limits.
SKILLS_SECTION_DEFAULT_BUDGET = 800
SKILLS_SECTION_MAX_BUDGET = 8000
SKILLS_SECTION_MAX_OFFSET = 10_000_000

#: Administrative browse/read activity. Catalog inspection telemetry only —
#: deliberately NOT an operational success or "task completed" counter.
_BROWSE_ACTIVITY: dict[str, int] = {"search": 0, "preview_route": 0, "section": 0}


def _record_browse_activity(kind: str, **fields: Any) -> None:
    """Record catalog browse/read activity; never an activation or success.

    A4's ``machina.skills.events`` is a closed ACTIVATION vocabulary
    (candidate_found/selected_for_plan/instruction_section_disclosed/...) with
    an explicit rule that administrative gateway browse/preview reads must not
    fabricate activation observations there. So this adapter stays optional and
    ``getattr``-guarded: if A4 ever exposes a typed administrative recorder
    (``record_event``/``record_skill_event``) it is used; otherwise the bounded
    local counter below records browse activity — it is deliberately NOT a
    "task completed" or success counter.
    """
    try:
        from machina.skills import events as skill_events
    except Exception:  # noqa: BLE001 - optional module must never fail the read
        skill_events = None
    recorder = getattr(skill_events, "record_event", None) if skill_events else None
    if not callable(recorder):
        recorder = getattr(skill_events, "record_skill_event", None) if skill_events else None
    if callable(recorder):
        try:
            recorder(kind, **fields)
            return
        except TypeError:
            try:
                recorder(kind=kind, **fields)
                return
            except Exception:  # noqa: BLE001 - recorder contracts are provisional
                pass
        except Exception:  # noqa: BLE001 - recorder contracts are provisional
            pass
    _BROWSE_ACTIVITY[kind] = _BROWSE_ACTIVITY.get(kind, 0) + 1


def _registry():
    from machina.skills.registry import skill_registry

    skill_registry.load_all()
    return skill_registry


async def skills_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """List all skills."""
    gw.require_db()
    return {"skills": [_skill(s) for s in sorted(_registry().list_skills(), key=lambda s: s.name)]}


async def skills_show(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Show a skill by name."""
    gw.require_db()
    skill = _registry().get(_str(params, "name", max_len=200))
    if skill is None:
        raise RpcError(NOT_FOUND, f"skill {params.get('name')!r} not found")
    result = _skill(skill, content=True)
    result["content"] = _registry().get_skill_content(skill.name)
    return {"skill": result}


async def skills_learn(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Learn a new skill from a file, URL, or existing skill."""
    import asyncio

    gw.require_db()
    triggers = params.get("triggers")
    if triggers is not None and (
        not isinstance(triggers, list) or not all(isinstance(t, str) for t in triggers)
    ):
        raise RpcError(INVALID_PARAMS, "triggers must be a list of strings")
    try:
        # 5.8: the sync fetch/parse path runs off-loop so URL retrieval never
        # blocks the gateway event loop.
        skill = await asyncio.to_thread(
            services.learn_skill,
            _str(params, "source", max_len=2000),
            name=_str(params, "name", required=False, max_len=100) or None,
            description=_str(params, "description", required=False, max_len=500) or None,
            triggers=triggers,
        )
    except services.ServiceError as e:
        raise RpcError(OPERATION_FAILED, str(e)) from None
    return {"skill": _skill(skill)}


async def skills_delete(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    name = _str(params, "name", max_len=200)
    skill = _registry().get(name)
    if skill and skill.source_type == "builtin-ecc":
        raise RpcError(INVALID_PARAMS, "ECC skills are bundled defaults and cannot be deleted")
    if not _registry().delete_skill(name):
        raise RpcError(NOT_FOUND, f"skill {name!r} not found")
    return {"deleted": True}


async def skills_curator(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    gw.require_db()
    from machina.skills.registry import SkillCurator

    curator = SkillCurator(_registry())
    report = curator.get_health_report()
    report["unused"] = [s.name for s in curator.get_unused_skills()]
    report["stale"] = [s.name for s in curator.get_stale_skills(_int(params, "days", 30, 1, 3650))]
    return report


async def learning_list(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """List learning proposals."""
    gw.require_db()
    from machina.skills.learning import learning_reviewer

    session = await gw.get_session(params)
    return {
        "reviews": await learning_reviewer.list_reviews(
            session.agent_id, limit=_int(params, "limit", 50, 1, 100)
        )
    }


async def learning_approve(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Approve a learning proposal."""
    gw.require_db()
    from machina.skills.learning import learning_reviewer

    session = await gw.get_session(params)
    try:
        review = await learning_reviewer.approve(_uuid(params, "id"), agent_id=session.agent_id)
    except (LookupError, PermissionError):
        raise RpcError(NOT_FOUND, "learning proposal not found for this agent") from None
    except (ValueError, FileExistsError) as exc:
        raise RpcError(OPERATION_FAILED, str(exc)) from None
    return {"review": review}


async def learning_reject(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Reject a learning proposal."""
    gw.require_db()
    from machina.skills.learning import learning_reviewer

    session = await gw.get_session(params)
    try:
        review = await learning_reviewer.reject(_uuid(params, "id"), agent_id=session.agent_id)
    except (LookupError, PermissionError):
        raise RpcError(NOT_FOUND, "learning proposal not found for this agent") from None
    return {"review": review}


async def learning_revert(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Revert an approved (typically auto-applied) learning proposal."""
    gw.require_db()
    from machina.skills.learning import learning_reviewer

    session = await gw.get_session(params)
    try:
        review = await learning_reviewer.revert(_uuid(params, "id"), agent_id=session.agent_id)
    except (LookupError, PermissionError):
        raise RpcError(NOT_FOUND, "learning proposal not found for this agent") from None
    except (ValueError, FileExistsError, OSError) as exc:
        raise RpcError(OPERATION_FAILED, str(exc)) from None
    return {"review": review}


# ─── §12.1 bounded catalog search ────────────────────────────────────────────


def _canonical_family(value: str) -> str:
    """Canonicalize a declared/typed family through the search module's aliases."""
    from machina.skills import search as skill_search

    aliases = getattr(skill_search, "_DECLARED_FAMILY_ALIASES", None)
    folded = value.strip().casefold()
    if isinstance(aliases, dict) and isinstance(aliases.get(folded), str):
        return aliases[folded]
    return folded


def _known_task_families() -> set[str]:
    from machina.skills import search as skill_search

    known = set(CANONICAL_TASK_FAMILIES)
    members = getattr(skill_search, "_FAMILY_MEMBERS", None)
    if isinstance(members, dict):
        known |= set(members)
    aliases = getattr(skill_search, "_DECLARED_FAMILY_ALIASES", None)
    if isinstance(aliases, dict):
        known |= set(aliases)
        known |= {value for value in aliases.values() if isinstance(value, str)}
    return known


def _text_values(value: Any) -> tuple[str, ...]:
    """Defensive list-like read: strings only, everything else ignored."""
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    return ()


def _skill_families(skill: Any) -> set[str]:
    """Declared + inferred families for one skill (the search module's rules)."""
    from machina.skills.search import normalize_tokens

    families: set[str] = set()
    typed = getattr(skill, "skill_metadata", None)
    families |= {
        _canonical_family(value) for value in _text_values(getattr(typed, "task_families", None))
    }
    legacy = getattr(skill, "metadata", None)
    if isinstance(legacy, dict):
        for key in ("task_families", "task_family", "families"):
            values = _text_values(legacy.get(key))
            if values:
                families |= {_canonical_family(value) for value in values}
                break
    from machina.skills import search as skill_search

    members = getattr(skill_search, "_FAMILY_MEMBERS", None)
    if isinstance(members, dict):
        text = " ".join(
            (
                str(getattr(skill, "name", "")),
                str(getattr(skill, "description", "")),
                " ".join(getattr(skill, "triggers", None) or []),
            )
        )
        tokens = set(normalize_tokens(text))
        families |= {family for family, words in members.items() if words & tokens}
    return families


def _skill_capabilities(skill: Any) -> set[str]:
    """Declared capabilities for one skill (typed metadata wins, legacy unioned)."""
    values: list[str] = []
    typed = getattr(skill, "skill_metadata", None)
    values.extend(_text_values(getattr(typed, "capabilities", None)))
    legacy = getattr(skill, "metadata", None)
    if isinstance(legacy, dict):
        for key in ("capabilities", "capability"):
            found = _text_values(legacy.get(key))
            if found:
                values.extend(found)
                break
    return {value.strip().casefold() for value in values}


def _search_filters(raw: Any) -> dict[str, Any]:
    """Validate and normalize the closed ``filters`` object for skills.search."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RpcError(INVALID_PARAMS, "filters must be an object")
    unknown = sorted(set(raw) - set(SKILLS_SEARCH_FILTER_KEYS))
    if unknown:
        raise RpcError(INVALID_PARAMS, "unknown filter(s): " + ", ".join(unknown))
    family = ""
    raw_family = _str(raw, "task_family", required=False, max_len=40)
    if raw_family:
        canonical = _canonical_family(raw_family)
        if canonical not in _known_task_families():
            expected = ", ".join(sorted(_known_task_families()))
            raise RpcError(
                INVALID_PARAMS, f"task_family {raw_family!r} is unknown; expected one of {expected}"
            )
        family = canonical
    capability = _str(raw, "capability", required=False, max_len=80).casefold()
    sources: tuple[str, ...] = ()
    raw_source = raw.get("source")
    if raw_source is not None:
        values = [raw_source] if isinstance(raw_source, str) else raw_source
        if not isinstance(values, (list, tuple)) or len(values) > SKILLS_SEARCH_MAX_SOURCES:
            raise RpcError(
                INVALID_PARAMS,
                f"source must be a string or a list of at most {SKILLS_SEARCH_MAX_SOURCES} strings",
            )
        normalized: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise RpcError(INVALID_PARAMS, "source entries must be non-empty strings")
            folded = value.strip().casefold()
            if folded not in SKILL_SOURCE_TYPES:
                expected = ", ".join(SKILL_SOURCE_TYPES)
                raise RpcError(
                    INVALID_PARAMS, f"source {value!r} is unknown; expected one of {expected}"
                )
            if folded not in normalized:
                normalized.append(folded)
        sources = tuple(normalized)
    enabled: bool | None = None
    if raw.get("enabled") is not None:
        enabled = _bool(raw, "enabled")
    include_excluded = _bool(raw, "include_excluded")
    if enabled is False:
        # Explicitly asking for disabled skills is the browse path.
        include_excluded = True
    return {
        "task_family": family,
        "capability": capability,
        "source": list(sources),
        "enabled": enabled,
        "include_excluded": include_excluded,
    }


def _filtered_skills(skills: list[Any], filters: dict[str, Any]) -> list[Any]:
    selected = list(skills)
    enabled = filters.get("enabled")
    if enabled is True:
        selected = [skill for skill in selected if getattr(skill, "enabled", True)]
    elif enabled is False:
        selected = [skill for skill in selected if not getattr(skill, "enabled", True)]
    if filters.get("task_family"):
        family = filters["task_family"]
        selected = [skill for skill in selected if family in _skill_families(skill)]
    if filters.get("capability"):
        capability = filters["capability"]
        selected = [skill for skill in selected if capability in _skill_capabilities(skill)]
    sources = filters.get("source") or ()
    if sources:
        selected = [
            skill
            for skill in selected
            if str(getattr(skill, "source_type", "local")).casefold() in sources
        ]
    return selected


def _search_candidate(candidate: Any, skill: Any | None) -> dict[str, Any]:
    source_type = str(getattr(skill, "source_type", "local")) if skill is not None else "local"
    return {
        "skillId": candidate.skill_id,
        "name": candidate.name,
        "contentRevision": candidate.content_revision,
        "score": candidate.score,
        "scoreComponents": dict(candidate.score_components),
        "matchedPhrases": list(candidate.matched_phrases),
        "matchedCapabilities": list(candidate.matched_capabilities),
        "applicability": candidate.applicability,
        "reasons": list(candidate.reasons),
        "workflowStages": list(candidate.workflow_stages),
        "explanation": candidate.explanation,
        "sourceType": source_type,
        "trust": _SOURCE_TRUST.get(source_type, _SOURCE_TRUST_DEFAULT),
        "enabled": bool(getattr(skill, "enabled", True)) if skill is not None else True,
    }


async def skills_search(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Bounded deterministic skill search over the loaded catalog (spec §12.1).

    Disabled skills are never candidates on the automatic path; they appear
    (flagged ``excluded``) only when the caller explicitly browses with
    ``filters.include_excluded`` or ``filters.enabled: false``. Cursors are
    opaque offsets into the effective filtered catalog.
    """
    gw.require_db()
    query = _str(params, "query", required=False, max_len=SKILLS_SEARCH_MAX_QUERY)
    limit = _int(params, "limit", SKILLS_SEARCH_DEFAULT_LIMIT, 1, SKILLS_SEARCH_MAX_LIMIT)
    cursor = _str(params, "cursor", required=False, max_len=SKILLS_SEARCH_MAX_CURSOR) or None
    filters = _search_filters(params.get("filters"))
    registry = _registry()
    selected = _filtered_skills(registry.list_skills(), filters)
    browse = bool(filters["include_excluded"] or filters["enabled"] is False)

    from machina.skills.runtime import SkillIndex
    from machina.skills.search import execute_search

    index = SkillIndex(selected, include_disabled=browse)
    try:
        result = execute_search(index, query, limit=limit, cursor=cursor)
    except ValueError:
        raise RpcError(INVALID_PARAMS, "cursor is invalid") from None
    by_id = {str(getattr(skill, "name", "")).casefold(): skill for skill in index.skills}
    _record_browse_activity(
        "search",
        query_chars=len(query),
        limit=limit,
        browse=browse,
        hits=len(result.candidates),
    )
    return {
        "query": query,
        "filters": filters,
        "limit": limit,
        "browse": browse,
        "candidates": [
            _search_candidate(candidate, by_id.get(candidate.skill_id))
            for candidate in result.candidates
        ],
        "catalogRevision": result.catalog_revision,
        "queryDescriptorRevision": result.query_descriptor_revision,
        "policyRevision": result.policy_revision,
        "nextCursor": result.next_cursor,
        "diagnostics": result.diagnostics,
    }


# ─── §12.2 call-free preview + version-bound section reads ───────────────────


def _bounded_strings(raw: Any, key: str, *, limit: int, max_len: int) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise RpcError(INVALID_PARAMS, f"context.{key} must be a string or a list of strings")
    if len(raw) > limit:
        raise RpcError(INVALID_PARAMS, f"context.{key} accepts at most {limit} entries")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise RpcError(INVALID_PARAMS, f"context.{key} entries must be strings")
        text = item.strip()
        if not text:
            continue
        if len(text) > max_len:
            raise RpcError(
                INVALID_PARAMS, f"context.{key} entries must be at most {max_len} characters"
            )
        if text not in values:
            values.append(text)
    return values


def _preview_stack(raw: Any) -> dict[str, list[str]] | list[str]:
    """Bound a caller task-state stack (``export_state`` shape or token list)."""
    from machina.core.task_descriptor import STACK_CATEGORIES

    if isinstance(raw, dict):
        unknown = sorted(set(raw) - set(STACK_CATEGORIES))
        if unknown:
            raise RpcError(INVALID_PARAMS, "unknown stack categor(ies): " + ", ".join(unknown))
        stack: dict[str, list[str]] = {}
        for category in STACK_CATEGORIES:
            if raw.get(category) is None:
                continue
            values = _bounded_strings(raw[category], f"stack.{category}", limit=24, max_len=64)
            if values:
                stack[category] = values
        return stack
    return _bounded_strings(raw, "stack", limit=24, max_len=64)


def _preview_context(params: dict[str, Any]) -> dict[str, Any]:
    """Bounded task-state context for previews (spec §12.2: task state only)."""
    raw = params.get("context")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RpcError(INVALID_PARAMS, "context must be an object")
    unknown = sorted(set(raw) - set(SKILLS_PREVIEW_CONTEXT_KEYS))
    if unknown:
        raise RpcError(INVALID_PARAMS, "unknown context field(s): " + ", ".join(unknown))
    context: dict[str, Any] = {}
    for key in ("active_class", "stage", "stage_source"):
        text = _str(raw, key, required=False, max_len=64)
        if text:
            context[key] = text
    for key in ("active_families", "capabilities"):
        if raw.get(key) is not None:
            context[key] = _bounded_strings(
                raw[key], key, limit=SKILLS_PREVIEW_MAX_LIST, max_len=64
            )
    if raw.get("constraints") is not None:
        context["constraints"] = _bounded_strings(
            raw["constraints"],
            "constraints",
            limit=SKILLS_PREVIEW_MAX_CONSTRAINTS,
            max_len=SKILLS_PREVIEW_MAX_CONSTRAINT_CHARS,
        )
    for key in ("revision", "stage_revision"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RpcError(INVALID_PARAMS, f"context.{key} must be a non-negative integer")
        context[key] = value
    if raw.get("stack") is not None:
        context["stack"] = _preview_stack(raw["stack"])
    return context


async def skills_preview_route(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Call-free route preview: descriptor + selection + disclosure (spec §12.2).

    Deterministic path only. Creates no turn, calls no provider or embedding,
    executes no tool, installs nothing, mutates no approval state, and records
    no operational success (browse activity only).
    """
    gw.require_db()
    request = _str(params, "request", max_len=SKILLS_SEARCH_MAX_QUERY)
    context = _preview_context(params)

    from machina.core.task_descriptor import preview_task_descriptor
    from machina.skills.planned_steps import (
        normalize_constraints,
        normalize_stage_state,
        plan_disclosure,
    )
    from machina.skills.workflow import plan_workflow

    payload = preview_task_descriptor(request, task_state=context or None)
    task_revision = int(payload["descriptor"]["task_state_revision"])
    registry = _registry()
    workflow = plan_workflow(
        request, skills=registry, task_state=context or None, task_revision=task_revision
    )
    state = normalize_stage_state(context) if context else None
    constraints = normalize_constraints(context.get("constraints")) if context else ()
    plan = plan_disclosure(
        workflow.steps,
        skill_for=registry.get,
        content_for=registry.get_skill_content,
        budget=SKILLS_PREVIEW_BUDGET,
        stage_state=state,
        task_revision=task_revision,
        catalog_revision=workflow.catalog_revision,
        constraints=constraints,
    )
    _record_browse_activity(
        "preview_route",
        request_chars=len(request),
        routing_kind=workflow.kind,
        steps=len(workflow.steps),
    )
    return {
        "descriptor": payload["descriptor"],
        "reasons": payload["reasons"],
        "selection": {
            "kind": workflow.kind,
            "skills": [skill.name for skill in workflow.skills],
            "steps": [step.to_dict() for step in workflow.steps],
            "catalogRevision": workflow.catalog_revision,
            "taskRevision": workflow.task_revision,
        },
        "disclosure": plan.to_dict(),
        "budget": SKILLS_PREVIEW_BUDGET,
        "tokenBasis": plan.token_basis,
    }


def _content_offset_read(
    skill: Any, content: str, *, expected_revision: str | None, offset: int, limit: int
) -> Any:
    """Version-bound raw character-offset read (no section anchor)."""
    from machina.skills.metadata import MetadataDiagnostic
    from machina.skills.sections import (
        CODE_SECTION_RANGE,
        CODE_SECTION_STALE,
        ReadSectionResult,
        skill_content_revision,
    )

    skill_id = str(getattr(skill, "name", ""))
    # The canonical skill content revision (what ``skills.search`` candidates
    # expose and ``skills.section`` binds), not the raw-byte fingerprint: a
    # revision shown by search joins this offset read too.
    revision = skill_content_revision(skill)
    total = len(content)
    if expected_revision is not None and expected_revision != revision:
        return ReadSectionResult(
            skill_id=skill_id,
            content_revision=revision,
            section_id="",
            total=total,
            section_total=total,
            stale=True,
            diagnostics=(
                MetadataDiagnostic(
                    code=CODE_SECTION_STALE,
                    message=(
                        f"content revision {expected_revision} no longer matches the current "
                        f"content ({revision})"
                    ),
                    skill_id=skill_id,
                ),
            ),
        )
    if offset > total:
        return ReadSectionResult(
            skill_id=skill_id,
            content_revision=revision,
            section_id="",
            total=total,
            section_total=total,
            diagnostics=(
                MetadataDiagnostic(
                    code=CODE_SECTION_RANGE,
                    message=f"offset {offset} is outside the skill content (0..{total})",
                    skill_id=skill_id,
                ),
            ),
        )
    end = min(total, offset + limit)
    return ReadSectionResult(
        skill_id=skill_id,
        content_revision=revision,
        section_id="",
        text=content[offset:end],
        total=total,
        section_total=total,
        continuation=end if end < total else None,
    )


def _section_payload(result: Any, budget: int) -> dict[str, Any]:
    data = result.to_dict()
    return {
        "skillId": data["skill_id"],
        "contentRevision": data["content_revision"],
        "sectionId": data["section_id"],
        "heading": data["heading"],
        "text": data["text"],
        "start": data["start"],
        "end": data["end"],
        "total": data["total"],
        "sectionTotal": data["section_total"],
        "line": data["line"],
        "continuation": data["continuation"],
        "stale": data["stale"],
        "bound": data["bound"],
        "anchorId": data["anchor_id"],
        "budget": budget,
        "diagnostics": data["diagnostics"],
    }


async def skills_section(gw: Gateway, params: dict[str, Any]) -> dict[str, Any]:
    """Read one exact versioned skill passage (spec §12.2).

    ``section`` addresses a declared/parsed anchor; ``offset`` (optionally
    within a section) continues a previous read. ``contentRevision`` is the
    canonical skill content revision (``search.content_revision``) — the same
    identity ``skills.search`` candidates carry — so a revision shown by a
    search is fresh on the first read. A stale ``contentRevision`` returns the
    structured stale response with empty text — never a different passage at
    the same address.
    """
    gw.require_db()
    skill_id = _str(params, "skillId", max_len=200)
    registry = _registry()
    skill = registry.get(skill_id)
    if skill is None:
        raise RpcError(NOT_FOUND, f"skill {skill_id!r} not found")
    content = registry.get_skill_content(skill.name)
    if content is None:
        # Disabled/archived skills have no served content; use the stored body
        # so an explicit browse read still sees exact bytes.
        content = getattr(skill, "content", "") or ""
    expected_revision = _str(params, "contentRevision", required=False, max_len=64) or None
    section = _str(params, "section", required=False, max_len=200)
    offset_supplied = params.get("offset") is not None
    offset = _int(params, "offset", 0, 0, SKILLS_SECTION_MAX_OFFSET)
    budget = _int(params, "budget", SKILLS_SECTION_DEFAULT_BUDGET, 1, SKILLS_SECTION_MAX_BUDGET)
    if not section and not offset_supplied:
        raise RpcError(INVALID_PARAMS, "section or offset is required")
    if section:
        from machina.skills.sections import read_skill_section

        result = read_skill_section(
            skill,
            section,
            content=content,
            expected_revision=expected_revision,
            offset=offset,
            limit=budget,
        )
    else:
        result = _content_offset_read(
            skill, content, expected_revision=expected_revision, offset=offset, limit=budget
        )
    _record_browse_activity("section", skill=skill.name, section=section, stale=result.stale)
    return _section_payload(result, budget)
