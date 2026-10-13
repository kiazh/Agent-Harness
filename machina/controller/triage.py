"""Task-state-aware resolution over the deterministic classifier.

``classify_task`` (in ``machina.core.intent``) never guesses: bare
follow-ons such as ``continue`` or ``just write the file`` return
``ambiguous``. This module resolves that label against explicit turn
state supplied by the caller.
"""

from machina.core.intent import TASK_CLASSES, classify_task

DEFAULT_FALLBACK = "implement"


def resolve_task(query: str, task_state=None, default: str | None = DEFAULT_FALLBACK) -> str:
    """Resolve *query* to a task class, using *task_state* for ambiguity.

    - Non-ambiguous labels pass through untouched.
    - ``ambiguous`` with ``task_state={"active_class": <class>}`` inherits
      that class (explicit state wins over guessing).
    - Otherwise ``default`` applies (``implement`` unless the caller passes
      ``None``, which preserves ``ambiguous`` for upstream handling).
    """
    label = classify_task(query)
    if label != "ambiguous":
        return label
    if isinstance(task_state, dict):
        active = task_state.get("active_class")
        if active in TASK_CLASSES and active != "ambiguous":
            return active
    if default in TASK_CLASSES and default != "ambiguous":
        return default
    return "ambiguous"
