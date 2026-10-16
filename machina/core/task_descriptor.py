"""Shared deterministic TaskDescriptor contract (spec §8).

One normalized task representation over the existing intent layer
(:mod:`machina.core.intent`) and the workflow planner's coding/research word
sets. No model call, embedding, provider, or external process is involved.

This descriptor is guidance, not authorization: it is **not a security boundary**.
Tools and the permission policy still enforce actual permissions; constraints
recorded here grant or revoke nothing, and quoted text is never treated as
permission.

Design rules (spec §8):

- ``task_class`` reuses ``machina.core.intent.TASK_CLASSES``; this module adds
  no third classifier. Family and stack facts are composed from the intent
  layer's exported phrase rules and shared word sets.
- Broad recognition stays narrow: the web-build family requires an action plus
  a web-building noun, and paired quoted example spans are ignored.
- Missing stack is a normal unresolved field. Project facts come only from
  already-known input or a bounded single-file ``package.json`` read. Package
  scripts are never executed and directories are never scanned recursively.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from machina.core.intent import (
    AMBIGUOUS_TECHNOLOGIES,
    CODING_TASK_WORDS,
    FOLLOW_UP_STACK_WORDS,
    RESEARCH_TASK_WORDS,
    TASK_CLASSES,
    classify_task_conservative,
    detect_constraints,
    released_constraints,
    strip_quoted_spans,
    web_build_request,
)
from machina.skills.runtime import _TECHNOLOGIES, _words

#: Closed vocabulary of task families, in canonical disclosure order.
TASK_FAMILIES = ("web-build", "coding", "research", "review", "task", "continuation")

#: Operation scope values (spec §8.1).
OPERATION_SCOPES = ("mutating", "read-only", "unknown")

#: Stack categories, in canonical order.
STACK_CATEGORIES = ("languages", "frameworks", "tools")

#: Provenance vocabulary for stack facts (spec §8.4).
STACK_PROVENANCE = ("known", "inherited", "unknown")

#: Unresolved-field vocabulary, in canonical order.
UNRESOLVED_FIELDS = ("stack", "task_family")

#: Evidence sources attached to inferred facts.
EVIDENCE_SOURCES = (
    "query",
    "rule",
    "task_state",
    "recent_context",
    "goal",
    "project_metadata",
)

#: Capability vocabulary, in canonical order, mapped to normalized request words.
_CAPABILITY_WORDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("testing", frozenset({"testing"})),
    ("authentication", frozenset({"authentication", "login", "oauth"})),
    ("security", frozenset({"security"})),
    ("database", frozenset({"database", "sql", "postgres", "mysql", "clickhouse"})),
    ("payments", frozenset({"payment", "payments", "stripe", "billing", "checkout"})),
    ("deployment", frozenset({"deploy", "deployment", "docker", "kubernetes"})),
    ("documentation", frozenset({"docs", "documentation"})),
    ("api", frozenset({"api"})),
    ("research", frozenset({"research", "investigate", "literature"})),
    ("review", frozenset({"review", "feedback"})),
    ("ui", frozenset({"ui", "ux", "frontend"})),
    ("accessibility", frozenset({"accessibility"})),
)

#: Detected technology tokens grouped for stack facts. Anything detected but
#: not listed falls back to ``tools`` so a new technology never breaks the
#: builder.
_STACK_LANGUAGES = frozenset(
    {
        "python",
        "rust",
        "go",
        "java",
        "kotlin",
        "swift",
        "perl",
        "cpp",
        "csharp",
        "fsharp",
        "dotnet",
        "dart",
        "ruby",
        "php",
        "javascript",
        "typescript",
        "elixir",
        "scala",
        "lua",
        "sql",
        "objectivec",
        "zig",
        "julia",
        "haskell",
        "ocaml",
        "erlang",
        "clojure",
        "crystal",
        "nim",
    }
)
_STACK_FRAMEWORKS = frozenset(
    {
        "react",
        "vue",
        "angular",
        "django",
        "laravel",
        "springboot",
        "quarkus",
        "rails",
        "fastapi",
        "nestjs",
        "nuxt",
        "nuxt4",
        "nextjs",
        "flutter",
        "pytorch",
        "swiftui",
        "svelte",
        "solid",
        "solidjs",
        "remix",
        "astro",
        "express",
    }
)

#: Bounded single-file metadata read; no script execution, no directory walk.
_MANIFEST_NAME = "package.json"
_MANIFEST_MAX_BYTES = 256 * 1024
_MANIFEST_MAX_DEPENDENCIES = 64
_FOLLOW_UP_MAX_WORDS = 16

#: Constraints that force read-only scope when stated (others scope-neutral).
_SCOPE_READONLY_CONSTRAINTS = frozenset({"read-only", "no-edit"})


@dataclass(frozen=True)
class FactEvidence:
    """Provenance for one inferred fact (field/value plus its source)."""

    field: str
    value: str
    source: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "value": self.value,
            "source": self.source,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TaskDescriptor:
    """Normalized, deterministic task representation consumed by skills/planning.

    Stack fields map category (``languages``/``frameworks``/``tools``) to a
    sorted tuple of technology tokens; empty means unknown. Effective stack
    precedence is ``explicit_stack`` > ``project_stack`` > ``inherited_stack``.
    """

    task_class: str
    task_families: tuple[str, ...]
    requested_capabilities: tuple[str, ...]
    explicit_stack: dict[str, tuple[str, ...]]
    project_stack: dict[str, tuple[str, ...]]
    inherited_stack: dict[str, tuple[str, ...]]
    operation_scope: str
    constraints: tuple[str, ...]
    unresolved_fields: tuple[str, ...]
    evidence: tuple[FactEvidence, ...]
    task_state_revision: int

    def effective_stack(self) -> dict[str, tuple[str, ...]]:
        """Merged stack facts with per-category precedence, canonical order."""
        stack: dict[str, tuple[str, ...]] = {}
        for category in STACK_CATEGORIES:
            tokens = (
                self.explicit_stack.get(category)
                or self.project_stack.get(category)
                or self.inherited_stack.get(category)
            )
            if tokens:
                stack[category] = tokens
        return stack

    def stack_provenance(self) -> str:
        """``known`` (explicit/project), ``inherited``, or ``unknown`` (§8.4)."""
        if self.explicit_stack or self.project_stack:
            return "known"
        if self.inherited_stack:
            return "inherited"
        return "unknown"

    def evidence_for(self, field: str) -> tuple[FactEvidence, ...]:
        return tuple(item for item in self.evidence if item.field == field)

    def explain(self) -> tuple[str, ...]:
        """Deterministic one-line reasons derived from recorded evidence."""
        lines = []
        for item in self.evidence:
            line = f"{item.field}={item.value} [{item.source}]"
            if item.detail:
                line += f" ({item.detail})"
            lines.append(line)
        return tuple(lines)

    def export_state(self) -> dict[str, Any]:
        """Minimal task-state mapping a later build can continue from."""
        return {
            "active_class": self.task_class,
            "active_families": list(self.task_families),
            "stack": {
                category: list(tokens) for category, tokens in self.effective_stack().items()
            },
            "constraints": list(self.constraints),
            "capabilities": list(self.requested_capabilities),
            "revision": self.task_state_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form for previews and gateway contracts."""
        return {
            "task_class": self.task_class,
            "task_families": list(self.task_families),
            "requested_capabilities": list(self.requested_capabilities),
            "explicit_stack": {key: list(value) for key, value in self.explicit_stack.items()},
            "project_stack": {key: list(value) for key, value in self.project_stack.items()},
            "inherited_stack": {key: list(value) for key, value in self.inherited_stack.items()},
            "operation_scope": self.operation_scope,
            "constraints": list(self.constraints),
            "unresolved_fields": list(self.unresolved_fields),
            "evidence": [item.to_dict() for item in self.evidence],
            "task_state_revision": self.task_state_revision,
            "effective_stack": {key: list(value) for key, value in self.effective_stack().items()},
            "stack_provenance": self.stack_provenance(),
        }


