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
    r"inspect|run|verify|investigate|search|read|write|export|plan|design)\b",
    re.I,
)


def conversational(query: str) -> bool:
    """Whether a greeting/question has no explicit follow-on action."""
    return bool(
        _GREETING.fullmatch(query)
        or (_QUESTION.match(query) and not _FOLLOW_ON_ACTION.search(query))
    )
