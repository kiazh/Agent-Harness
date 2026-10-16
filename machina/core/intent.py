"""Cheap conversational routing shared by skills and learned task strategies.

This is guidance, not an authorization decision or a semantic classifier.
Uncertain requests remain eligible for task routing; no provider call is added.

Extensions for the shared deterministic TaskDescriptor (spec §8) stay additive:
:func:`classify_task` and :func:`conversational` keep their exact signatures and
prior behavior, while :func:`classify_task_conservative`,
:func:`web_build_request`, :func:`strip_quoted_spans`,
:func:`detect_constraints`, and :func:`released_constraints` expose the narrow
phrase rules the descriptor normalizes. The shared task-family word sets below
are the single source of truth imported by ``machina.skills.workflow`` (no third
classifier).
"""

import re

_GREETING = re.compile(
    r"\s*(?:hello(?: there)?|hi|hey|thanks|thank you|ok|okay|bye)"
    r"[\s!.,]*(?:thanks)?\s*",
    re.I,
)
_QUESTION = re.compile(
    r"\s*(?:(?:can|could|would)\s+you\s+)?"
    r"(?:what\b|why\b|how\b|who\b|when\b|where\b|explain\b|describe\b|"
    r"tell\s+me\b|(?:do|have)\s+(?:you|we|i|they)\b|does\b|did\b|is\b|are\b|has\b|"
    r"thanks\b|thank\s+you\b)",
    re.I,
)
_FOLLOW_ON_ACTION = re.compile(
    r"(?:[.!?;,]\s*|\b(?:and|then)\s+)"
    r"(?:then\s+|now\s+|please\s+)*"
    r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"(?:fix|build|implement|add|refactor|review|research|create|test|check|"
    r"inspect|run|verify|investigate|search|read|write|export|plan|design|"
    r"change|update|modify|edit|remove|delete|set|enable|disable|rename|"
    r"migrate|deploy|install|configure|switch)\b",
    re.I,
)
# An explicit explanation clause after a clause boundary ("…, just explain …").
# Used only to read the request that remains once a negated action is removed
# (see :func:`classify_task`); it never matches a bare "how"/"what" question on
# its own, so "don't touch it, how about you build the site" cannot flip.
_EXPLAIN_CLAUSE = re.compile(
    r"(?:^|[.!?;,]\s*|\b(?:and|then)\s+)"
    r"(?:just\s+|only\s+)?(?:please\s+)?(?:explain|describe)\b",
    re.I,
)

# Task-classifier v2: regex + keyword sets only, no model calls.
# Precedence is load-bearing: ambiguous short follow-ons first (never guess),
# then explicit read-only review, then error signals (debug), then bare review
# verbs, then repo-layout questions, then the conversational() split, then any
# remaining action verb (implement). Anything unrecognized stays ambiguous.

_AMBIGUOUS_FOLLOW_ON = re.compile(
    r"\s*(?:continue|go\s+on|proceed|do\s+it|go\s+ahead|and\s+then\??|"
    r"just\s+write\s+the\s+file|yes(?:\s+do\s+(?:it|that))?|"
    r"same\s+for\s+the\s+rest|switch)\s*[!?.]*\s*",
    re.I,
)

_READONLY_MARKERS = re.compile(
    r"(?:just\s+review|review\s+only|(?:don't|dont|do\s+not)\s+implement|"
    r"do\s+not\s+(?:edit|change|modify|touch)|no\s+changes?|"
    r"without\s+(?:chang|modif|edit|touch)|read[\s-]?only|give\s+feedback)",
    re.I,
)

_DEBUG_SIGNALS = re.compile(
    r"(?:traceback|stack\s*trace|assertion|exception|importerror|typeerror|"
    r"valueerror|keyerror|segfault|core\s+dump|crash(?:es|ed|ing)?|"
    r"fail(?:ing|ed|ure|s)?|broken|bug|500\s+error|error|what'?s\s+wrong|"
    r"why\s+does\b.{0,80}\bfail|\bdebug\b|\bfix\b.{0,40}\b(?:it|this|the)\b|"
    r"\bfix\b)",
    re.I,
)

_REVIEW_VERBS = re.compile(
    r"(?:\breview\b|code\s+review|take\s+a\s+look|have\s+a\s+look|"
    r"look\s+at\b.{0,40}\bfailures?\b|give\s+feedback)",
    re.I,
)

_LEARN_DIRECTORY = re.compile(
    r"(?:where\s+(?:is|are)\b|list\s+(?:all\s+)?\w*\s*files?|"
    r"project\s+structure|repos\w*\s+(?:layout|structure)|"
    r"layout\s+of\s+this\s+repo|what\s+files?\b.{0,40}\b(?:repo|project|here)\b|"
    r"directory\s+(?:layout|structure|listing))",
    re.I,
)

