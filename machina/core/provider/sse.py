"""Shared SSE field parsing for provider streaming transports.

Every SSE-streaming provider (direct, anthropic, chatgpt, copilot,
openrouter) routes its ``data:`` / ``event:`` line handling through
:func:`sse_field` so the wire tolerance is defined exactly once:

* the single space after the colon is optional (``data:{...}`` and
  ``data:[DONE]`` are legal SSE — the spec strips at most one leading
  space);
* a bare field name with no colon (``data``) is the empty value;
* anything else (blank lines, ``:`` comments, other fields) is not a
  match and is reported as ``None`` so callers skip it.

Ollama is intentionally excluded: it streams NDJSON, not SSE.
"""

from __future__ import annotations


def sse_field(line: str, field: str) -> str | None:
    """Return the value of SSE *field* on *line*, or None when absent.

    Only one leading space is stripped (per the SSE spec); further
    whitespace is payload and is preserved for ``json.loads`` (which
    tolerates it) and for exact-match sentinels (callers compare
    ``value.strip()``).
    """
    prefix = field + ":"
    if line.startswith(prefix):
        value = line[len(prefix) :]
        if value.startswith(" "):
            value = value[1:]
        return value
    if line == field:
        return ""
    return None


def sse_data(line: str) -> str | None:
    """Return the ``data:`` payload of an SSE line, or None."""
    return sse_field(line, "data")


def sse_event(line: str) -> str | None:
    """Return the ``event:`` type of an SSE line, or None."""
    return sse_field(line, "event")
