"""Deterministic skill search: typed contract, applicability, explanations (spec §9).

This module is a stdlib-only leaf. ``machina.skills.runtime`` imports it at
module level (never the reverse), so the search path cannot create an import
cycle and can never pull a provider or embedding stack — ``execute_search``
takes plain skill objects, an index, and a query string, nothing else.

It owns:

* alias normalization shared by ranking and explanations
  (:func:`normalize_tokens`). The rules match the legacy ``runtime._words``
  baseline (casefold, C++/C#/F#/.NET folding, synonym expansion, stopwords),
  so exact ID/name matches and boundary-aware triggers keep their behavior;
* the typed result contract (:class:`SearchResult` / :class:`SearchCandidate`).
  Scores are RANKING SIGNALS derived from named components — never calibrated
  probabilities. ``candidate.score == sum(score_components.values())``;
* applicability evaluation (applicable / conditional / excluded), kept
  separate from candidate retrieval and stage composition;
* the explanation builder, derived from the same components that produced the
  ordering, so an explanation cannot disagree with the decision it explains.

Pipeline: retrieve (lexical index + declared signals) -> evaluate
applicability -> compose stages -> score with named components -> order by
(applicability bucket, score desc, source precedence, case-insensitive name,
skill_id) -> page with an opaque cursor.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "APPLICABLE",
    "CONDITIONAL",
    "EXCLUDED",
    "POLICY_REVISION",
    "QUERY_DESCRIPTOR_REVISION",
    "SearchCandidate",
    "SearchResult",
    "build_query_descriptor",
    "execute_search",
    "normalize_tokens",
]

#: Revision of the ranking/applicability policy encoded in this module.
POLICY_REVISION = "skills-search-v1"
#: Revision of the query-descriptor schema and normalization rules.
QUERY_DESCRIPTOR_REVISION = "skills-query-descriptor-v1"

APPLICABLE = "applicable"
CONDITIONAL = "conditional"
EXCLUDED = "excluded"
_APPLICABILITY_ORDER = {APPLICABLE: 0, CONDITIONAL: 1, EXCLUDED: 2}

# Source trust ordering used only as a deterministic tie-break (curated before
# agent-generated; the verified bundle first).
_SOURCE_PRECEDENCE = {"builtin-ecc": 0, "local": 1, "hub": 2, "url": 3, "learned": 4}
_DEFAULT_SOURCE_PRECEDENCE = 3

# ─── canonical normalization (single home for ranking + explanations) ────────

STOPWORDS = frozenset(
    "a an the to of for and or in on with using use me my please can could would should help i we "
    "you it this that is are be do does how want need write build create make implement review "
    "code task when".split()
)
#: Meaning-sensitive punctuation is folded to stable tokens (C++, C#, F#, .NET)
#: instead of being stripped; commands, paths, permissions, and IDs are never
#: fuzzy-corrected anywhere on this path.
ALIAS_REPLACEMENTS = (
    ("c++", "cpp"),
    ("c#", "csharp"),
    ("f#", "fsharp"),
    (".net", "dotnet"),
)
#: One-way synonym expansion; mirrors the legacy baseline so consumers see the
#: same vocabulary ("pytest" reaches python-testing skills, etc.).
SYNONYM_EXPANSIONS = {
    "postgresql": "postgres",
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "k8s": "kubernetes",
    "golang": "go",
    "a11y": "accessibility",
    "auth": "authentication",
    "tests": "testing",
    "test": "testing",
    "pytest": "python testing",
    "unittest": "python testing",
    "reactjs": "react",
    "vuejs": "vue",
    "django": "django python",
    "nextjs": "nextjs react",
    "springboot": "springboot java",
    "optimise": "optimize",
    "animate": "motion animation",
    "animation": "motion animation",
    "animations": "motion animation",
}

#: Technology markers used to infer a skill's stack from its name (legacy
#: parity with ``runtime._TECHNOLOGIES``). A6 follow-up: widely-named frontend
#: frameworks are included so "switch the ... to Svelte" can name an explicit
#: stack; ordinary-English-word tokens stay guarded in
#: ``machina.core.intent.AMBIGUOUS_TECHNOLOGIES``.
TECHNOLOGY_MARKERS = frozenset(
    "python rust go react vue angular svelte solid solidjs django laravel kotlin swift swiftui "
    "java springboot quarkus perl cpp csharp fsharp dotnet rails redis postgres mysql clickhouse "
    "kubernetes docker fastapi nestjs nuxt nuxt4 vite bun flutter dart android nextjs pytorch "
    "prisma netmiko cisco evm".split()
)
#: Specialty markers behave like stacks: an unmentioned specialty keeps the
#: skill visible but conditional (legacy parity with ``runtime._SPECIALTIES``).
SPECIALTY_MARKERS = frozenset(
    "celery native exposed ktor turbopack keccak256 installer motion".split()
)

_FAMILY_MEMBERS: dict[str, frozenset[str]] = {
    "debugging": frozenset(
        "bug bugs debug debugging crash crashes crashed crashing diagnose diagnoses diagnosed "
        "diagnosing triage failure failures fail fails failed failing broken error errors "
        "regression regressions incident incidents outage outages troubleshoot troubleshooting".split()
    ),
    "testing": frozenset(
        "test tests testing pytest unittest coverage assert asserts verify verifies verified "
        "verification".split()
    ),
    "implementing": frozenset(
        "implement implements implemented implementing implementation build builds built building "
        "create creates created creating add adds added adding write writes wrote written writing "
        "develop develops developed developing development coding feature features refactor "
        "refactors refactored refactoring migrate migrates migrated migrating migration migrations "
        "fix fixes fixed fixing".split()
    ),
    "reviewing": frozenset(
        "review reviews reviewed reviewing audit audits audited auditing inspect inspects inspection "
        "critique critiques".split()
    ),
    "researching": frozenset(
        "research researches researched researching investigate investigates investigated "
        "investigating investigation compare compares compared comparing comparison comparisons "
        "survey surveys literature sources".split()
    ),
    "deploying": frozenset(
        "deploy deploys deployed deploying deployment deployments release releases released "
        "releasing ship ships shipped shipping rollout rollouts pipeline pipelines".split()
    ),
    "documenting": frozenset(
        "document documents documented documenting documentation readme readmes guide guides "
        "changelog explain explains explained explaining".split()
    ),
    "optimizing": frozenset(
        "optimize optimizes optimized optimizing optimization performance perf speed faster latency "
        "throughput profile profiling bottleneck bottlenecks".split()
    ),
    "securing": frozenset(
        "security secure secures securing authentication authorization permission permissions "
        "vulnerability vulnerabilities cve secrets secret credentials credential".split()
    ),
    "planning": frozenset(
        "plan plans planned planning design designs designed designing architecture architectural adr "
        "decision decisions".split()
    ),
}
#: Canonical family order and the workflow stage each family can compose.
#: Stage labels align with the platform's closed section-stage vocabulary from
#: ``machina.skills.metadata`` (plan/inspect/implement/verify/debug/review/
#: deliver/maintain) when that module is present.
_FAMILY_STAGES: dict[str, str] = {
    "debugging": "debug",
    "testing": "verify",
    "implementing": "implement",
    "reviewing": "review",
    "researching": "inspect",
    "deploying": "deliver",
    "documenting": "deliver",
    "optimizing": "maintain",
    "securing": "review",
    "planning": "plan",
}

#: Schema-v2 declared task families (``machina.skills.metadata`` closed
#: vocabulary) projected onto this module's canonical families. Unknown
#: declared values are ignored; the metadata module already validated them
#: against its own closed set.
_DECLARED_FAMILY_ALIASES = {
    "backend-api": "implementing",
    "data-analysis": "researching",
    "debugging": "debugging",
    "deployment": "deploying",
    "documentation": "documenting",
    "frontend-review": "reviewing",
    "maintenance": "implementing",
    "refactoring": "implementing",
    "research": "researching",
    "security-review": "securing",
    "testing": "testing",
    "web-build": "implementing",
}

# ─── score components (bounded ranking signals) ──────────────────────────────

_EXACT_NAME_BOOST = 60.0
_TRIGGER_BOOST = 25.0
_BM25_K = 2.2
_PHRASE_BOOST = 2.0
_NAME_OVERLAP_BOOST = 0.8
_FAMILY_BOOST = 6.0
_CAPABILITY_BOOST = 4.0
_STACK_BOOST = 3.0
#: Signals contribute at most this many matches each; influence stays bounded.
_MAX_SIGNAL_MATCHES = 3
#: Preference weights are clamped to this band, so learned/caller preferences
#: nudge ordering but can never override eligibility.
_PREFERENCE_MIN = 0.8
_PREFERENCE_MAX = 1.2
#: A word carried by more than half the effective catalog is not distinguishing.
#: Catalog-dependent by construction; see the robustness notes in the tests.
_DISTINGUISHING_RATIO = 0.5

COMPONENT_ORDER = (
    "exact_name",
    "trigger",
    "lexical",
    "phrase",
    "name_overlap",
    "task_family",
    "capability",
    "stack",
    "preference",
)
_COMPONENT_LABELS = {
    "exact_name": "exact name",
    "trigger": "trigger",
    "lexical": "lexical overlap",
    "phrase": "phrase",
    "name_overlap": "name overlap",
    "task_family": "task family",
    "capability": "capability",
    "stack": "stack",
    "preference": "preference",
}

# ─── typo tolerance (OFF by default; discovery tokens only) ─────────────────

_MIN_TYPO_TOKEN_LENGTH = 5
_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
#: Tokens adjacent to these characters in the raw query are path/id-like and
#: are never corrected.
_PROTECTED_NEIGHBOURS = frozenset("-_./\\:@#$%^~")

_CURSOR_PREFIX = "skills-search-v1:"


@dataclass(frozen=True)
class QueryDescriptor:
    """Normalized, deterministic view of a query used by every decision."""

    raw: str
    token_list: tuple[str, ...]
    tokens: frozenset[str]
    families: tuple[str, ...]
    stacks: frozenset[str]
    corrections: tuple[tuple[str, str], ...] = ()
    revision: str = QUERY_DESCRIPTOR_REVISION


@dataclass(frozen=True)
class SearchCandidate:
    """One retrieved skill with its ranking signals and eligibility decision.

    ``score_components`` names every contribution to ``score`` (the score is
    exactly their sum) and ``explanation`` is built from those same values.
    ``reasons`` carries exclusion or missing-precondition reasons; it is empty
    for applicable candidates.
    """

    skill_id: str
    name: str
    content_revision: str
    score: float
    score_components: dict[str, float]
    matched_phrases: tuple[str, ...]
    matched_capabilities: tuple[str, ...]
    applicability: str
    reasons: tuple[str, ...]
    workflow_stages: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class SearchResult:
    """Typed, deterministic search result. Scores are ranking signals only."""

    catalog_revision: str
    query_descriptor_revision: str
    policy_revision: str
    candidates: list[SearchCandidate]
    next_cursor: str | None
    diagnostics: dict[str, Any]


@dataclass
class _Retrieved:
    skill: Any
    terms: Mapping[str, int]
    name_words: frozenset[str]
    overlap: frozenset[str]
    exact_spelling: str | None
    triggers: tuple[str, ...]
    phrase_matches: tuple[str, ...]
    declared_families: tuple[str, ...]
    matched_families: tuple[str, ...]
    matched_capabilities: tuple[str, ...]
    declared_stack: tuple[str, ...]
    matched_stack: tuple[str, ...]
    preconditions: tuple[str, ...]
    declared_stages: tuple[str, ...]
    matched_phrases: tuple[str, ...]


# ─── normalization ───────────────────────────────────────────────────────────


def normalize_tokens(text: str) -> list[str]:
    """Canonical tokens: aliases folded, synonyms expanded, stopwords dropped.

    Meaning-sensitive punctuation for C++, C#, F#, and .NET is preserved as
    stable tokens; everything else is tokenized on ``[a-z0-9]+``. No
    fuzzy correction happens here (see :func:`build_query_descriptor` for the
    opt-in, bounded tolerance).
    """
    folded = text.casefold()
    for source, replacement in ALIAS_REPLACEMENTS:
        folded = folded.replace(source, replacement)
    words = re.findall(r"[a-z0-9]+", folded)
    result: list[str] = []
    for word in words:
        result.extend(SYNONYM_EXPANSIONS.get(word, word).split())
    return [word for word in result if word not in STOPWORDS and len(word) > 1]


def build_query_descriptor(
    query: str,
    *,
    vocabulary: Iterable[str] | None = None,
    typo_tolerance: bool = False,
) -> QueryDescriptor:
    """Normalize *query* once; every ranking and explanation decision reuses it.

    Typo tolerance is OFF by default. When enabled it is bounded to plain
    alphabetic discovery tokens (>= 5 characters, clean word context in the raw
    query), corrects only when exactly one vocabulary word is within one edit,
    and records every correction in ``corrections``/``diagnostics``.
    """
    raw = query or ""
    token_list = normalize_tokens(raw)
    corrections: list[tuple[str, str]] = []
    if typo_tolerance and vocabulary is not None:
        known = set(vocabulary)
        corrected: list[str] = []
        for token in token_list:
            fix = _bounded_correction(raw, token, known)
            if fix is not None:
                corrections.append((token, fix))
                corrected.append(fix)
            else:
                corrected.append(token)
        token_list = corrected
    tokens = frozenset(token_list)
    families = tuple(family for family, members in _FAMILY_MEMBERS.items() if tokens & members)
    return QueryDescriptor(
        raw=raw,
        token_list=tuple(token_list),
        tokens=tokens,
        families=families,
        stacks=frozenset(tokens & TECHNOLOGY_MARKERS),
        corrections=tuple(corrections),
    )


def _bounded_correction(raw: str, token: str, vocabulary: set[str]) -> str | None:
    if len(token) < _MIN_TYPO_TOKEN_LENGTH or not token.isalpha():
        return None
    if token in vocabulary:
        return None
    if not _clean_word_context(raw, token):
        return None
    candidates = sorted(edit for edit in _edits_within_one(token) if edit in vocabulary)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _clean_word_context(raw: str, token: str) -> bool:
    """True when *token* appears in *raw* as a standalone word (not a path/id)."""
    pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])", re.I)
    for match in pattern.finditer(raw):
        before = raw[match.start() - 1] if match.start() else ""
        after = raw[match.end()] if match.end() < len(raw) else ""
        if before in _PROTECTED_NEIGHBOURS or after in _PROTECTED_NEIGHBOURS:
            continue
        return True
    return False


def _edits_within_one(token: str) -> set[str]:
    edits: set[str] = set()
    for position in range(len(token)):
        edits.add(token[:position] + token[position + 1 :])  # deletion
        for letter in _ALPHABET:
            edits.add(token[:position] + letter + token[position + 1 :])  # substitution
    for position in range(len(token) + 1):
        for letter in _ALPHABET:
            edits.add(token[:position] + letter + token[position:])  # insertion
    for position in range(len(token) - 1):
        edits.add(  # adjacent transposition
            token[:position] + token[position + 1] + token[position] + token[position + 2 :]
        )
    return edits


# ─── metadata reads (defensive; absence is the v1 baseline) ──────────────────


def _meta_value(meta: Any, key: str) -> Any:
    if meta is None:
        return None
    if isinstance(meta, Mapping):
        return meta.get(key)
    getter = getattr(meta, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except Exception:  # third-party metadata must never break search
            return None
    return getattr(meta, key, None)


def _meta_list(meta: Any, *keys: str) -> tuple[str, ...]:
    """Read a list-like metadata field defensively.

    Accepts a string as a one-item value, lists/tuples/sets of strings, and
    ignores anything else; a missing/None/unsupported value means "no
    requirement" (the v1 baseline, before any metadata module exists).
    """
    for key in keys:
        value = _meta_value(meta, key)
        if value is None:
            continue
        if isinstance(value, str):
            items = (value,)
        elif isinstance(value, (list, tuple, frozenset, set)):
            items = tuple(item for item in value if isinstance(item, str))
        else:
            continue
        if items:
            return items
    return ()


# ─── retrieval (lexical index + declared signals) ────────────────────────────


def _is_distinguishing(index: Any, word: str) -> bool:
    count = index.df.get(word, 0)
    return count / max(1, index.doc_count) <= _DISTINGUISHING_RATIO


def _trigger_match(trigger: str, query: str) -> bool:
    """Boundary-aware trigger match; non-word characters (c++, .net) work."""

    return (
        re.search(r"(?<![A-Za-z0-9_])" + re.escape(trigger) + r"(?![A-Za-z0-9_])", query, re.I)
        is not None
    )


def _contains_sequence(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    return any(
        haystack[start : start + len(needle)] == needle
        for start in range(len(haystack) - len(needle) + 1)
    )


def _exact_name_match(
    name: str, aliases: tuple[str, ...], descriptor: QueryDescriptor
) -> str | None:
    """The matched spelling when the query names the skill, else None.

    Matches the raw string (boundary-aware) or the canonical token run, for
    the name, its ``ecc-``-stripped form, and any declared alias.
    """

    raw = descriptor.raw.casefold()
    spellings = dict.fromkeys((*[name, name.removeprefix("ecc-")], *aliases))
    for spelling in spellings:
        if not spelling:
            continue
        folded = spelling.casefold()
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(folded) + r"(?![A-Za-z0-9_])", raw):
            return spelling
        tokens = tuple(normalize_tokens(folded))
        if tokens and _contains_sequence(descriptor.token_list, tokens):
            return spelling
    return None


def _phrase_in_query(tokens: tuple[str, ...], descriptor: QueryDescriptor) -> bool:
    if not tokens:
        return False
    if len(tokens) == 1:
        return tokens[0] in descriptor.tokens
    return _contains_sequence(descriptor.token_list, tokens)


def _declared_stack(
    meta: Any, name_words: frozenset[str], typed_stack: tuple[str, ...] = ()
) -> tuple[str, ...]:
    words = set(name_words & (TECHNOLOGY_MARKERS | SPECIALTY_MARKERS))
    for value in (
        *typed_stack,
        *_meta_list(meta, "stack", "stacks", "technologies", "requires_stack"),
    ):
        words.update(normalize_tokens(value))
    return tuple(sorted(words))


def _typed_metadata(skill: Any) -> Any:
    """Validated schema-v2 metadata object when the metadata module is present.

    Read with ``getattr`` only: ``machina.skills.search`` stays a stdlib leaf
    and works with or without ``machina.skills.metadata``.
    """

    return getattr(skill, "skill_metadata", None)


def _metadata_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple, frozenset, set)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _typed_stack_values(typed: Any) -> tuple[str, ...]:
    stack = _meta_value(typed, "stack")
    if stack is None:
        return ()
    return (
        *_metadata_strings(_meta_value(stack, "languages")),
        *_metadata_strings(_meta_value(stack, "frameworks")),
    )


def _declared_families(
    legacy_values: tuple[str, ...], typed_values: tuple[str, ...]
) -> tuple[str, ...]:
    families: list[str] = []
    for raw in (*typed_values, *legacy_values):
        canonical = _DECLARED_FAMILY_ALIASES.get(raw.strip().casefold(), raw.strip().casefold())
        if canonical in _FAMILY_STAGES and canonical not in families:
            families.append(canonical)
    return tuple(families)


def _declared_stages(typed: Any) -> tuple[str, ...]:
    sections = _meta_value(typed, "sections")
    if not isinstance(sections, (list, tuple)):
        return ()
    stages: list[str] = []
    for section in sections:
        stage = _meta_value(section, "stage")
        if isinstance(stage, str) and stage.strip() and stage not in stages:
            stages.append(stage)
    return tuple(stages)


def _phrase_matches(descriptor: QueryDescriptor, terms: Mapping[str, int]) -> tuple[str, ...]:
    seen: set[str] = set()
    matches: list[str] = []
    for position in range(len(descriptor.token_list) - 1):
        first, second = descriptor.token_list[position], descriptor.token_list[position + 1]
        phrase = f"{first} {second}"
        if first in terms and second in terms and phrase not in seen:
            seen.add(phrase)
            matches.append(phrase)
    return tuple(matches)


def retrieve(index: Any, descriptor: QueryDescriptor) -> list[_Retrieved]:
    """Candidate retrieval: lexical evidence plus declared metadata signals.

    Admission is separate from eligibility: exact name/alias matches, boundary
    triggers, declared capabilities/stacks/families, and overlapping query
    words that are distinguishing in this catalog all admit a candidate.
    Inferred task families never widen retrieval; they only score once a
    candidate is admitted (declared families do admit). Schema-v2 metadata
    (``skill.skill_metadata``, when the metadata module is present) is
    preferred and unioned with the legacy ``skill.metadata`` dict, so the v1
    baseline keeps working unchanged.
    """
    rows: list[_Retrieved] = []
    for skill, terms, name_words in zip(index.skills, index.docs, index.names, strict=True):
        legacy = getattr(skill, "metadata", None)
        typed = _typed_metadata(skill)
        aliases = tuple(
            dict.fromkeys(
                (*_metadata_strings(_meta_value(typed, "aliases")), *_meta_list(legacy, "aliases"))
            )
        )
        exact_spelling = _exact_name_match(skill.name, tuple(aliases), descriptor)
        triggers = tuple(
            trigger for trigger in skill.triggers if _trigger_match(trigger, descriptor.raw)
        )
        declared_families = _declared_families(
            _meta_list(legacy, "task_families", "task_family", "families"),
            _metadata_strings(_meta_value(typed, "task_families")),
        )
        inferred_families = frozenset(
            family for family, members in _FAMILY_MEMBERS.items() if members & terms.keys()
        )
        family_pool = set(declared_families) | inferred_families
        matched_families = tuple(
            family
            for family in _FAMILY_STAGES
            if family in descriptor.families and family in family_pool
        )
        capabilities = (
            *_metadata_strings(_meta_value(typed, "capabilities")),
            *_meta_list(legacy, "capabilities", "capability"),
        )
        matched_capabilities = tuple(
            capability
            for capability in capabilities
            if _phrase_in_query(tuple(normalize_tokens(capability)), descriptor)
        )
        declared_stack = _declared_stack(legacy, name_words, _typed_stack_values(typed))
        matched_stack = tuple(word for word in declared_stack if word in descriptor.stacks)
        preconditions = tuple(
            dict.fromkeys(
                (
                    *_metadata_strings(_meta_value(typed, "preconditions")),
                    *_meta_list(legacy, "preconditions"),
                )
            )
        )
        declared_family_signal = bool(set(declared_families) & set(descriptor.families))
        if not (
            exact_spelling
            or triggers
            or matched_capabilities
            or matched_stack
            or declared_family_signal
        ):
            probe = descriptor.tokens & terms.keys()
            if not any(_is_distinguishing(index, word) for word in probe):
                continue
        overlap = frozenset(descriptor.tokens & terms.keys())
        phrase_matches = _phrase_matches(descriptor, terms)
        phrases: list[str] = []
        seen_phrases: set[str] = set()
        for phrase in [*([exact_spelling] if exact_spelling else []), *triggers, *phrase_matches]:
            if phrase and phrase not in seen_phrases:
                seen_phrases.add(phrase)
                phrases.append(phrase)
        rows.append(
            _Retrieved(
                skill=skill,
                terms=terms,
                name_words=name_words,
                overlap=overlap,
                exact_spelling=exact_spelling,
                triggers=triggers,
                phrase_matches=phrase_matches,
                declared_families=declared_families,
                matched_families=matched_families,
                matched_capabilities=matched_capabilities,
                declared_stack=declared_stack,
                matched_stack=matched_stack,
                preconditions=preconditions,
                declared_stages=_declared_stages(typed),
                matched_phrases=tuple(phrases),
            )
        )
    return rows


# ─── eligibility ─────────────────────────────────────────────────────────────


def _exclusion_reasons(descriptor: QueryDescriptor, skill: Any) -> tuple[str, ...]:
    typed = _typed_metadata(skill)
    legacy = getattr(skill, "metadata", None)
    phrases = (
        *_metadata_strings(_meta_value(typed, "exclusions")),
        *_meta_list(legacy, "not_for", "exclude_when", "exclusions", "hard_exclusions"),
    )
    reasons = []
    for phrase in phrases:
        tokens = tuple(normalize_tokens(phrase))
        if tokens and _contains_sequence(descriptor.token_list, tokens):
            reasons.append(f"excluded by skill policy: '{phrase}'")
    return tuple(reasons)


def _required_tools(skill: Any) -> tuple[str, ...]:
    typed = _typed_metadata(skill)
    legacy = getattr(skill, "metadata", None)
    values = (
        *_metadata_strings(_meta_value(typed, "required_tools")),
        *_meta_list(legacy, "requires_tools", "required_tools", "tools"),
    )
    return tuple(value.strip() for value in values if value.strip())


def evaluate_applicability(
    skill: Any,
    descriptor: QueryDescriptor,
    *,
    available_tools: frozenset[str] | None,
    required_tools: tuple[str, ...],
    declared_stack: tuple[str, ...],
    matched_stack: tuple[str, ...],
    preconditions: tuple[str, ...],
    exact_name: bool,
    exclusion_reasons: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    """Eligibility is decided before scoring and never by scores or weights.

    excluded  — disabled skill, hard exclusion match, or a required tool the
                caller's available-tools set is known to lack;
    conditional — required-tool availability unknown, a declared stack the
                query does not confirm, or unverified declared preconditions;
    applicable — otherwise.

    An explicitly named skill (exact name/alias in the query) stays applicable
    for the soft conditions: user selection is not second-guessed. Hard
    conditions (disabled, exclusions, missing tools) still win.
    """
    reasons: list[str] = []
    if not getattr(skill, "enabled", True):
        reasons.append(f"disabled: skill '{skill.name}' is disabled")
        return EXCLUDED, tuple(reasons)
    reasons.extend(exclusion_reasons)
    if required_tools:
        if available_tools is None:
            reasons.append("tool availability unknown: " + ", ".join(sorted(required_tools)))
        else:
            missing = [tool for tool in required_tools if tool not in available_tools]
            if missing:
                reasons.append("missing required tool: " + ", ".join(sorted(missing)))
    if declared_stack and not matched_stack and not exact_name:
        detail = ", ".join(declared_stack)
        if descriptor.stacks:
            detail += " (query mentions: " + ", ".join(sorted(descriptor.stacks)) + ")"
        else:
            detail += " (query does not mention it)"
        reasons.append("declared stack not confirmed: " + detail)
    if preconditions and not exact_name:
        reasons.append("unverified precondition: " + "; ".join(preconditions))
    if any(
        reason.startswith(("disabled:", "missing required tool:", "excluded by skill policy:"))
        for reason in reasons
    ):
        return EXCLUDED, tuple(reasons)
    if reasons:
        return CONDITIONAL, tuple(reasons)
    return APPLICABLE, ()


def compose_stages(
    matched_families: tuple[str, ...], declared_stages: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Possible workflow stages: query-matched families first, then the
    skill's declared section stages (deduplicated, declaration order)."""

    stages: list[str] = []
    for family, stage in _FAMILY_STAGES.items():
        if family in matched_families and stage not in stages:
            stages.append(stage)
    for stage in declared_stages:
        if stage not in stages:
            stages.append(stage)
    return tuple(stages)