def build_task_descriptor(
    query: str,
    *,
    task_state: Mapping[str, Any] | None = None,
    recent: Sequence[Mapping[str, Any]] | None = None,
    goal: str | None = None,
    project_metadata: Mapping[str, Any] | str | Path | None = None,
) -> TaskDescriptor:
    """Normalize one request (plus bounded known state) into a TaskDescriptor.

    ``task_state`` follows ``machina.controller.triage.resolve_task`` keys
    (``active_class``) and may additionally carry ``active_families``,
    ``stack``, ``constraints``, ``capabilities``, and ``revision`` (the shape
    produced by :meth:`TaskDescriptor.export_state`). ``recent``/``goal`` are
    read-only context; ``project_metadata`` is already-known information or a
    path to a single existing ``package.json``. No script, spawned process,
    network call, or recursive scan ever runs here.
    """
    evidence: list[FactEvidence] = []
    state: Mapping[str, Any] | None = (
        task_state if isinstance(task_state, Mapping) and task_state else None
    )
    text = str(query or "")
    text_stripped = strip_quoted_spans(text)
    words = set(_words(text_stripped))

    # --- class: reuse intent, resolve bare follow-ons against explicit state ---
    query_class = classify_task_conservative(text)
    state_class = state.get("active_class") if state else None
    if state_class not in TASK_CLASSES or state_class == "ambiguous":
        state_class = None
    task_class = state_class if query_class == "ambiguous" and state_class else query_class
    if task_class == state_class and query_class == "ambiguous" and state_class:
        evidence.append(
            FactEvidence(
                "task_class",
                task_class,
                "task_state",
                f"ambiguous follow-on resolved to active class {state_class}",
            )
        )
    else:
        evidence.append(FactEvidence("task_class", task_class, "query", "classified request"))

    continuation = bool(state) and (
        query_class == "ambiguous"
        or bool(words & FOLLOW_UP_STACK_WORDS)
        or len(words) <= _FOLLOW_UP_MAX_WORDS
    )

    # A bare ambiguous English-word technology ("go", "rust", "swift") is a
    # topic, never a stack claim (spec §8.2) — including when inherited.
    ambiguous_named = (
        {token for token in AMBIGUOUS_TECHNOLOGIES if token in words}
        if query_class == "ambiguous"
        else set()
    )

    # --- stacks ---------------------------------------------------------------
    explicit_stack = _categorize(sorted((words & _TECHNOLOGIES) - ambiguous_named))
    for category, tokens in explicit_stack.items():
        for token in tokens:
            evidence.append(
                FactEvidence("explicit_stack", token, "query", f"named in request ({category})")
            )

    project_stack, project_detail = _read_project_metadata(project_metadata)
    for category, tokens in project_stack.items():
        for token in tokens:
            evidence.append(
                FactEvidence(
                    "project_stack",
                    token,
                    "project_metadata",
                    f"already-known project fact ({project_detail}, {category})",
                )
            )

    inherited_stack, inherited_source, inherited_detail = _inherited_stack(
        state, recent, goal, ambiguous_named
    )
    for category, tokens in inherited_stack.items():
        for token in tokens:
            evidence.append(
                FactEvidence(
                    "inherited_stack",
                    token,
                    inherited_source or "recent_context",
                    f"{inherited_detail} ({category})",
                )
            )

    # --- capabilities (lexical; ambiguous follow-ons may inherit state) -------
    if query_class == "ambiguous" and state:
        requested_capabilities = _state_capabilities(state)
        capability_source = "task_state"
    else:
        requested_capabilities = _detect_capabilities(words)
        capability_source = "query"
    for capability in requested_capabilities:
        evidence.append(
            FactEvidence(
                "requested_capabilities",
                capability,
                capability_source,
                "requested capability",
            )
        )

    # --- constraints (sticky; explicit release only) --------------------------
    constraints, released = _merge_constraints(state, text)
    for constraint in constraints:
        source = "query" if constraint in detect_constraints(text) else "task_state"
        evidence.append(FactEvidence("constraints", constraint, source, "active constraint"))
    for constraint in sorted(released):
        evidence.append(
            FactEvidence("constraint_release", constraint, "query", "explicit release phrase")
        )

    # --- families -------------------------------------------------------------
    families, family_sources = _task_families(
        task_class, query_class, text, words, state, ambiguous_named, continuation
    )
    for family in families:
        evidence.append(FactEvidence("task_families", family, family_sources[family]))

    # --- operation scope --------------------------------------------------------
    operation_scope = _operation_scope(task_class, constraints)
    evidence.append(
        FactEvidence(
            "operation_scope",
            operation_scope,
            "rule",
            "read-only constraint"
            if operation_scope == "read-only" and constraints
            else f"task_class={task_class}",
        )
    )

    # --- unresolved fields ------------------------------------------------------
    unresolved = []
    if task_class in {"implement", "debug"} and not (
        explicit_stack or project_stack or inherited_stack
    ):
        unresolved.append("stack")
    if task_class == "ambiguous" and not families:
        unresolved.append("task_family")
    unresolved_fields = tuple(field for field in UNRESOLVED_FIELDS if field in unresolved)
    for field in unresolved_fields:
        evidence.append(FactEvidence("unresolved_fields", field, "rule", "no evidence available"))

    # --- revision ----------------------------------------------------------------
    revision = 0
    if state:
        try:
            revision = max(0, int(state.get("revision", 0))) + 1
        except (TypeError, ValueError):
            revision = 1
        evidence.append(
            FactEvidence(
                "task_state_revision",
                str(revision),
                "task_state",
                f"incremented prior revision {revision - 1}",
            )
        )

    return TaskDescriptor(
        task_class=task_class,
        task_families=families,
        requested_capabilities=requested_capabilities,
        explicit_stack=explicit_stack,
        project_stack=project_stack,
        inherited_stack=inherited_stack,
        operation_scope=operation_scope,
        constraints=constraints,
        unresolved_fields=unresolved_fields,
        evidence=tuple(evidence),
        task_state_revision=revision,
    )


