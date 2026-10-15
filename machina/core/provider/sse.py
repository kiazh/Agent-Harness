"""Shared SSE field parsing for provider streaming transports.

Every SSE-streaming provider (direct, anthropic, chatgpt, copilot,
openrouter) routes its ``data:`` / ``event:`` line handling through
:func:`sse_field` so the wire tolerance is defined exactly once:

* the single space after the colon is optional (``data:{...}`` and
  ``data:[DONE]`` are legal SSE — the spec strips at most one leading
  space);
* a bare field name with no colon (``data``) is the empty value;
* a single leading UTF-8 BOM (``U+FEFF``) is ignored, per the SSE spec
  ("event stream interpretation"), so a BOM-prefixed first frame is not
  dropped (final-review P3-4);
* anything else (blank lines, ``:`` comments, other fields) is not a
  match and is reported as ``None`` so callers skip it.

Ollama is intentionally excluded: it streams NDJSON, not SSE.
"""

from __future__ import annotations

#: UTF-8 byte-order mark: the SSE spec requires a single leading BOM be
#: ignored. At most one is stripped; a second is payload.
_BOM = "\ufeff"


def sse_field(line: str, field: str) -> str | None:
    """Return the value of SSE *field* on *line*, or None when absent.

    Only one leading space is stripped (per the SSE spec); further
    whitespace is payload and is preserved for ``json.loads`` (which
    tolerates it) and for exact-match sentinels (callers compare
    ``value.strip()``). A single leading UTF-8 BOM is ignored.
    """
    if line.startswith(_BOM):
        line = line[1:]
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