# ─── scoring ─────────────────────────────────────────────────────────────────


def _preference_factor(weights: Mapping[str, float] | None, name: str) -> float:
    if not weights:
        return 1.0
    try:
        number = float(weights.get(name))
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(number):
        return 1.0
    return max(_PREFERENCE_MIN, min(_PREFERENCE_MAX, number))


def score_candidate(
    index: Any,
    row: _Retrieved,
    descriptor: QueryDescriptor,
    weights: Mapping[str, float] | None,
) -> dict[str, float]:
    """Named, additive ranking components; the score is exactly their sum.

    ``preference`` is the bounded weight adjustment expressed as an additive
    delta over the base, so ``score == sum(components.values())`` always holds
    and explanations can quote every contribution.
    """
    components: dict[str, float] = {}
    if row.exact_spelling:
        components["exact_name"] = _EXACT_NAME_BOOST
    if row.triggers:
        components["trigger"] = _TRIGGER_BOOST
    terms = row.terms
    length = sum(terms.values())
    lexical = sum(
        index.idf[word]
        * terms[word]
        * _BM25_K
        / (terms[word] + 1.2 * (0.25 + 0.75 * length / index.average))
        for word in sorted(row.overlap)
    )
    if lexical > 0:
        components["lexical"] = lexical
    if row.phrase_matches:
        components["phrase"] = _PHRASE_BOOST * min(_MAX_SIGNAL_MATCHES, len(row.phrase_matches))
    name_overlap = (
        sum(index.idf.get(word, 0.0) for word in sorted(descriptor.tokens & row.name_words))
        * _NAME_OVERLAP_BOOST
    )
    if name_overlap > 0:
        components["name_overlap"] = name_overlap
    if row.matched_families:
        components["task_family"] = _FAMILY_BOOST * min(
            _MAX_SIGNAL_MATCHES, len(row.matched_families)
        )
    if row.matched_capabilities:
        components["capability"] = _CAPABILITY_BOOST * min(
            _MAX_SIGNAL_MATCHES, len(row.matched_capabilities)
        )
    if row.matched_stack:
        components["stack"] = _STACK_BOOST * min(_MAX_SIGNAL_MATCHES, len(row.matched_stack))
    base = sum(components.values())
    factor = _preference_factor(weights, row.skill.name)
    if factor != 1.0:
        components["preference"] = base * (factor - 1.0)
    return components