def preview_task_descriptor(
    query: str,
    *,
    task_state: Mapping[str, Any] | None = None,
    recent: Sequence[Mapping[str, Any]] | None = None,
    goal: str | None = None,
    project_metadata: Mapping[str, Any] | str | Path | None = None,
) -> dict[str, Any]:
    """Call-free preview payload: descriptor dict plus deterministic reasons.

    Mirrors the read-only preview contract intent (spec §12.2): building a
    preview creates no agent turn, calls no provider or embedding, executes no
    tool, installs nothing, and mutates no approval state.
    """
    descriptor = build_task_descriptor(
        query,
        task_state=task_state,
        recent=recent,
        goal=goal,
        project_metadata=project_metadata,
    )
    return {"descriptor": descriptor.to_dict(), "reasons": list(descriptor.explain())}


def _categorize(tokens: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Group technology tokens into canonical stack categories."""
    grouped: dict[str, list[str]] = {category: [] for category in STACK_CATEGORIES}
    for token in sorted({token for token in tokens if token}):
        if token in _STACK_LANGUAGES:
            grouped["languages"].append(token)
        elif token in _STACK_FRAMEWORKS:
            grouped["frameworks"].append(token)
        else:
            grouped["tools"].append(token)
    return {
        category: tuple(grouped[category]) for category in STACK_CATEGORIES if grouped[category]
    }


def _normalize_stack(raw: Any) -> dict[str, tuple[str, ...]]:
    """Bound a caller-provided stack mapping or token list to known categories."""
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        stack: dict[str, tuple[str, ...]] = {}
        for category in STACK_CATEGORIES:
            values = raw.get(category)
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, (list, tuple, set, frozenset)):
                continue
            tokens = sorted(
                {str(value).casefold().strip() for value in values if isinstance(value, str)}
            )
            tokens = [token for token in tokens if token]
            if tokens:
                stack[category] = tuple(tokens)
        return stack
    if isinstance(raw, (list, tuple, set, frozenset)):
        return _categorize(str(value).casefold() for value in raw if isinstance(value, str))
    return {}


def _read_project_metadata(
    source: Mapping[str, Any] | str | Path | None,
) -> tuple[dict[str, tuple[str, ...]], str]:
    """Read already-known project facts or one bounded ``package.json`` file."""
    if source is None:
        return {}, ""
    if isinstance(source, Mapping):
        nested = source.get("stack", source)
        return _normalize_stack(nested), "provided metadata"
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_dir():
            path = path / _MANIFEST_NAME
        if path.name == _MANIFEST_NAME and path.is_file():
            return _stack_from_package_json(path), path.name
    return {}, ""


def _stack_from_package_json(path: Path) -> dict[str, tuple[str, ...]]:
    """Extract stack tokens from one manifest file; never runs its scripts."""
    try:
        payload = path.read_text(encoding="utf-8", errors="replace")[:_MANIFEST_MAX_BYTES]
        data = json.loads(payload)
    except Exception:
        return {}
    if not isinstance(data, Mapping):
        return {}
    tokens: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        declared = data.get(section)
        if not isinstance(declared, Mapping):
            continue
        for name in list(declared)[:_MANIFEST_MAX_DEPENDENCIES]:
            tokens.update(_words(str(name)))
    return _categorize(sorted(tokens & _TECHNOLOGIES))


def _recent_snippets(
    recent: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[str], list[str]]:
    """Bounded recent-context texts, mirroring the workflow planner's window."""
    user_snippets: list[str] = []
    other_snippets: list[str] = []
    for chunk in list(recent or [])[:3]:
        if not isinstance(chunk, Mapping):
            continue
        kind = chunk.get("chunk_type", chunk.get("type"))
        if kind not in ("user_message", "assistant_message", "compression_summary"):
            continue
        payload = chunk.get("payload", {})
        if isinstance(payload, Mapping):
            text = str(payload.get("content", ""))[:1500]
            (user_snippets if kind == "user_message" else other_snippets).append(text)
    return user_snippets, other_snippets


def _inherited_stack(
    state: Mapping[str, Any] | None,
    recent: Sequence[Mapping[str, Any]] | None,
    goal: str | None,
    ambiguous_named: set[str],
) -> tuple[dict[str, tuple[str, ...]], str | None, str]:
    """First available prior stack: explicit state, then recent, goal, history."""
    if state:
        state_stack = _normalize_stack(state.get("stack"))
        if state_stack:
            return state_stack, "task_state", "active task stack"
    user_snippets, other_snippets = _recent_snippets(recent)
    candidates: list[tuple[str, str, str]] = [
        (text, "recent_context", "inherited from recent user message") for text in user_snippets
    ]
    if goal:
        candidates.append((goal, "goal", "inherited from session goal"))
    candidates.extend(
        (text, "recent_context", "inherited from recent context") for text in other_snippets
    )
    for text, source, detail in candidates:
        tokens = (set(_words(text)) & _TECHNOLOGIES) - ambiguous_named
        if tokens:
            return _categorize(sorted(tokens)), source, detail
    return {}, None, ""


def _detect_capabilities(words: set[str]) -> tuple[str, ...]:
    return tuple(capability for capability, signals in _CAPABILITY_WORDS if words & signals)


def _state_capabilities(state: Mapping[str, Any]) -> tuple[str, ...]:
    raw = state.get("capabilities")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    return tuple(
        dict.fromkeys(str(value).casefold() for value in raw if isinstance(value, str) and value)
    )


def _merge_constraints(
    state: Mapping[str, Any] | None, text: str
) -> tuple[tuple[str, ...], frozenset[str]]:
    """Sticky constraints: prior and stated constraints minus explicit releases."""
    released = released_constraints(text)
    merged: list[str] = []
    if state:
        prior = state.get("constraints")
        if isinstance(prior, (list, tuple, set, frozenset)):
            merged.extend(str(value) for value in prior if isinstance(value, str) and value)
    merged.extend(detect_constraints(text))
    ordered = [token for token in dict.fromkeys(merged) if token not in released]
    return tuple(ordered), released


def _task_families(
    task_class: str,
    query_class: str,
    text: str,
    words: set[str],
    state: Mapping[str, Any] | None,
    ambiguous_named: set[str],
    continuation: bool,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Compose families from intent rules plus explicit state; canonical order."""
    found: dict[str, str] = {}
    if query_class == "ambiguous" and state:
        active = state.get("active_families")
        if isinstance(active, (list, tuple, set, frozenset)):
            for family in active:
                if isinstance(family, str) and family in TASK_FAMILIES:
                    found[family] = "task_state"
    if task_class in {"implement", "debug", "review-readonly"}:
        if task_class == "review-readonly" and "review" not in found:
            found["review"] = "rule"
        if task_class in {"implement", "debug"} and web_build_request(text):
            found["web-build"] = "rule"
        coding = bool(((words & _TECHNOLOGIES) - ambiguous_named) | (words & CODING_TASK_WORDS))
        research = bool(words & RESEARCH_TASK_WORDS) and not coding
        if coding:
            found["coding"] = "rule"
        elif research:
            found["research"] = "rule"
        elif not found:
            found["task"] = "rule"
    if continuation and found:
        found["continuation"] = "rule"
    families = tuple(family for family in TASK_FAMILIES if family in found)
    return families, found


def _operation_scope(task_class: str, constraints: tuple[str, ...]) -> str:
    if set(constraints) & _SCOPE_READONLY_CONSTRAINTS:
        return "read-only"
    if task_class in {"conversational", "explain", "learn-directory", "review-readonly"}:
        return "read-only"
    if task_class in {"implement", "debug"}:
        return "mutating"
    return "unknown"
