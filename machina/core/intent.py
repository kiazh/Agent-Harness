"""Cheap conversational routing shared by skills and learned task strategies.

This is guidance, not an authorization decision or a semantic classifier.
Uncertain requests remain eligible for task routing; no provider call is added.
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
    r"migrate|deploy|install|configure)\b",
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
    r"same\s+for\s+the\s+rest)\s*[!?.]*\s*",
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
    r"migrate|deploy|install|configure)\b",
    re.I,
)

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
    if _ACTION_VERB.search(query):
        return "implement"
    return "ambiguous"