def build_explanation(
    applicability: str,
    reasons: tuple[str, ...],
    components: Mapping[str, float],
    matched_families: tuple[str, ...],
    stages: tuple[str, ...],
) -> str:
    """Human-readable decision trace derived from the actual ranking values."""

    head = applicability
    if reasons:
        head += ": " + "; ".join(reasons)
    parts = [f"{_COMPONENT_LABELS[name]} {value:+.1f}" for name, value in components.items()]
    if matched_families:
        parts.append("families " + ", ".join(matched_families))
    if stages:
        parts.append("stages " + ", ".join(stages))
    detail = "; ".join(parts) if parts else "no ranking components"
    return f"{head}; {detail}; ranking signal (not a calibrated probability)"


# ─── revisions ───────────────────────────────────────────────────────────────


def content_revision(skill: Any) -> str:
    """Canonical skill content revision: one identity for every consumer.

    SHA-256 (16 hex) over the skill's declared identity — version, the
    content bytes the object carries, bundle path/revision metadata, and the
    validated schema-v2 metadata digest. ``skills.search`` candidates, the
    version-bound section reads (``read_skill_section`` / ``skills.section``),
    and ``machina.skills.revisions`` all bind this value, so a revision shown
    by search is fresh on the first section read and a text or metadata edit
    moves all of them together. Telemetry changes never move it.
    """
    meta = getattr(skill, "metadata", None)
    typed = _typed_metadata(skill)
    typed_digest = ""
    if typed is not None:
        to_dict = getattr(typed, "to_dict", None)
        if callable(to_dict):
            try:
                typed_digest = json.dumps(to_dict(), sort_keys=True, default=str)
            except Exception:  # revision must never fail on odd payloads
                typed_digest = str(typed)
        else:
            typed_digest = str(typed)
    payload = "\n".join(
        (
            str(getattr(skill, "version", "")),
            str(getattr(skill, "content", "")),
            str(_meta_value(meta, "bundle_path") or ""),
            str(_meta_value(meta, "revision") or ""),
            typed_digest,
        )
    )
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]