_ACTION_VERB = re.compile(
    r"\b(?:fix|build|implement|add|refactor|review|research|create|test|check|"
    r"inspect|run|verify|investigate|search|read|write|export|plan|design|"
    r"change|update|modify|edit|remove|delete|set|enable|disable|rename|"
    r"migrate|deploy|install|configure|switch)\b",
    re.I,
)

# ─── Shared TaskDescriptor vocabulary (spec §8; single source of truth) ──────

#: Technology names that are ordinary English words. A bare token alone is a
#: topic, not an implementation authorization (spec §8.2 "go/rust/swift";
#: "solid" joins them: SolidJS is named by an ordinary adjective).
AMBIGUOUS_TECHNOLOGIES = frozenset({"go", "rust", "swift", "solid"})

#: Task words that mark coding work even without a named technology. Hoisted
#: from ``machina.skills.workflow.plan_workflow`` so intent, workflow, and the
#: TaskDescriptor share one set (spec §8.1: no third classifier).
CODING_TASK_WORDS = frozenset(
    {
        "api",
        "debug",
        "refactor",
        "bug",
        "feature",
        "function",
        "component",
        "coding",
        "programming",
        "testing",
    }
)

#: Task words that select the research family (mirrors the workflow planner).
RESEARCH_TASK_WORDS = frozenset({"research", "investigate", "competitors", "literature", "pubmed"})

#: Follow-up words allowed to inherit a stack from recent context.
FOLLOW_UP_STACK_WORDS = frozenset(
    {
        "testing",
        "fix",
        "debug",
        "refactor",
        "continue",
        "add",
        "implement",
        "optimize",
        "component",
        "function",
        "api",
    }
)

# ─── Quoted-example conservatism (spec §8.3) ────────────────────────────────

# Paired quotes only: an unpaired apostrophe in "don't" is ordinary text and
# must not swallow the rest of the request. A regex cannot parse every quoting
# construction; the descriptor stays conservative where it is unsure.
_QUOTED_SPANS = re.compile(
    "\"[^\"\\n]*\"|\u201c[^\u201d\\n]*\u201d|\u00ab[^\u00bb\\n]*\u00bb|`[^`\\n]*`|'[^']{2,}'"
)

# ─── Narrow web-build phrase rule: action + web noun (spec §8.2) ────────────

_WEB_BUILD_ACTION = re.compile(
    r"\b(?:make|build|create|design|develop|ship|scaffold|construct|craft|"
    r"launch|write|code|implement|bootstrap)\b|\bspin\s+up\b|\bset\s+up\b",
    re.I,
)
#: Closed phrase list for web-building nouns (spec §8.2). A noun qualifies only
#: as an explicit compound ("homepage", "landing page") or a qualified site
#: phrase ("marketing site"); bare "site" is deliberately absent so unrelated
#: contexts ("stock portfolio", a question about how a band site gets made)
#: keep their observed non-web decisions.
_WEB_BUILD_NOUN = re.compile(
    r"\b(?:website|web\s+site|web\s?page|home\s?page|"
    r"web\s+app(?:lication)?|webapp|landing\s+page|"
    r"portfolio\s+(?:website|web\s+site|site|page)|"
    r"(?:web|portfolio|e-?commerce|marketing)\s+site|"
    r"web\s+front[\s-]?end|"
    r"front[\s-]?end\s+(?:site|app|website|project))\b",
    re.I,
)
# "make a decision/plan about the website" is deliberation, not a build ask.
_NON_BUILD_MAKE = re.compile(
    r"\bmake\s+(?:a\s+|an\s+|the\s+|me\s+a\s+)?(?:decision|choice|comparison|"
    r"list|plan|sense|sure|money|room|note|claim|judgment|judgement|complaint|"
    r"request)\b",
    re.I,
)
_WEB_BUILD_WINDOW = 48

# ─── Sticky constraints (spec §8.3) ─────────────────────────────────────────

_READONLY_CONSTRAINT = re.compile(
    r"(?:just\s+review|review\s+only|(?:don't|dont|do\s+not)\s+"
    r"(?:implement|edit|change|modify|touch|write)|no\s+changes?|"
    r"without\s+(?:chang|modif|edit|touch)|read[\s-]?only|give\s+feedback)",
    re.I,
)
_NEGATED_ACTION = re.compile(
    r"\b(?:don't|dont|do\s+not|never|without|no\s+need\s+to|no\s+need)\b"
    r"[\s,:-]*(?:(?:actually|really|ever|just|now|please)\s+)*"
    r"(?:(?:want|need)\s+to\s+)?"
    r"(install|deploy|edit|change|modify|touch|delete|remove|commit|push|run|"
    r"execute|write|build)\b",
    re.I,
)
_CONSTRAINT_FOR_VERB = {
    "edit": "no-edit",
    "change": "no-edit",
    "modify": "no-edit",
    "touch": "no-edit",
    "write": "no-edit",
    "delete": "no-delete",
    "remove": "no-delete",
    "install": "no-install",
    "deploy": "no-deploy",
    "commit": "no-commit",
    "push": "no-push",
    "run": "no-execute",
    "execute": "no-execute",
    # "don't build anything" forbids creating, i.e. forces the read-only scope;
    # there is no separate "no-build" token in CONSTRAINTS.
    "build": "read-only",
}
#: Canonical constraint tokens in stable order.
CONSTRAINTS = (
    "read-only",
    "no-edit",
    "no-delete",
    "no-install",
    "no-deploy",
    "no-commit",
    "no-push",
    "no-execute",
)