def catalog_revision(skills: Iterable[Any]) -> str:
    lines = sorted(
        f"{getattr(skill, 'name', '').casefold()}:{content_revision(skill)}:"
        f"{int(bool(getattr(skill, 'enabled', True)))}:{getattr(skill, 'source_type', 'local')}"
        for skill in skills
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


# ─── orchestration ───────────────────────────────────────────────────────────


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.startswith(_CURSOR_PREFIX):
        raise ValueError("invalid cursor")
    suffix = cursor[len(_CURSOR_PREFIX) :]
    if not suffix.isdigit():
        raise ValueError("invalid cursor")
    return int(suffix)


def _classify(
    index: Any, ordered: list[SearchCandidate], counts: dict[str, int], missing_tool: int
) -> str:
    """Bounded state label for the full retrieved set (never a whole catalog).

    ``invalid_catalog`` means the *effective* catalog for this mode has no
    documents (empty registry, or every skill disabled on the automatic path);
    ``all_excluded`` requires retrieved candidates that all failed eligibility.
    """

    if index.doc_count == 0:
        return "invalid_catalog"
    if not ordered:
        return "no_lexical_evidence"
    if counts[APPLICABLE]:
        return "ok"
    if missing_tool and not counts[CONDITIONAL]:
        return "missing_capability"
    if counts[CONDITIONAL]:
        return "conditional_only"
    return "all_excluded"


def execute_search(
    index: Any,
    query: str,
    *,
    weights: Mapping[str, float] | None = None,
    limit: int = 10,
    cursor: str | None = None,
    available_tools: Iterable[str] | None = None,
    typo_tolerance: bool = False,
) -> SearchResult:
    """Run the full deterministic pipeline over a ``runtime.SkillIndex``."""
    if limit < 1:
        raise ValueError("limit must be a positive integer")
    offset = _decode_cursor(cursor)
    available = (
        None if available_tools is None else frozenset(str(tool) for tool in available_tools)
    )
    descriptor = build_query_descriptor(query, vocabulary=index.idf, typo_tolerance=typo_tolerance)
    rows = retrieve(index, descriptor)
    counts = {APPLICABLE: 0, CONDITIONAL: 0, EXCLUDED: 0}
    missing_tool_exclusions = 0
    scored = []
    for row in rows:
        applicability, reasons = evaluate_applicability(
            row.skill,
            descriptor,
            available_tools=available,
            required_tools=_required_tools(row.skill),
            declared_stack=row.declared_stack,
            matched_stack=row.matched_stack,
            preconditions=row.preconditions,
            exact_name=row.exact_spelling is not None,
            exclusion_reasons=_exclusion_reasons(descriptor, row.skill),
        )
        counts[applicability] += 1
        if any(reason.startswith("missing required tool:") for reason in reasons):
            missing_tool_exclusions += 1
        components = score_candidate(index, row, descriptor, weights)
        stages = compose_stages(row.matched_families, row.declared_stages)
        candidate = SearchCandidate(
            skill_id=row.skill.name.casefold(),
            name=row.skill.name,
            content_revision=content_revision(row.skill),
            score=sum(components.values()),
            score_components=components,
            matched_phrases=row.matched_phrases,
            matched_capabilities=row.matched_capabilities,
            applicability=applicability,
            reasons=reasons,
            workflow_stages=stages,
            explanation=build_explanation(
                applicability, reasons, components, row.matched_families, stages
            ),
        )
        scored.append(
            (
                _APPLICABILITY_ORDER[applicability],
                -candidate.score,
                _SOURCE_PRECEDENCE.get(
                    getattr(row.skill, "source_type", "local"), _DEFAULT_SOURCE_PRECEDENCE
                ),
                candidate.name.casefold(),
                candidate.skill_id,
                candidate,
            )
        )
    scored.sort(key=lambda item: item[:5])
    ordered = [item[5] for item in scored]
    page = ordered[offset : offset + limit]
    next_cursor = _CURSOR_PREFIX + str(offset + limit) if offset + limit < len(ordered) else None
    diagnostics = {
        "classification": _classify(index, ordered, counts, missing_tool_exclusions),
        "catalog_size": index.doc_count,
        "retrieved": len(ordered),
        "applicable": counts[APPLICABLE],
        "conditional": counts[CONDITIONAL],
        "excluded": counts[EXCLUDED],
        "corrections": [list(pair) for pair in descriptor.corrections],
        "weights_applied": bool(weights),
    }
    return SearchResult(
        catalog_revision=catalog_revision(index.skills),
        query_descriptor_revision=QUERY_DESCRIPTOR_REVISION,
        policy_revision=POLICY_REVISION,
        candidates=page,
        next_cursor=next_cursor,
        diagnostics=diagnostics,
    )