_CONSTRAINT_RELEASE = re.compile(
    r"(?:you\s+(?:can|may)|feel\s+free\s+to|go\s+ahead\s+and|now\s+you\s+can)"
    r"\s+(?:now\s+)?(?:edit|change|modify|write|make\s+changes|implement)\b"
    r"|no\s+longer\s+read[\s-]?only",
    re.I,
)

#: Constraint tokens cleared by an explicit release phrase (spec §8.3).
_RELEASABLE_CONSTRAINTS = frozenset({"read-only", "no-edit"})


def strip_quoted_spans(text: str) -> str:
    """Replace paired quoted spans with a space (quoted text is not permission).

    Conservative by design: only paired quotes are stripped, so apostrophes in
    ordinary words (``don't``) never remove request text.
    """
    return _QUOTED_SPANS.sub(" ", text)


def web_build_request(query: str) -> bool:
    """Whether *query* is an action + web-noun build request (spec §8.2).

    Narrow phrase rule only: requires an action verb near a web-building noun.
    A financial "portfolio" or a quoted example never matches on its own.
    """
    text = strip_quoted_spans(query)
    for match in _WEB_BUILD_ACTION.finditer(text):
        action = match.group(0).casefold()
        if action.startswith("make") and _NON_BUILD_MAKE.match(text, match.start()):
            continue
        window = text[match.end() : match.end() + _WEB_BUILD_WINDOW]
        if _WEB_BUILD_NOUN.search(window):
            return True
    return False


def detect_constraints(query: str) -> tuple[str, ...]:
    """Canonical read-only/negation constraints stated in *query* (spec §8.3)."""
    found: set[str] = set()
    if _READONLY_CONSTRAINT.search(query):
        found.add("read-only")
    for match in _NEGATED_ACTION.finditer(query):
        found.add(_CONSTRAINT_FOR_VERB[match.group(1).casefold()])
    return tuple(token for token in CONSTRAINTS if token in found)


def released_constraints(query: str) -> frozenset[str]:
    """Constraint tokens explicitly released by *query* (sticky otherwise)."""
    if not _CONSTRAINT_RELEASE.search(query):
        return frozenset()
    return _RELEASABLE_CONSTRAINTS


TASK_CLASSES = (
    "conversational",
    "explain",
    "learn-directory",
    "review-readonly",
    "debug",
    "implement",
    "ambiguous",
)


def conversational(query: str) -> bool:
    """Whether a greeting/question has no explicit follow-on action."""
    return bool(
        _GREETING.fullmatch(query)
        or (_QUESTION.match(query) and not _FOLLOW_ON_ACTION.search(query))
    )


def classify_task(query: str) -> str:
    """Deterministic task class for a turn query (regex/keywords only).

    Returns one of ``TASK_CLASSES``. Bare follow-ons (``continue``,
    ``just write the file``) return ``ambiguous`` — the caller resolves
    those against explicit task state instead of guessing.
    """
    if _AMBIGUOUS_FOLLOW_ON.fullmatch(query):
        return "ambiguous"
    if _READONLY_MARKERS.search(query):
        return "review-readonly"
    if _DEBUG_SIGNALS.search(query):
        return "debug"
    if _REVIEW_VERBS.search(query):
        return "review-readonly"
    if _LEARN_DIRECTORY.search(query):
        return "learn-directory"
    if conversational(query):
        return "conversational" if _GREETING.fullmatch(query) else "explain"
    if _NEGATED_ACTION.search(query):
        # A negated action is a constraint, not authorization (spec §8.3):
        # classify what remains once the negated span is removed, so
        # "don't build anything, just explain how sites work" reads as the
        # explanation it requests, while "build a website but don't deploy
        # yet" still reads as a build carrying a no-deploy constraint.
        remaining = _NEGATED_ACTION.sub(" ", query)
        if _EXPLAIN_CLAUSE.search(remaining):
            return "explain"
        if web_build_request(remaining) or _ACTION_VERB.search(remaining):
            return "implement"
        return "ambiguous"
    if web_build_request(query):
        # "make me a website": the narrow action+web-noun rule (spec §8.2).
        return "implement"
    if _ACTION_VERB.search(query):
        return "implement"
    return "ambiguous"


def classify_task_conservative(query: str) -> str:
    """Like :func:`classify_task` but quoted example spans are ignored first.

    A routing question that quotes "make me a website" is not a request to
    build one (spec §8.3). For queries without paired quotes this is exactly
    :func:`classify_task`.
    """
    return classify_task(strip_quoted_spans(query))
